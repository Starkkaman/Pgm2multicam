"""Analysis dict (docs/CONTRACT.md v1) -> FCPXML 1.10 documents (multicam + cut, conform, stacked).

All positions are integer frames at the PGM rate; they become rational seconds only when written.
Geometry follows docs/RESEARCH.md section 12 ("Mapping the Analysis JSON to FCPXML").
"""
import os
import re
import xml.etree.ElementTree as ET
from fractions import Fraction
from urllib.parse import quote

FCPXML_VERSION = "1.10"
PGM_ANGLE = "pgm"
DF_RATES = (Fraction(30000, 1001), Fraction(60000, 1001))
AUDIO_HZ = {32000: "32k", 44100: "44.1k", 48000: "48k", 88200: "88.2k", 96000: "96k",
            176400: "176.4k", 192000: "192k"}


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


def _stem(path):
    base = re.split(r"[\\/]", str(path))[-1]
    return os.path.splitext(base)[0] or base


class Plan:
    """Validated geometry for one Analysis: angles, multicam range and effective (media-safe) segments."""

    def __init__(self, analysis, name=None):
        if analysis.get("version") != 1:
            raise FcpxmlError("unsupported analysis version %r" % analysis.get("version"))
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
        self.segments = self._effective_segments(analysis["segments"])

    def _angle(self, d, aid, name, pos):
        return {"id": aid, "name": str(name), "path": d["path"], "pos": pos,
                "tc": int(d.get("start_tc_frames") or 0), "dur": int(d["duration_frames"]),
                "width": int(d.get("width") or 0), "height": int(d.get("height") or 0),
                "has_audio": bool(d.get("has_audio")), "audio_rate": int(d.get("audio_rate") or 0),
                "audio_channels": int(d.get("audio_channels") or 0)}

    def mc_pos(self, angle):
        """Multicam time (frames, absolute TC) where the angle's file starts."""
        return self.mc_tc0 + angle["pos"] - self.origin

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
            if angle == PGM_ANGLE:
                out.append([a, b, angle, score])
                continue
            iso = self.by_id[angle]
            lo, hi = max(a, iso["pos"]), min(b, iso["pos"] + iso["dur"])
            if lo >= hi:
                out.append([a, b, PGM_ANGLE, score])
                self.warnings.append("%s has no media for PGM frames %d-%d; using PGM there"
                                     % (iso["name"], a, b))
                continue
            if a < lo:
                out.append([a, lo, PGM_ANGLE, score])
            out.append([lo, hi, angle, score])
            if hi < b:
                out.append([hi, b, PGM_ANGLE, score])
            if a < lo or hi < b:
                self.warnings.append("%s has no media for part of PGM frames %d-%d; using PGM there"
                                     % (iso["name"], a, b))
        if expect != self.pgm_dur:
            raise FcpxmlError("segments cover %d frames, program has %d" % (expect, self.pgm_dur))
        merged = []
        for seg in out:
            if merged and merged[-1][2] == seg[2]:
                prev = merged[-1]
                la, lb = prev[1] - prev[0], seg[1] - seg[0]
                prev[3] = (prev[3] * la + seg[3] * lb) / (la + lb)
                prev[1] = seg[1]
            else:
                merged.append(seg)
        return [{"start": a, "end": b, "angle": g, "score": sc} for a, b, g, sc in merged]


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
    for s in plan.segments:
        angle = plan.by_id[s["angle"]]
        clip = ET.SubElement(spine, "mc-clip", {
            "ref": mid, "name": angle["name"], "offset": doc.t(plan.pgm_tc + s["start"]),
            "start": doc.t(pgm_mc + s["start"]), "duration": doc.t(s["end"] - s["start"])})
        if audio == "pgm" and angle["id"] != PGM_ANGLE:
            ET.SubElement(clip, "mc-source", angleID=angle["id"], srcEnable="video")
            ET.SubElement(clip, "mc-source", angleID=PGM_ANGLE, srcEnable="audio")
        else:
            ET.SubElement(clip, "mc-source", angleID=angle["id"], srcEnable="all")
    return doc.root


def build_conform(plan):
    doc = _Doc(plan)
    doc.add_assets()
    spine = doc.project("%s Conform" % plan.name, plan.pgm_tc, plan.pgm_dur)
    first = None
    for s in plan.segments:
        angle = plan.by_id[s["angle"]]
        src = angle["tc"] + s["start"] - angle["pos"]
        clip = ET.SubElement(spine, "asset-clip", {
            "ref": doc.asset_ids[angle["id"]], "name": angle["name"], "offset": doc.t(plan.pgm_tc + s["start"]),
            "start": doc.t(src), "duration": doc.t(s["end"] - s["start"]), "srcEnable": "video",
            "format": doc.format_for(angle), "tcFormat": plan.tc_format})
        if first is None:
            first = (clip, src)
    if plan.pgm["has_audio"]:
        clip, src = first
        ET.SubElement(clip, "asset-clip", {
            "ref": doc.asset_ids[PGM_ANGLE], "name": "%s audio" % plan.pgm["name"], "lane": "-1",
            "offset": doc.t(src), "start": doc.t(plan.pgm_tc), "duration": doc.t(plan.pgm_dur),
            "srcEnable": "audio", "format": doc.fmt_pgm, "tcFormat": plan.tc_format})
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


def write_all(analysis, outdir, name=None, audio="pgm"):
    """Write NAME_multicam/_conform/_stacked.fcpxml into outdir. Returns (paths dict, plan)."""
    plan = Plan(analysis, name)
    if audio == "pgm" and not plan.pgm["has_audio"]:
        plan.warnings.append("PGM has no audio; the cut timelines will be silent")
    os.makedirs(os.path.abspath(outdir), exist_ok=True)
    base = os.path.join(os.path.abspath(outdir), safe_filename(plan.name))
    paths = {"multicam": base + "_multicam.fcpxml", "conform": base + "_conform.fcpxml",
             "stacked": base + "_stacked.fcpxml"}
    write(build_multicam(plan, audio), paths["multicam"])
    write(build_conform(plan), paths["conform"])
    write(build_stacked(plan), paths["stacked"])
    return paths, plan
