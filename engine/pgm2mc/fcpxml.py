"""Analysis dict (docs/CONTRACT.md v1 / v2) -> FCPXML 1.10 documents (multicam + cut, conform, stacked).

All positions are integer frames at the PGM rate; they become rational seconds only when written.
Geometry follows docs/RESEARCH.md section 12 ("Mapping the Analysis JSON to FCPXML"); v2 transitions, stills
and SuperSource layers follow docs/RESEARCH2.md and CONTRACT "Writer decisions".
"""
import os
import re
import struct
import xml.etree.ElementTree as ET
import zlib
from fractions import Fraction
from urllib.parse import quote

FCPXML_VERSION = "1.10"
PGM_ANGLE = "pgm"
DF_RATES = (Fraction(30000, 1001), Fraction(60000, 1001))
AUDIO_HZ = {32000: "32k", 44100: "44.1k", 48000: "48k", 88200: "88.2k", 96000: "96k",
            176400: "176.4k", 192000: "192k"}
LANE_KINDS = ("still", "supersource")
CROSS_DISSOLVE = "Cross Dissolve"
CROSS_DISSOLVE_UID = "FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265"
TRANSITION_MARKER = "PGM transition"


class FcpxmlError(ValueError):
    pass


def rational(frames, fd):
    """Integer frame count * frame duration -> FCPXML time string ("0s", "5s", "1001/30000s")."""
    v = Fraction(int(frames)) * fd
    if v == 0:
        return "0s"
    if v.denominator == 1:
        return "%ds" % v.numerator
    return "%d/%ds" % (v.numerator, v.denominator)


def file_url(path):
    """Absolute macOS/Linux or Windows path -> percent-encoded file:// URL (RFC 8089)."""
    p = str(path)
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
    if m:
        rest = m.group(2).replace("\\", "/")
        return "file:///%s:/%s" % (m.group(1).upper(), quote(rest, safe="/"))
    if p.startswith("\\\\") or (p.startswith("//") and not p.startswith("///")):
        parts = p.replace("\\", "/").lstrip("/").split("/", 1)
        rest = parts[1] if len(parts) > 1 else ""
        return "file://%s/%s" % (quote(parts[0], safe=""), quote(rest, safe="/"))
    if not p.startswith("/"):
        p = os.path.abspath(p)
        if os.sep == "\\":
            return file_url(p)
    return "file://" + quote(p, safe="/")


def _num(v):
    """Up to 4 decimals, trailing zeros stripped, never "-0"."""
    txt = ("%.4f" % v).rstrip("0").rstrip(".")
    return "0" if txt in ("", "-0") else txt


def box_params(rect, crop, src_w, src_h, seq_w, seq_h, uniform_tol=0.02):
    """RESEARCH2 section 7: CONTRACT rect [x0,y0,x1,y1] + crop [l,t,r,b] -> FCPXML transform / trim-rect values
    and the Resolve SetProperty equivalents. Spatial conform = fit."""
    x0, y0, x1, y1 = rect
    l, t, r, b = crop
    fit = min(seq_w / float(src_w), seq_h / float(src_h))
    cw, ch = src_w * fit, src_h * fit
    sx = (x1 - x0) * seq_w / (cw * (1 - l - r))
    sy = (y1 - y0) * seq_h / (ch * (1 - t - b))
    if abs(sx - sy) <= uniform_tol * max(sx, sy):
        sx = sy = (sx + sy) / 2
    dcx, dcy = cw * (l - r) / 2, ch * (t - b) / 2
    tcx, tcy = ((x0 + x1) / 2 - 0.5) * seq_w, ((y0 + y1) / 2 - 0.5) * seq_h
    px = tcx - sx * dcx
    py = -(tcy - sy * dcy)
    return {
        "fcpxml": {
            "position": (px / seq_h * 100, py / seq_h * 100),
            "scale": (sx, sy),
            "trim": {"left": l * cw / ch * 100, "top": t * 100, "right": r * cw / ch * 100, "bottom": b * 100},
        },
        "resolve": {"Scaling": 2, "ZoomGang": sx == sy, "ZoomX": sx, "ZoomY": sy, "Pan": px, "Tilt": py,
                    "CropRetain": True, "CropLeft": l * cw, "CropRight": r * cw, "CropTop": t * ch,
                    "CropBottom": b * ch},
    }


def transition_place(cut, d):
    """Frames -> (offset, duration, frames needed from A after its out, frames needed from B before its in)."""
    pre = d // 2
    return cut - pre, d, d - pre, pre


def dip_layout(edit, d):
    """Dip as dissolve -> colour slug -> dissolve (RESEARCH2 1.5)."""
    d1 = d // 2
    d2 = d - d1
    c1 = edit - (d1 - d1 // 2)
    c2 = edit + d2 // 2
    return {"a_out": c1, "slug": (c1, c2), "slug_start": d1 // 2,
            "t1": (c1 - d1 // 2, d1), "t2": (c2 - d2 // 2, d2), "b_in": c2}


def _clamp_crop(crop):
    c = [min(max(float(v), 0.0), 0.95) for v in crop]
    for i, j in ((0, 2), (1, 3)):
        if c[i] + c[j] > 0.9:
            k = 0.9 / (c[i] + c[j])
            c[i], c[j] = c[i] * k, c[j] * k
    return c


def write_png(path, width, height, rgb):
    """Solid-colour RGB PNG (stdlib only)."""
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    row = b"\x00" + bytes(bytearray(rgb)) * width
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * height, 9)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(data)


def _stem(path):
    base = re.split(r"[\\/]", str(path))[-1]
    return os.path.splitext(base)[0] or base


class Plan:
    """Validated geometry for one Analysis: angles, multicam range and effective (media-safe) segments."""

    def __init__(self, analysis, name=None, assets_dir=None):
        self.version = analysis.get("version")
        if self.version not in (1, 2):
            raise FcpxmlError("unsupported analysis version %r" % self.version)
        self.v2 = self.version == 2
        self.warnings = []
        fps = Fraction(int(analysis["fps"]["num"]), int(analysis["fps"]["den"]))
        if fps <= 0:
            raise FcpxmlError("bad fps %s" % fps)
        self.fps = fps
        self.fd = 1 / fps
        self.df = bool(analysis.get("drop_frame"))
        if self.df and fps not in DF_RATES:
            self.warnings.append("drop-frame timecode is only valid at 29.97/59.94 fps; writing NDF")
            self.df = False
        self.tc_format = "DF" if self.df else "NDF"

        pgm = analysis["program"]
        self.pgm = self._angle(pgm, PGM_ANGLE, "PGM", 0)
        self.pgm_tc = self.pgm["tc"]
        self.pgm_dur = self.pgm["dur"]
        if self.pgm_dur <= 0:
            raise FcpxmlError("program has no frames")
        self.isos = []
        seen = {PGM_ANGLE}
        for iso in analysis["isos"]:
            iid = str(iso["id"])
            if iid in seen:
                raise FcpxmlError("duplicate ISO id %r" % iid)
            seen.add(iid)
            self.isos.append(self._angle(iso, iid, iso.get("name") or _stem(iso["path"]),
                                         -int(iso["offset_frames"])))
        if not self.isos:
            raise FcpxmlError("analysis has no ISOs")
        self.angles = self.isos + [self.pgm]
        self.by_id = dict((a["id"], a) for a in self.angles)

        self.origin = min(a["pos"] for a in self.angles)
        self.mc_len = max(a["pos"] + a["dur"] for a in self.angles) - self.origin
        self.mc_tc0 = max(0, self.pgm_tc + self.origin)
        self.name = name or self.pgm["name"]
        self.seq_w = self.pgm["width"] or 1920
        self.seq_h = self.pgm["height"] or 1080
        self.assets_dir = assets_dir
        self.stills = {}
        self.supersources = {}
        if self.v2:
            self._load_v2(analysis)
        self.segments = self._effective_segments(analysis["segments"])
        self.transitions = self._plan_transitions(analysis.get("transitions") or []) if self.v2 else []
        self.spine = self._build_spine()

    def _load_v2(self, analysis):
        for st in analysis.get("stills") or []:
            sid = str(st["id"])
            self.stills[sid] = {"id": sid, "path": st["path"], "role": st.get("role") or "still",
                                "width": int(st.get("width") or 0) or self.seq_w,
                                "height": int(st.get("height") or 0) or self.seq_h}
        iso_ids = set(a["id"] for a in self.isos)
        for ss in analysis.get("supersources") or []:
            sid = str(ss["id"])
            boxes = []
            for n, b in enumerate(ss.get("boxes") or []):
                angle = str(b.get("angle"))
                try:
                    rect = [float(v) for v in b["rect"]]
                    crop = _clamp_crop(b.get("crop") or [0, 0, 0, 0])
                    ok = angle in iso_ids and len(rect) == 4 and len(crop) == 4 and \
                        rect[2] > rect[0] and rect[3] > rect[1]
                except (KeyError, TypeError, ValueError):
                    ok = False
                if not ok:
                    self.warnings.append("SuperSource %s: box %d is invalid or uses an unknown ISO; skipped"
                                         % (sid, n + 1))
                    continue
                iso = self.by_id[angle]
                geo = box_params(rect, crop, iso["width"] or self.seq_w, iso["height"] or self.seq_h,
                                 self.seq_w, self.seq_h)
                boxes.append({"angle": angle, "layer": int(b.get("layer") or n + 1), "rect": rect, "crop": crop,
                              "fcpxml": geo["fcpxml"], "resolve": geo["resolve"]})
            boxes.sort(key=lambda q: q["layer"])
            bg = ss.get("background")
            if bg is not None and str(bg) not in self.stills:
                self.warnings.append("SuperSource %s: background still %r not found" % (sid, bg))
                bg = None
            if ss.get("animated"):
                self.warnings.append("SuperSource %s is animated; its boxes use the average layout" % sid)
            self.supersources[sid] = {"id": sid, "background": None if bg is None else str(bg), "boxes": boxes}

    def _label(self, s, angle):
        """-> (angle, kind, still id, supersource id) for one input segment."""
        kind = s.get("kind") if self.v2 else None
        if kind == "still":
            sid = s.get("still")
            if sid is not None and str(sid) in self.stills:
                return PGM_ANGLE, "still", str(sid), None
            self.warnings.append("segment %s-%s references unknown still %r; using PGM" % (s["start"], s["end"], sid))
            return PGM_ANGLE, "pgm", None, None
        if kind == "supersource":
            sid = s.get("supersource")
            if sid is not None and str(sid) in self.supersources:
                return PGM_ANGLE, "supersource", None, str(sid)
            self.warnings.append("segment %s-%s references unknown SuperSource %r; using PGM"
                                 % (s["start"], s["end"], sid))
            return PGM_ANGLE, "pgm", None, None
        return angle, ("pgm" if angle == PGM_ANGLE else "camera"), None, None

    def _angle(self, d, aid, name, pos):
        return {"id": aid, "name": str(name), "path": d["path"], "pos": pos,
                "tc": int(d.get("start_tc_frames") or 0), "dur": int(d["duration_frames"]),
                "width": int(d.get("width") or 0), "height": int(d.get("height") or 0),
                "has_audio": bool(d.get("has_audio")), "audio_rate": int(d.get("audio_rate") or 0),
                "audio_channels": int(d.get("audio_channels") or 0)}

    def mc_pos(self, angle):
        """Multicam time (frames, absolute TC) where the angle's file starts."""
        return self.mc_tc0 + angle["pos"] - self.origin

    def _media_range(self, angle_id):
        a = self.by_id[angle_id]
        return a["pos"], a["pos"] + a["dur"]

    def _effective_segments(self, segments):
        segs = sorted(segments, key=lambda s: int(s["start"]))
        expect = 0
        out = []
        for s in segs:
            a, b, angle = int(s["start"]), int(s["end"]), str(s["angle"])
            if a != expect or b <= a:
                raise FcpxmlError("segments are not contiguous at frame %d" % expect)
            expect = b
            if angle not in self.by_id:
                raise FcpxmlError("segment %d-%d uses unknown angle %r" % (a, b, angle))
            score = float(s.get("score", 0.0))
            angle, kind, still, ss = self._label(s, angle)
            if kind != "camera":
                out.append([a, b, angle, score, kind, still, ss])
                continue
            iso = self.by_id[angle]
            lo, hi = max(a, iso["pos"]), min(b, iso["pos"] + iso["dur"])
            pgm = [PGM_ANGLE, score, "pgm", None, None]
            if lo >= hi:
                out.append([a, b] + pgm)
                self.warnings.append("%s has no media for PGM frames %d-%d; using PGM there"
                                     % (iso["name"], a, b))
                continue
            if a < lo:
                out.append([a, lo] + pgm)
            out.append([lo, hi, angle, score, kind, None, None])
            if hi < b:
                out.append([hi, b] + pgm)
            if a < lo or hi < b:
                self.warnings.append("%s has no media for part of PGM frames %d-%d; using PGM there"
                                     % (iso["name"], a, b))
        if expect != self.pgm_dur:
            raise FcpxmlError("segments cover %d frames, program has %d" % (expect, self.pgm_dur))
        merged = []
        for seg in out:
            if merged and merged[-1][2] == seg[2] and merged[-1][4:] == seg[4:]:
                prev = merged[-1]
                la, lb = prev[1] - prev[0], seg[1] - seg[0]
                prev[3] = (prev[3] * la + seg[3] * lb) / (la + lb)
                prev[1] = seg[1]
            else:
                merged.append(seg)
        return [{"start": a, "end": b, "angle": g, "score": sc, "kind": k, "still": st, "supersource": ss}
                for a, b, g, sc, k, st, ss in merged]

    def _plan_transitions(self, transitions):
        """Validate transitions against the effective cuts; clamp them to the available room and media."""
        segs = self.segments
        index = dict((s["start"], i) for i, s in enumerate(segs) if i > 0)
        reqs = {}
        for t in transitions:
            try:
                edit, dur = int(t["edit"]), int(t["duration"])
                kind = str(t.get("kind") or "dissolve")
                color = tuple(min(max(int(c), 0), 255) for c in (t.get("color") or (0, 0, 0)))[:3]
            except (KeyError, TypeError, ValueError):
                self.warnings.append("malformed transition %r dropped" % (t,))
                continue
            if kind not in ("dissolve", "dip") or len(color) != 3:
                self.warnings.append("transition at PGM frame %d: unsupported kind %r; dropped" % (edit, kind))
                continue
            if edit not in index:
                self.warnings.append("transition at PGM frame %d is not on a cut; dropped" % edit)
                continue
            if edit in reqs or dur < 2:
                self.warnings.append("transition at PGM frame %d dropped (duplicate or shorter than 2 frames)"
                                     % edit)
                continue
            reqs[edit] = (kind, dur, color)
        used = [0] * len(segs)
        out = []
        for edit in sorted(reqs):
            kind, dur, color = reqs[edit]
            i = index[edit]
            A, B = segs[i - 1], segs[i]
            covered = A["kind"] in LANE_KINDS or B["kind"] in LANE_KINDS
            reserve = 1 if covered or kind == "dip" else 0
            nxt = reqs.get(B["end"])
            max_pre = A["end"] - A["start"] - used[i - 1] - reserve
            max_post = B["end"] - B["start"] - (nxt[1] // 2 if nxt else 0) - reserve
            if not covered and kind == "dissolve":
                max_pre = min(max_pre, B["start"] - self._media_range(B["angle"])[0])
                max_post = min(max_post, self._media_range(A["angle"])[1] - A["end"])
            d = dur
            if d // 2 > max_pre or d - d // 2 > max_post:
                d = min(dur, 2 * min(max_pre, max_post))
                if d < 2:
                    self.warnings.append("%s at PGM frame %d dropped: not enough media or clip length around "
                                         "the cut" % (kind, edit))
                    continue
                self.warnings.append("%s at PGM frame %d shortened from %d to %d frames (not enough media or "
                                     "clip length around the cut)" % (kind, edit, dur, d))
            pre = d // 2
            used[i] = d - pre
            out.append({"edit": edit, "duration": d, "pre": pre, "post": d - pre,
                        "kind": "covered" if covered else kind, "source_kind": kind, "color": color})
        return out

    def slug_path(self, color):
        folder = self.assets_dir or os.path.abspath(safe_filename(self.name) + "_stills")
        return os.path.join(folder, "dip_%02X%02X%02X.png" % tuple(color))

    def slug_colors(self):
        return sorted(set(item["color"] for item in self.spine if item["type"] == "slug"))

    def _build_spine(self):
        """Spine items: clips (with lane layers and markers), transitions and dip slugs, in PGM frames."""
        segs = self.segments
        s0 = [s["start"] for s in segs]
        s1 = [s["end"] for s in segs]
        lane_a, lane_b = list(s0), list(s1)
        before = {}
        marks = []
        index = dict((s["start"], i) for i, s in enumerate(segs))
        for t in self.transitions:
            i = index[t["edit"]]
            e, pre, post, d = t["edit"], t["pre"], t["post"], t["duration"]
            if t["kind"] == "covered":
                a_lane, b_lane = segs[i - 1]["kind"] in LANE_KINDS, segs[i]["kind"] in LANE_KINDS
                cut = e if a_lane and b_lane else (e - pre if b_lane else e + post)
                s1[i - 1] = s0[i] = cut
                if a_lane:
                    lane_b[i - 1] = e - pre
                if b_lane:
                    lane_a[i] = e + post
                marks.append(e)
            elif t["kind"] == "dissolve":
                off, dd, _, _ = transition_place(e, d)
                before[i] = [{"type": "transition", "offset": off, "duration": dd}]
            else:
                lay = dip_layout(e, d)
                s1[i - 1], s0[i] = lay["a_out"], lay["b_in"]
                before[i] = [{"type": "transition", "offset": lay["t1"][0], "duration": lay["t1"][1]},
                             {"type": "slug", "color": t["color"], "s0": lay["slug"][0], "s1": lay["slug"][1],
                              "start": lay["slug_start"]},
                             {"type": "transition", "offset": lay["t2"][0], "duration": lay["t2"][1]}]
        spine = []
        for i, seg in enumerate(segs):
            spine.extend(before.get(i, []))
            lanes = self._lanes(seg, lane_a[i], lane_b[i]) if seg["kind"] in LANE_KINDS else []
            spine.append({"type": "clip", "seg": seg, "angle": seg["angle"], "s0": s0[i], "s1": s1[i],
                          "lanes": lanes, "markers": []})
        for e in marks:
            for item in spine:
                if item["type"] == "clip" and item["s0"] <= e < item["s1"]:
                    item["markers"].append(e)
                    break
        return spine

    def _lanes(self, seg, a, b):
        if a >= b:
            self.warnings.append("no room for the layers of PGM frames %d-%d between transitions"
                                 % (seg["start"], seg["end"]))
            return []
        if seg["kind"] == "still":
            return [{"kind": "still", "still": seg["still"], "lane": 1, "a": a, "b": b, "name": seg["still"]}]
        ss = self.supersources[seg["supersource"]]
        out = []
        if ss["background"]:
            out.append({"kind": "still", "still": ss["background"], "lane": 1, "a": a, "b": b,
                        "name": "%s SuperSource BG" % self.name})
        for lane, box in enumerate(ss["boxes"], 2):
            iso = self.by_id[box["angle"]]
            lo, hi = max(a, iso["pos"]), min(b, iso["pos"] + iso["dur"])
            if lo >= hi:
                self.warnings.append("SuperSource %s: %s has no media for PGM frames %d-%d; box skipped"
                                     % (ss["id"], iso["name"], a, b))
                continue
            if lo > a or hi < b:
                self.warnings.append("SuperSource %s: %s covers only PGM frames %d-%d of %d-%d"
                                     % (ss["id"], iso["name"], lo, hi, a, b))
            out.append({"kind": "box", "angle": box["angle"], "lane": lane, "a": lo, "b": hi,
                        "name": "%s box" % iso["name"], "box": box})
        return out

    def clip_name(self, seg):
        if seg["kind"] == "still":
            return "%s (still %s)" % (self.pgm["name"], seg["still"])
        if seg["kind"] == "supersource":
            return "%s (SuperSource %s)" % (self.pgm["name"], seg["supersource"])
        return self.by_id[seg["angle"]]["name"]


class _Doc:
    def __init__(self, plan):
        self.plan = plan
        self.root = ET.Element("fcpxml", version=FCPXML_VERSION)
        self.resources = ET.SubElement(self.root, "resources")
        self.formats = {}
        self.asset_ids = {}
        self.next_id = 1
        self.fmt_pgm = self.format_for(plan.pgm)

    def t(self, frames):
        return rational(frames, self.plan.fd)

    def new_id(self):
        rid = "r%d" % self.next_id
        self.next_id += 1
        return rid

    def format_for(self, angle):
        key = (self.plan.fd, angle["width"], angle["height"])
        if key not in self.formats:
            rid = self.new_id()
            attrs = {"id": rid, "frameDuration": rational(1, self.plan.fd)}
            if angle["width"] and angle["height"]:
                attrs["width"] = str(angle["width"])
                attrs["height"] = str(angle["height"])
            ET.SubElement(self.resources, "format", attrs)
            self.formats[key] = rid
        return self.formats[key]

    def add_assets(self):
        for a in self.plan.angles:
            self.format_for(a)
        for a in self.plan.angles:
            rid = self.new_id()
            self.asset_ids[a["id"]] = rid
            attrs = {"id": rid, "name": a["name"], "start": self.t(a["tc"]), "duration": self.t(a["dur"]),
                     "hasVideo": "1", "format": self.format_for(a), "videoSources": "1"}
            if a["has_audio"]:
                attrs.update({"hasAudio": "1", "audioSources": "1",
                              "audioChannels": str(a["audio_channels"] or 2),
                              "audioRate": str(a["audio_rate"] or 48000)})
            else:
                attrs["hasAudio"] = "0"
            asset = ET.SubElement(self.resources, "asset", attrs)
            ET.SubElement(asset, "media-rep", kind="original-media", src=file_url(a["path"]))

    def add_v2_resources(self):
        """Still / slug assets and the Cross Dissolve effect used by the plan's spine."""
        plan = self.plan
        self.still_ids = {}
        self.effect_id = None
        used = []
        for item in plan.spine:
            for ln in item.get("lanes", ()):
                if ln["kind"] == "still" and ln["still"] not in used:
                    used.append(ln["still"])
        for sid in used:
            st = plan.stills[sid]
            self._still_asset(sid, sid, st["path"], st["width"], st["height"])
        for item in plan.spine:
            if item["type"] == "slug":
                key = ("slug", item["color"])
                if key not in self.still_ids:
                    self._still_asset(key, "dip_%02X%02X%02X" % item["color"], plan.slug_path(item["color"]),
                                      plan.seq_w, plan.seq_h)
        if any(item["type"] == "transition" for item in plan.spine):
            self.effect_id = self.new_id()
            ET.SubElement(self.resources, "effect", id=self.effect_id, name=CROSS_DISSOLVE, uid=CROSS_DISSOLVE_UID)

    def _still_asset(self, key, name, path, w, h):
        fkey = ("still", w, h)
        if fkey not in self.formats:
            rid = self.new_id()
            ET.SubElement(self.resources, "format", id=rid, name="FFVideoFormatRateUndefined", width=str(w),
                          height=str(h), colorSpace="1-13-1")
            self.formats[fkey] = rid
        rid = self.new_id()
        self.still_ids[key] = rid
        asset = ET.SubElement(self.resources, "asset", id=rid, name=name, start="0s", duration="0s", hasVideo="1",
                              format=self.formats[fkey], videoSources="1")
        ET.SubElement(asset, "media-rep", kind="original-media", src=file_url(path))

    def add_between(self, spine, item):
        """A spine transition or dip slug."""
        pgm_tc = self.plan.pgm_tc
        if item["type"] == "transition":
            tr = ET.SubElement(spine, "transition", name=CROSS_DISSOLVE, offset=self.t(pgm_tc + item["offset"]),
                               duration=self.t(item["duration"]))
            ET.SubElement(tr, "filter-video", ref=self.effect_id, name=CROSS_DISSOLVE)
            return
        color = item["color"]
        name = "dip to black" if color == (0, 0, 0) else "dip to #%02X%02X%02X" % color
        ET.SubElement(spine, "video", {"ref": self.still_ids[("slug", color)], "offset": self.t(pgm_tc + item["s0"]),
                                       "name": name, "start": self.t(item["start"]),
                                       "duration": self.t(item["s1"] - item["s0"])})

    def add_layers(self, parent, item, local0):
        """Lane layers and markers of one spine clip; local0 = parent-local time (frames) at item s0."""
        plan = self.plan
        for ln in item["lanes"]:
            off, dur = self.t(local0 + ln["a"] - item["s0"]), self.t(ln["b"] - ln["a"])
            if ln["kind"] == "still":
                ET.SubElement(parent, "video", {"ref": self.still_ids[ln["still"]], "lane": str(ln["lane"]),
                                                "offset": off, "name": ln["name"], "start": "0s", "duration": dur})
                continue
            iso = plan.by_id[ln["angle"]]
            clip = ET.SubElement(parent, "asset-clip", {
                "ref": self.asset_ids[iso["id"]], "lane": str(ln["lane"]), "offset": off, "name": ln["name"],
                "start": self.t(iso["tc"] + ln["a"] - iso["pos"]), "duration": dur, "format": self.format_for(iso),
                "tcFormat": plan.tc_format, "srcEnable": "video"})
            geo = ln["box"]["fcpxml"]
            crop = ET.SubElement(clip, "adjust-crop", mode="trim")
            ET.SubElement(crop, "trim-rect", left=_num(geo["trim"]["left"]), top=_num(geo["trim"]["top"]),
                          right=_num(geo["trim"]["right"]), bottom=_num(geo["trim"]["bottom"]))
            ET.SubElement(clip, "adjust-conform", type="fit")
            ET.SubElement(clip, "adjust-transform", position="%s %s" % tuple(_num(v) for v in geo["position"]),
                          scale="%s %s" % tuple(_num(v) for v in geo["scale"]))
        for e in item["markers"]:
            ET.SubElement(parent, "marker", start=self.t(local0 + e - item["s0"]), duration=self.t(1),
                          value=TRANSITION_MARKER)

    def whole_clip(self, parent, angle, offset, lane=None):
        attrs = {"ref": self.asset_ids[angle["id"]], "name": angle["name"]}
        if lane is not None:
            attrs["lane"] = str(lane)
        attrs.update({"offset": self.t(offset), "start": self.t(angle["tc"]), "duration": self.t(angle["dur"]),
                      "format": self.format_for(angle), "tcFormat": self.plan.tc_format})
        return ET.SubElement(parent, "asset-clip", attrs)

    def project(self, title, tc_start, duration):
        library = self.root.find("library")
        if library is None:
            library = ET.SubElement(self.root, "library")
            ET.SubElement(library, "event", name=self.plan.name)
        proj = ET.SubElement(library.find("event"), "project", name=title)
        pgm = self.plan.pgm
        seq = ET.SubElement(proj, "sequence", {
            "format": self.fmt_pgm, "duration": self.t(duration), "tcStart": self.t(tc_start),
            "tcFormat": self.plan.tc_format,
            "audioLayout": "mono" if pgm["audio_channels"] == 1 else "stereo",
            "audioRate": AUDIO_HZ.get(pgm["audio_rate"], "48k")})
        return ET.SubElement(seq, "spine")


def build_multicam(plan, audio="pgm"):
    if audio not in ("pgm", "follow"):
        raise FcpxmlError("audio must be 'pgm' or 'follow'")
    doc = _Doc(plan)
    doc.add_assets()
    doc.add_v2_resources()
    mid = doc.new_id()
    media = ET.SubElement(doc.resources, "media", id=mid, name="%s Multicam" % plan.name)
    mc = ET.SubElement(media, "multicam", {"format": doc.fmt_pgm, "duration": doc.t(plan.mc_len),
                                           "tcStart": doc.t(plan.mc_tc0), "tcFormat": plan.tc_format})
    for a in plan.angles:
        ang = ET.SubElement(mc, "mc-angle", name=a["name"], angleID=a["id"])
        lead = a["pos"] - plan.origin
        if lead > 0:
            ET.SubElement(ang, "gap", name="Gap", offset=doc.t(plan.mc_tc0), start=doc.t(plan.mc_tc0),
                          duration=doc.t(lead))
        doc.whole_clip(ang, a, plan.mc_pos(a))

    spine = doc.project("%s Multicam Cut" % plan.name, plan.pgm_tc, plan.pgm_dur)
    pgm_mc = plan.mc_pos(plan.pgm)
    for item in plan.spine:
        if item["type"] != "clip":
            doc.add_between(spine, item)
            continue
        angle = plan.by_id[item["angle"]]
        clip = ET.SubElement(spine, "mc-clip", {
            "ref": mid, "name": plan.clip_name(item["seg"]), "offset": doc.t(plan.pgm_tc + item["s0"]),
            "start": doc.t(pgm_mc + item["s0"]), "duration": doc.t(item["s1"] - item["s0"])})
        if audio == "pgm" and angle["id"] != PGM_ANGLE:
            ET.SubElement(clip, "mc-source", angleID=angle["id"], srcEnable="video")
            ET.SubElement(clip, "mc-source", angleID=PGM_ANGLE, srcEnable="audio")
        else:
            ET.SubElement(clip, "mc-source", angleID=angle["id"], srcEnable="all")
        doc.add_layers(clip, item, pgm_mc + item["s0"])
    return doc.root


def build_conform(plan):
    doc = _Doc(plan)
    doc.add_assets()
    doc.add_v2_resources()
    spine = doc.project("%s Conform" % plan.name, plan.pgm_tc, plan.pgm_dur)
    first = True
    for item in plan.spine:
        if item["type"] != "clip":
            doc.add_between(spine, item)
            continue
        angle = plan.by_id[item["angle"]]
        src = angle["tc"] + item["s0"] - angle["pos"]
        clip = ET.SubElement(spine, "asset-clip", {
            "ref": doc.asset_ids[angle["id"]], "name": plan.clip_name(item["seg"]),
            "offset": doc.t(plan.pgm_tc + item["s0"]), "start": doc.t(src),
            "duration": doc.t(item["s1"] - item["s0"]), "srcEnable": "video",
            "format": doc.format_for(angle), "tcFormat": plan.tc_format})
        if first and plan.pgm["has_audio"]:
            ET.SubElement(clip, "asset-clip", {
                "ref": doc.asset_ids[PGM_ANGLE], "name": "%s audio" % plan.pgm["name"], "lane": "-1",
                "offset": doc.t(src), "start": doc.t(plan.pgm_tc), "duration": doc.t(plan.pgm_dur),
                "srcEnable": "audio", "format": doc.fmt_pgm, "tcFormat": plan.tc_format})
        first = False
        doc.add_layers(clip, item, src)
    return doc.root


def build_stacked(plan):
    doc = _Doc(plan)
    doc.add_assets()
    spine = doc.project("%s Stacked" % plan.name, plan.mc_tc0, plan.mc_len)
    gap = ET.SubElement(spine, "gap", name="Gap", offset=doc.t(plan.mc_tc0), start=doc.t(plan.mc_tc0),
                        duration=doc.t(plan.mc_len))
    for lane, a in enumerate(plan.angles, 1):
        doc.whole_clip(gap, a, plan.mc_pos(a), lane=lane)
    return doc.root


def _indent(elem, level=0):
    pad = "\n" + "    " * level
    if len(elem):
        if not (elem.text or "").strip():
            elem.text = pad + "    "
        for child in elem:
            _indent(child, level + 1)
        if not (child.tail or "").strip():
            child.tail = pad
    if level and not (elem.tail or "").strip():
        elem.tail = pad


def to_string(root):
    _indent(root)
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + body + "\n"


def write(root, path):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(to_string(root))


def safe_filename(name):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip() or "pgm2mc"


def write_all(analysis, outdir, name=None, audio="pgm", assets_dir=None):
    """Write NAME_multicam/_conform/_stacked.fcpxml into outdir (dip slug PNGs into assets_dir, default
    OUTDIR/NAME_stills). Returns (paths dict, plan)."""
    outdir = os.path.abspath(outdir)
    plan = Plan(analysis, name, assets_dir=os.path.abspath(assets_dir) if assets_dir else None)
    if plan.assets_dir is None:
        plan.assets_dir = os.path.join(outdir, safe_filename(plan.name) + "_stills")
    if audio == "pgm" and not plan.pgm["has_audio"]:
        plan.warnings.append("PGM has no audio; the cut timelines will be silent")
    os.makedirs(outdir, exist_ok=True)
    for color in plan.slug_colors():
        path = plan.slug_path(color)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_png(path, plan.seq_w, plan.seq_h, color)
    base = os.path.join(outdir, safe_filename(plan.name))
    paths = {"multicam": base + "_multicam.fcpxml", "conform": base + "_conform.fcpxml",
             "stacked": base + "_stacked.fcpxml"}
    write(build_multicam(plan, audio), paths["multicam"])
    write(build_conform(plan), paths["conform"])
    write(build_stacked(plan), paths["stacked"])
    return paths, plan


def summary(plan):
    """Counts for the CLI RESULT line: transitions, supersources and stills placed in the timelines."""
    still_ids, ss_ids = set(), set()
    for item in plan.spine:
        if item["type"] == "clip":
            if item["seg"]["kind"] == "supersource":
                ss_ids.add(item["seg"]["supersource"])
            for ln in item["lanes"]:
                if ln["kind"] == "still":
                    still_ids.add(ln["still"])
    return {"transitions": len(plan.transitions), "supersources": len(ss_ids), "stills": len(still_ids)}
