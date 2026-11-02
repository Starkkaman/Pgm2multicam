"""FCPXML v2 writer: transitions, dips, stills and SuperSource layers from hand-made v2 analyses."""
import os
import struct
import sys
import xml.etree.ElementTree as ET
import zlib
from fractions import Fraction

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "engine"))
sys.path.insert(0, HERE)

from pgm2mc import fcpxml as F  # noqa: E402
from test_fcpxml import check_file, check_structure, cut_boundaries, media, parse_time, validate_dtd  # noqa: E402

FD = Fraction(1, 25)
SPINE = "library/event/project/sequence/spine"


def t(frames):
    return F.rational(frames, FD)


def seg(a, b, angle="pgm", kind=None, **extra):
    d = {"start": a, "end": b, "angle": angle, "score": 0.9,
         "kind": kind or ("pgm" if angle == "pgm" else "camera")}
    d.update(extra)
    return d


def v2_analysis(segments, transitions=(), isos=None, stills=(), supersources=(), pgm_dur=None, pgm_tc=90000,
                pgm_size=(1920, 1080)):
    """isos: [(name, tc, dur, offset, w, h)]"""
    pgm = media("/shoot/PGM.mov", pgm_tc, pgm_dur or segments[-1]["end"], *pgm_size)
    pgm["id"] = "pgm"
    iso_list = []
    for i, (name, tc, dur, off, w, h) in enumerate(isos or [("CAM1", 90000, 1500, 0, 3840, 2160),
                                                             ("CAM2", 90000, 1500, 0, 3840, 2160)]):
        d = media("/shoot/%s.mov" % name, tc, dur, w, h)
        d.update({"id": "a%d" % (i + 1), "offset_frames": off, "sync_method": "timecode", "sync_confidence": 1.0})
        iso_list.append(d)
    return {"version": 2, "fps": {"num": 25, "den": 1}, "drop_frame": False, "program": pgm, "isos": iso_list,
            "segments": list(segments), "transitions": list(transitions), "stills": list(stills),
            "supersources": list(supersources), "warnings": []}


def still(sid, role="still"):
    return {"id": sid, "path": "/out/PGM_stills/%s.png" % sid, "width": 1920, "height": 1080,
            "occurrences": [], "role": role}


def build(analysis, audio="pgm", assets_dir="/out/PGM_stills"):
    plan = F.Plan(analysis, assets_dir=assets_dir)
    docs = [F.build_multicam(plan, audio), F.build_conform(plan), F.build_stacked(plan)]
    roots = []
    for d in docs:
        text = F.to_string(d)
        validate_dtd(text)
        root = ET.fromstring(text.encode("utf-8"))
        check_structure(root)
        roots.append(root)
    return plan, roots


# Worked example of docs/sample_v2.fcpxml: CAM1 -> 1 s dissolve -> CAM2, still s1, SuperSource, CAM1 -> dip -> CAM2.
SS1 = {"id": "ss1", "background": "s2", "animated": False, "confidence": 0.9, "occurrences": [[625, 875]],
       "boxes": [{"angle": "a1", "layer": 1, "rect": [0.07, 0.275, 0.43, 0.725], "crop": [0.1, 0.0, 0.1, 0.0]},
                 {"angle": "a2", "layer": 2, "rect": [0.57, 0.275, 0.93, 0.725], "crop": [0.1, 0.0, 0.1, 0.0]}]}
SAMPLE_SEGS = [seg(0, 250, "a1"), seg(250, 500, "a2"), seg(500, 625, kind="still", still="s1"),
               seg(625, 875, kind="supersource", supersource="ss1"), seg(875, 1000, "a1"), seg(1000, 1125, "a2")]
SAMPLE_TRANS = [{"edit": 250, "duration": 25, "kind": "dissolve", "confidence": 0.9},
                {"edit": 1000, "duration": 30, "kind": "dip", "color": [0, 0, 0], "confidence": 0.8}]


def sample_analysis():
    return v2_analysis(SAMPLE_SEGS, SAMPLE_TRANS, stills=[still("s1"), still("s2", "background")],
                       supersources=[SS1])


def test_box_params_worked_example():
    left = F.box_params([0.07, 0.275, 0.43, 0.725], [0.1, 0, 0.1, 0], 3840, 2160, 1920, 1080)
    fx, rv = left["fcpxml"], left["resolve"]
    assert F._num(fx["position"][0]) == "-44.4444" and F._num(fx["position"][1]) == "0"
    assert [F._num(v) for v in fx["scale"]] == ["0.45", "0.45"]
    assert F._num(fx["trim"]["left"]) == "17.7778" and F._num(fx["trim"]["right"]) == "17.7778"
    assert rv["Pan"] == pytest.approx(-480) and rv["Tilt"] == pytest.approx(0) and rv["ZoomX"] == pytest.approx(0.45)
    assert rv["CropLeft"] == pytest.approx(192) and rv["Scaling"] == 2
    assert F._num(-0.00001) == "0" and F._num(12.5) == "12.5" and F._num(3.0) == "3"


def test_transition_and_dip_layout_helpers():
    assert F.transition_place(250, 25) == (238, 25, 13, 12)
    lay = F.dip_layout(1000, 30)
    assert lay == {"a_out": 992, "slug": (992, 1007), "slug_start": 7, "t1": (985, 15), "t2": (1000, 15),
                   "b_in": 1007}


def test_matches_docs_sample_v2():
    plan, (mc, conform, stacked) = build(sample_analysis())
    assert not plan.warnings
    sample = ET.parse(os.path.join(os.path.dirname(HERE), "docs", "sample_v2.fcpxml")).getroot()
    want = list(sample.find(SPINE))
    got = list(mc.find(SPINE))
    assert [e.tag for e in got] == [e.tag for e in want]
    for g, w in zip(got, want):
        for k in ("offset", "start", "duration"):
            assert g.get(k) == w.get(k), (g.tag, k, g.attrib, w.attrib)
        gk, wk = list(g), list(w)
        assert [(c.tag, c.get("lane")) for c in gk] == [(c.tag, c.get("lane")) for c in wk]
        for gc, wc in zip(gk, wk):
            if gc.tag in ("video", "asset-clip"):
                for k in ("offset", "start", "duration", "srcEnable"):
                    assert gc.get(k) == wc.get(k), (gc.attrib, wc.attrib)
                for sub in ("adjust-crop/trim-rect", "adjust-conform", "adjust-transform"):
                    if wc.find(sub) is not None:
                        assert gc.find(sub).attrib == wc.find(sub).attrib, sub
            elif gc.tag == "filter-video":
                assert gc.get("name") == "Cross Dissolve"
    eff = mc.find("resources/effect")
    assert (eff.get("name"), eff.get("uid")) == ("Cross Dissolve", "FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265")
    trs = mc.findall(SPINE + "/transition")
    assert all(tr.get("name") == "Cross Dissolve" and tr.find("filter-video").get("ref") == eff.get("id")
               for tr in trs)
    # names: boxes "<ISO> box", background "<NAME> SuperSource BG"
    ss_clip = got[4]
    assert [c.get("name") for c in ss_clip if c.get("lane")] == ["PGM SuperSource BG", "CAM1 box", "CAM2 box"]
    # still assets: FFVideoFormatRateUndefined without frameDuration, 0s duration, used as <video>
    fmts = dict((f.get("id"), f) for f in mc.findall("resources/format"))
    stills = [a for a in mc.findall("resources/asset") if a.get("duration") == "0s"]
    assert sorted(a.get("name") for a in stills) == ["dip_000000", "s1", "s2"]
    for a in stills:
        f = fmts[a.get("format")]
        assert f.get("name") == "FFVideoFormatRateUndefined" and f.get("frameDuration") is None
        assert f.get("colorSpace") == "1-13-1" and a.get("hasAudio") is None
    assert stills[0].find("media-rep").get("src") == "file:///out/PGM_stills/s1.png"
    slug = got[7]
    assert slug.get("name") == "dip to black"
    assert mc.find("resources/asset[@id='%s']/media-rep" % slug.get("ref")).get("src") == \
        "file:///out/PGM_stills/dip_000000.png"
    # stacked timeline is unchanged by v2
    assert stacked.find("resources/effect") is None and len(stacked.find(SPINE + "/gap")) == 3
    assert F.summary(plan) == {"transitions": 2, "supersources": 1, "stills": 2}


def test_conform_carries_the_same_v2_layers():
    plan, (mc, conform, _) = build(sample_analysis())
    cs = list(conform.find(SPINE))
    ms = list(mc.find(SPINE))
    assert [e.tag for e in cs] == ["asset-clip" if e.tag == "mc-clip" else e.tag for e in ms]
    for c, m in zip(cs, ms):
        assert (c.get("offset"), c.get("duration")) == (m.get("offset"), m.get("duration"))
    # still lane: offset in the PGM clip's local time (PGM source time)
    still_clip = cs[3]
    assert still_clip.get("start") == t(90000 + 500)
    v = still_clip.find("video")
    assert (v.get("lane"), v.get("offset"), v.get("duration"), v.get("start")) == ("1", t(90500), t(125), "0s")
    # boxes: ISO start = ISO TC + p + offset
    ss_clip = cs[4]
    boxes = ss_clip.findall("asset-clip")
    assert [b.get("lane") for b in boxes] == ["2", "3"]
    assert all(b.get("start") == t(90000 + 625) and b.get("srcEnable") == "video" for b in boxes)
    assert boxes[1].find("adjust-transform").get("position") == "44.4444 0"
    # PGM audio stays anchored below the first clip for the whole programme
    aud = cs[0].find("asset-clip")
    assert aud.get("lane") == "-1" and aud.get("duration") == t(1125)


def test_dissolve_offsets_odd_and_even():
    for dur, pre in ((24, 12), (25, 12), (2, 1), (7, 3)):
        a = v2_analysis([seg(0, 100, "a1"), seg(100, 200, "a2")],
                        [{"edit": 100, "duration": dur, "kind": "dissolve"}])
        plan, (mc, conform, _) = build(a)
        assert not plan.warnings
        for root in (mc, conform):
            tr = root.find(SPINE + "/transition")
            assert parse_time(tr.get("offset")) == (90000 + 100 - pre) * FD
            assert parse_time(tr.get("duration")) == dur * FD
        assert cut_boundaries(mc) == [(0, 100, "a1"), (100, 200, "a2")]


def test_dissolve_clamped_by_iso_media():
    # CAM2 media starts at PGM frame 95: only 5 frames of pre-handle for the incoming clip
    a = v2_analysis([seg(0, 100, "a1"), seg(100, 200, "a2")], [{"edit": 100, "duration": 25, "kind": "dissolve"}],
                    isos=[("CAM1", 90000, 1000, 0, 1920, 1080), ("CAM2", 90000, 1000, -95, 1920, 1080)])
    plan, (mc, conform, _) = build(a)
    assert any("shortened from 25 to 10" in w for w in plan.warnings)
    for root in (mc, conform):
        tr = root.find(SPINE + "/transition")
        assert (tr.get("offset"), tr.get("duration")) == (t(90095), t(10))
    # CAM1 ends its media at the cut: no post handle -> dropped
    a = v2_analysis([seg(0, 100, "a1"), seg(100, 200, "a2")], [{"edit": 100, "duration": 25, "kind": "dissolve"}],
                    isos=[("CAM1", 90000, 100, 0, 1920, 1080), ("CAM2", 90000, 1000, 0, 1920, 1080)])
    plan, (mc, _, _) = build(a)
    assert mc.find(SPINE + "/transition") is None and any("dropped" in w for w in plan.warnings)


def test_dissolve_clamped_by_clip_length_and_neighbours():
    # B is only 8 frames long before the next 10-frame dissolve takes 5 of them
    a = v2_analysis([seg(0, 100, "a1"), seg(100, 108, "a2"), seg(108, 300, "a1")],
                    [{"edit": 100, "duration": 20, "kind": "dissolve"},
                     {"edit": 108, "duration": 10, "kind": "dissolve"}])
    plan, (mc, _, _) = build(a)
    trs = mc.findall(SPINE + "/transition")
    assert [(tr.get("offset"), tr.get("duration")) for tr in trs] == [(t(90097), t(6)), (t(90103), t(10))]
    assert any("shortened from 20 to 6" in w for w in plan.warnings)


def test_dip_slug_png_and_colour(tmp_path):
    a = v2_analysis([seg(0, 100, "a1"), seg(100, 200, "a2")],
                    [{"edit": 100, "duration": 21, "kind": "dip", "color": [16, 32, 255]}],
                    pgm_size=(64, 36))
    paths, plan = F.write_all(a, str(tmp_path), "Show")
    assert not plan.warnings
    png = tmp_path / "Show_stills" / "dip_1020FF.png"
    data = png.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", data[16:24])
    assert (w, h) == (64, 36)
    idat = data.index(b"IDAT")
    n = struct.unpack(">I", data[idat - 4:idat])[0]
    raw = zlib.decompress(data[idat + 4:idat + 4 + n])
    assert raw[:4] == b"\x00\x10\x20\xff" and len(raw) == (1 + 64 * 3) * 36
    mc = check_file(paths["multicam"])
    check_file(paths["conform"])
    check_file(paths["stacked"])
    kids = list(mc.find(SPINE))
    assert [k.tag for k in kids] == ["mc-clip", "transition", "video", "transition", "mc-clip"]
    lay = F.dip_layout(100, 21)
    assert lay["t1"] == (90, 10) and lay["t2"] == (100, 11) and lay["slug"] == (95, 105)
    assert kids[2].get("name") == "dip to #1020FF"
    assert (kids[2].get("offset"), kids[2].get("start"), kids[2].get("duration")) == (t(90095), t(5), t(10))
    assert (kids[1].get("offset"), kids[1].get("duration")) == (t(90090), t(10))
    assert (kids[3].get("offset"), kids[3].get("duration")) == (t(90100), t(11))
    assert kids[0].get("duration") == t(95) and kids[4].get("offset") == t(90105)


def test_transition_touching_still_is_not_rebuilt():
    a = v2_analysis([seg(0, 250, "a1"), seg(250, 400, kind="still", still="s1"), seg(400, 600, "a2")],
                    [{"edit": 250, "duration": 20, "kind": "dissolve"}, {"edit": 400, "duration": 9, "kind": "dip",
                                                                          "color": [0, 0, 0]}],
                    stills=[still("s1")])
    plan, (mc, conform, _) = build(a)
    assert not plan.warnings
    assert cut_boundaries(mc) == [(0, 240, "a1"), (240, 405, "pgm"), (405, 600, "a2")]
    for root in (mc, conform):
        spine = root.find(SPINE)
        assert spine.find("transition") is None and spine.find("video") is None
        clip = list(spine)[1]
        v = clip.find("video")
        local0 = parse_time(clip.get("start"))
        assert parse_time(v.get("offset")) == local0 + 20 * FD and v.get("duration") == t(136)
        marks = clip.findall("marker")
        assert [m.get("value") for m in marks] == ["PGM transition", "PGM transition"]
        assert [parse_time(m.get("start")) for m in marks] == [local0 + 10 * FD, local0 + 160 * FD]
    assert root.find("resources/effect") is None and F.summary(plan)["transitions"] == 2


def test_transition_between_still_and_supersource():
    ss = dict(SS1, occurrences=[[200, 400]])
    a = v2_analysis([seg(0, 200, kind="still", still="s1"), seg(200, 400, kind="supersource", supersource="ss1"),
                     seg(400, 500, "a1")],
                    [{"edit": 200, "duration": 10, "kind": "dissolve"}], stills=[still("s1"), still("s2")],
                    supersources=[ss])
    plan, (mc, _, _) = build(a)
    kids = list(mc.find(SPINE))
    assert [(k.get("offset"), k.get("duration")) for k in kids] == [(t(90000), t(200)), (t(90200), t(200)),
                                                                   (t(90400), t(100))]
    assert kids[0].find("video").get("duration") == t(195)
    lanes = [c for c in kids[1] if c.get("lane")]
    assert all(c.get("offset") == F.rational(parse_time(kids[1].get("start")) / FD + 5, FD) for c in lanes)
    assert kids[1].find("marker") is not None and kids[0].find("marker") is None


def test_supersource_iso_size_differs_from_sequence():
    ss = {"id": "ss1", "background": None, "animated": False, "occurrences": [[100, 300]],
          "boxes": [{"angle": "a1", "layer": 1, "rect": [0.1, 0.1, 0.5, 0.6], "crop": [0.05, 0.1, 0.05, 0.0]}]}
    a = v2_analysis([seg(0, 100, "a1"), seg(100, 300, kind="supersource", supersource="ss1")],
                    isos=[("CAM1", 1000, 900, 40, 1280, 960)], supersources=[ss])
    plan, (mc, conform, _) = build(a)
    # fit = min(1920/1280, 1080/960) = 1.125 -> fitted 1440x1080; sx = 0.4*1920/(1440*0.9), sy = 0.5/0.9
    for root in (mc, conform):
        box = list(root.find(SPINE))[1].find("asset-clip")
        assert box.get("lane") == "2" and box.get("name") == "CAM1 box"
        assert box.get("start") == t(1000 + 100 + 40)
        assert box.find("adjust-transform").attrib == {"position": "-35.5556 17.7778", "scale": "0.5926 0.5556"}
        assert box.find("adjust-crop").get("mode") == "trim"
        assert box.find("adjust-crop/trim-rect").attrib == {"left": "6.6667", "top": "10", "right": "6.6667",
                                                            "bottom": "0"}
        assert box.find("adjust-conform").get("type") == "fit"
    clip = list(mc.find(SPINE))[1]
    assert clip.find("video") is None and clip.get("name") == "PGM (SuperSource ss1)"


def test_supersource_box_without_media_and_layer_order():
    ss = {"id": "ss1", "background": "s2", "animated": True, "occurrences": [[100, 300]],
          "boxes": [{"angle": "a2", "layer": 2, "rect": [0.5, 0, 1, 0.5], "crop": [0, 0, 0, 0]},
                    {"angle": "a1", "layer": 1, "rect": [0, 0, 0.5, 0.5], "crop": [0, 0, 0, 0]},
                    {"angle": "zz", "layer": 3, "rect": [0, 0, 0.5, 0.5], "crop": [0, 0, 0, 0]}]}
    a = v2_analysis([seg(0, 100, "a1"), seg(100, 300, kind="supersource", supersource="ss1")],
                    isos=[("CAM1", 0, 300, 0, 1920, 1080), ("CAM2", 0, 200, 0, 1920, 1080)],
                    stills=[still("s2", "background")], supersources=[ss])
    plan, (mc, _, _) = build(a)
    clip = list(mc.find(SPINE))[1]
    lanes = [(c.tag, c.get("lane"), c.get("name"), c.get("duration")) for c in clip if c.get("lane")]
    assert lanes == [("video", "1", "PGM SuperSource BG", t(200)), ("asset-clip", "2", "CAM1 box", t(200)),
                     ("asset-clip", "3", "CAM2 box", t(100))]
    assert any("animated" in w for w in plan.warnings) and any("box 3" in w for w in plan.warnings)
    assert any("CAM2 covers only" in w for w in plan.warnings)


def test_unknown_still_reference_falls_back_to_pgm():
    a = v2_analysis([seg(0, 100, "a1"), seg(100, 200, kind="still", still="nope")])
    plan, (mc, _, _) = build(a)
    assert cut_boundaries(mc) == [(0, 100, "a1"), (100, 200, "pgm")]
    assert mc.find(SPINE + "/mc-clip/video") is None and plan.warnings


def test_transition_off_cut_dropped_and_v1_ignores_v2_fields():
    a = v2_analysis([seg(0, 100, "a1"), seg(100, 200, "a2")], [{"edit": 150, "duration": 10, "kind": "dissolve"}])
    plan, (mc, _, _) = build(a)
    assert mc.find(SPINE + "/transition") is None and any("not on a cut" in w for w in plan.warnings)
    a = v2_analysis([seg(0, 100, "a1"), seg(100, 200, "a2")], [{"edit": 100, "duration": 10, "kind": "dissolve"}])
    a["version"] = 1
    plan, (mc, _, _) = build(a)
    assert mc.find(SPINE + "/transition") is None and mc.find("resources/effect") is None
    assert not plan.warnings and F.summary(plan) == {"transitions": 0, "supersources": 0, "stills": 0}


def test_missing_iso_media_splits_keep_transitions_safe():
    # CAM1 media ends at PGM frame 90 inside its segment: 90-100 becomes PGM, the dissolve sits PGM -> CAM2
    a = v2_analysis([seg(0, 100, "a1"), seg(100, 200, "a2")], [{"edit": 100, "duration": 10, "kind": "dissolve"}],
                    isos=[("CAM1", 0, 90, 0, 1920, 1080), ("CAM2", 0, 300, 0, 1920, 1080)])
    plan, (mc, _, _) = build(a)
    assert cut_boundaries(mc) == [(0, 90, "a1"), (90, 100, "pgm"), (100, 200, "a2")]
    assert mc.find(SPINE + "/transition").get("duration") == t(10)
