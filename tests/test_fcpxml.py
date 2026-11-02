import os
import sys
import xml.etree.ElementTree as ET
from fractions import Fraction

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "engine"))

from pgm2mc import fcpxml as F  # noqa: E402

DTD_PATH = os.path.join(HERE, "media", "FCPXMLv1_10.dtd")
TIME_ATTRS = ("offset", "start", "duration", "tcStart")


# ---------- shared checks (also used by test_cli.py) ----------

def parse_time(s):
    assert s.endswith("s"), s
    return Fraction(s[:-1])


def validate_dtd(path_or_text):
    if not os.path.exists(DTD_PATH):
        pytest.skip("FCPXML DTD not found at %s" % DTD_PATH)
    lxml = pytest.importorskip("lxml.etree")
    dtd = lxml.DTD(DTD_PATH)
    if os.path.exists(str(path_or_text)):
        doc = lxml.parse(str(path_or_text))
    else:
        doc = lxml.fromstring(path_or_text.encode("utf-8"))
    ok = dtd.validate(doc)
    assert ok, dtd.error_log.filter_from_errors()


def _contiguous(container, origin):
    pos = origin
    for child in container:
        assert parse_time(child.get("offset")) == pos, (child.tag, child.attrib, pos)
        pos += parse_time(child.get("duration"))
    return pos


def check_structure(root):
    """Frame alignment, contiguity and media-range checks for one generated document."""
    res = root.find("resources")
    fd = parse_time(res.find("format").get("frameDuration"))
    for el in root.iter():
        for attr in TIME_ATTRS:
            if el.get(attr) is not None:
                v = parse_time(el.get(attr))
                assert (v / fd).denominator == 1, (el.tag, attr, el.get(attr))
                if attr == "duration":
                    assert v > 0, (el.tag, el.attrib)
    for el in root.iter():
        for attr in TIME_ATTRS:
            assert "." not in (el.get(attr) or ""), el.attrib
    assets = dict((a.get("id"), a) for a in res.findall("asset"))
    for clip in root.iter("asset-clip"):
        a = assets[clip.get("ref")]
        a0, ad = parse_time(a.get("start")), parse_time(a.get("duration"))
        s, d = parse_time(clip.get("start")), parse_time(clip.get("duration"))
        assert a0 <= s and s + d <= a0 + ad, (clip.attrib, a.attrib)
    media = dict((m.get("id"), m) for m in res.findall("media"))
    angle_media = {}
    for m in media.values():
        mc = m.find("multicam")
        t0, dur = parse_time(mc.get("tcStart")), parse_time(mc.get("duration"))
        for ang in mc.findall("mc-angle"):
            end = _contiguous(ang, t0)
            assert end <= t0 + dur
            clip = ang.find("asset-clip")
            cs = parse_time(clip.get("offset"))
            angle_media[(m.get("id"), ang.get("angleID"))] = (cs, cs + parse_time(clip.get("duration")))
    for seq in root.iter("sequence"):
        spine = seq.find("spine")
        end = _contiguous(spine, parse_time(seq.get("tcStart")))
        assert end - parse_time(seq.get("tcStart")) == parse_time(seq.get("duration"))
        for clip in spine.findall("mc-clip"):
            mc = media[clip.get("ref")].find("multicam")
            t0, dur = parse_time(mc.get("tcStart")), parse_time(mc.get("duration"))
            s, d = parse_time(clip.get("start")), parse_time(clip.get("duration"))
            assert t0 <= s and s + d <= t0 + dur
            for src in clip.findall("mc-source"):
                lo, hi = angle_media[(clip.get("ref"), src.get("angleID"))]
                assert lo <= s and s + d <= hi, (clip.attrib, src.attrib, lo, hi)


def check_file(path):
    validate_dtd(path)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n')
    root = ET.fromstring(text.encode("utf-8"))
    check_structure(root)
    return root


def cut_boundaries(root):
    """[(start, end, angleID)] in PGM frames from the Multicam Cut spine."""
    seq = root.find("library/event/project/sequence")
    fd = parse_time(root.find("resources/format").get("frameDuration"))
    t0 = parse_time(seq.get("tcStart"))
    out = []
    for clip in seq.find("spine"):
        a = (parse_time(clip.get("offset")) - t0) / fd
        b = a + parse_time(clip.get("duration")) / fd
        srcs = clip.findall("mc-source")
        video = [s for s in srcs if s.get("srcEnable") in ("video", "all")][0]
        out.append((int(a), int(b), video.get("angleID")))
    return out


# ---------- hand-made analyses ----------

def media(path, tc, dur, w=1920, h=1080, audio=True):
    return {"path": path, "name": os.path.splitext(os.path.basename(path))[0], "start_tc_frames": tc,
            "duration_frames": dur, "width": w, "height": h, "has_audio": audio,
            "audio_rate": 48000 if audio else 0, "audio_channels": 2 if audio else 0}


def make_analysis(fps=(25, 1), pgm_tc=0, pgm_dur=1000, isos=(), segments=None, df=False):
    """isos: [(name, tc, dur, offset)]"""
    pgm = media("/shoot/PGM.mov", pgm_tc, pgm_dur, 1280, 720)
    pgm["id"] = "pgm"
    iso_list = []
    for i, (name, tc, dur, off) in enumerate(isos):
        d = media("/shoot/%s.mov" % name, tc, dur)
        d.update({"id": "a%d" % (i + 1), "offset_frames": off, "sync_method": "timecode",
                  "sync_confidence": 1.0})
        iso_list.append(d)
    if segments is None:
        n = len(iso_list) + 1
        step = pgm_dur // n
        segments = []
        for i in range(n):
            a, b = i * step, (pgm_dur if i == n - 1 else (i + 1) * step)
            segments.append({"start": a, "end": b, "angle": "pgm" if i == n - 1 else "a%d" % (i + 1),
                             "score": 0.9})
    return {"version": 1, "fps": {"num": fps[0], "den": fps[1]}, "drop_frame": df, "program": pgm,
            "isos": iso_list, "segments": segments, "warnings": []}


def seg(a, b, angle):
    return {"start": a, "end": b, "angle": angle, "score": 0.9}


def build_all(analysis, audio="pgm"):
    plan = F.Plan(analysis)
    docs = [F.build_multicam(plan, audio), F.build_conform(plan), F.build_stacked(plan)]
    texts = [F.to_string(d) for d in docs]
    for t in texts:
        validate_dtd(t)
        check_structure(ET.fromstring(t.encode("utf-8")))
    return plan, [ET.fromstring(t.encode("utf-8")) for t in texts]


# ---------- tests ----------

def test_rational():
    assert F.rational(0, Fraction(1, 25)) == "0s"
    assert F.rational(25, Fraction(1, 25)) == "1s"
    assert F.rational(262, Fraction(1, 25)) == "262/25s"
    assert F.rational(1, Fraction(1001, 30000)) == "1001/30000s"
    assert F.rational(30, Fraction(1001, 30000)) == "1001/1000s"
    assert F.rational(-5, Fraction(1, 25)) == "-1/5s"
    assert F.rational(107892, Fraction(1001, 30000)) == "8999991/2500s"


def test_file_url():
    assert F.file_url("/Users/ed/My Shoot/CAM1.mov") == "file:///Users/ed/My%20Shoot/CAM1.mov"
    assert F.file_url("/Volumes/X/Caf\u00e9 #2/50%.mov") == "file:///Volumes/X/Caf%C3%A9%20%232/50%25.mov"
    assert F.file_url("/a/b&c?.mov") == "file:///a/b%26c%3F.mov"
    assert F.file_url("C:\\Shoot\\My Clip #1.mov") == "file:///C:/Shoot/My%20Clip%20%231.mov"
    assert F.file_url("d:/x/\u00e9.mov") == "file:///D:/x/%C3%A9.mov"
    assert F.file_url("\\\\server\\share\\a b.mov") == "file://server/share/a%20b.mov"


def test_matches_docs_sample():
    a = make_analysis(pgm_tc=90025, pgm_dur=1375,
                      isos=[("CAM1", 90000, 1500, 25), ("CAM2", 90050, 1450, -25)],
                      segments=[seg(0, 262, "a1"), seg(262, 762, "a2"), seg(762, 1375, "pgm")])
    plan, (mc, conform, stacked) = build_all(a)
    m = mc.find("resources/media/multicam")
    assert (m.get("tcStart"), m.get("duration")) == ("3600s", "60s")
    gaps = [ang.find("gap") for ang in m.findall("mc-angle")]
    assert gaps[0] is None and gaps[1].get("duration") == "2s" and gaps[2].get("duration") == "1s"
    clips = mc.findall("library/event/project/sequence/spine/mc-clip")
    assert [c.get("start") for c in clips] == ["3601s", "90287/25s", "90787/25s"]
    assert [c.get("offset") for c in clips] == ["3601s", "90287/25s", "90787/25s"]
    assert [(s.get("angleID"), s.get("srcEnable")) for s in clips[0]] == [("a1", "video"), ("pgm", "audio")]
    assert [(s.get("angleID"), s.get("srcEnable")) for s in clips[2]] == [("pgm", "all")]
    assert [ang.get("name") for ang in m.findall("mc-angle")] == ["CAM1", "CAM2", "PGM"]
    assert mc.find("library/event/project").get("name") == "PGM Multicam Cut"
    assert conform.find("library/event/project").get("name") == "PGM Conform"
    assert stacked.find("library/event/project").get("name") == "PGM Stacked"
    # conform: ISO source = ISO TC + p + offset; PGM audio connected below for the whole programme
    spine = conform.find("library/event/project/sequence/spine")
    cs = spine.findall("asset-clip")
    assert cs[0].get("start") == F.rational(90000 + 0 + 25, Fraction(1, 25))
    assert cs[1].get("start") == F.rational(90050 + 262 - 25, Fraction(1, 25))
    assert cs[2].get("start") == F.rational(90025 + 762, Fraction(1, 25))
    assert all(c.get("srcEnable") == "video" for c in cs)
    aud = cs[0].find("asset-clip")
    assert (aud.get("lane"), aud.get("srcEnable"), aud.get("duration")) == ("-1", "audio", "55s")
    assert aud.get("offset") == cs[0].get("start")
    gap = stacked.find("library/event/project/sequence/spine/gap")
    assert [c.get("offset") for c in gap] == ["3600s", "3602s", "3601s"]
    assert [c.get("lane") for c in gap] == ["1", "2", "3"]


def test_audio_follow():
    a = make_analysis(isos=[("C1", 0, 1000, 0)])
    _, (mc, _, _) = build_all(a, audio="follow")
    for clip in mc.iter("mc-clip"):
        srcs = clip.findall("mc-source")
        assert len(srcs) == 1 and srcs[0].get("srcEnable") == "all"


def test_merge_adjacent_same_angle():
    a = make_analysis(isos=[("C1", 0, 1000, 0), ("C2", 0, 1000, 0)],
                      segments=[seg(0, 100, "a1"), seg(100, 200, "a1"), seg(200, 300, "pgm"),
                                seg(300, 400, "pgm"), seg(400, 1000, "a2")])
    plan, (mc, _, _) = build_all(a)
    assert cut_boundaries(mc) == [(0, 200, "a1"), (200, 400, "pgm"), (400, 1000, "a2")]
    assert len(plan.segments) == 3


def test_short_iso_falls_back_to_pgm():
    # C1 covers PGM frames 0..600 only; C2 starts at PGM frame 300 (negative offset)
    a = make_analysis(isos=[("C1", 0, 600, 0), ("C2", 0, 900, -300)],
                      segments=[seg(0, 500, "a1"), seg(500, 700, "a1"), seg(700, 800, "a2"),
                                seg(800, 1000, "a2")])
    plan, (mc, conform, _) = build_all(a)
    assert cut_boundaries(mc) == [(0, 600, "a1"), (600, 700, "pgm"), (700, 1000, "a2")]
    assert any("C1" in w and "PGM" in w for w in plan.warnings)
    a = make_analysis(isos=[("C1", 0, 900, -300)], segments=[seg(0, 100, "a1"), seg(100, 1000, "pgm")])
    plan, (mc, _, _) = build_all(a)
    assert cut_boundaries(mc) == [(0, 1000, "pgm")]
    assert plan.warnings


def test_negative_offsets_and_no_timecode():
    # PGM without TC, ISO started 50 frames before the PGM: origin would be negative -> clamp tcStart to 0
    a = make_analysis(pgm_tc=0, isos=[("C1", 0, 1100, 50), ("C2", 0, 950, -40)],
                      segments=[seg(0, 30, "a1"), seg(30, 500, "pgm"), seg(500, 990, "a2"), seg(990, 1000, "pgm")])
    plan, (mc, _, stacked) = build_all(a)
    m = mc.find("resources/media/multicam")
    assert m.get("tcStart") == "0s" and plan.mc_len == 1100
    first = mc.find("library/event/project/sequence/spine/mc-clip")
    assert first.get("offset") == "0s" and first.get("start") == "2s"
    seq = stacked.find("library/event/project/sequence")
    assert seq.get("tcStart") == "0s" and seq.get("duration") == "44s"


def test_drop_frame():
    fd = Fraction(1001, 30000)
    a = make_analysis(fps=(30000, 1001), pgm_tc=107892, df=True, isos=[("C1", 107800, 1200, 92)])
    plan, docs = build_all(a)
    for root in docs:
        for el in root.iter():
            if el.tag in ("sequence", "multicam", "asset-clip"):
                assert el.get("tcFormat") == "DF"
    assert docs[0].find("resources/format").get("frameDuration") == "1001/30000s"
    assert docs[0].find("resources/asset").get("start") == F.rational(107800, fd)
    plan = F.Plan(make_analysis(fps=(25, 1), df=True, isos=[("C1", 0, 1000, 0)]))
    assert plan.tc_format == "NDF" and plan.warnings


@pytest.mark.parametrize("fps", [(25, 1), (30000, 1001), (24000, 1001), (50, 1), (60000, 1001), (24, 1)])
@pytest.mark.parametrize("n_iso", [1, 9])
def test_rates_and_angle_counts(fps, n_iso):
    isos = [("CAM %d" % (i + 1), 3600 + 7 * i, 1000 + 13 * i, 11 * i - 40) for i in range(n_iso)]
    plan, (mc, conform, stacked) = build_all(make_analysis(fps=fps, pgm_tc=3640, isos=isos))
    m = mc.find("resources/media/multicam")
    assert len(m.findall("mc-angle")) == n_iso + 1
    assert m.findall("mc-angle")[-1].get("angleID") == "pgm"
    assert len(stacked.find("library/event/project/sequence/spine/gap")) == n_iso + 1
    expect = [(s["start"], s["end"], s["angle"]) for s in plan.segments]
    assert cut_boundaries(mc) == expect
    assert len(conform.find("library/event/project/sequence/spine")) == len(expect)


def test_formats_dedup_and_assets():
    a = make_analysis(isos=[("C1", 0, 1000, 0), ("C2", 0, 1000, 0)])
    a["isos"][1].update({"width": 3840, "height": 2160, "has_audio": False})
    _, (mc, _, _) = build_all(a)
    fmts = mc.findall("resources/format")
    assert [(f.get("width"), f.get("height")) for f in fmts] == [("1280", "720"), ("1920", "1080"),
                                                                  ("3840", "2160")]
    assets = mc.findall("resources/asset")
    assert assets[1].get("hasAudio") == "0" and assets[1].get("audioRate") is None
    assert assets[0].get("audioChannels") == "2" and assets[0].get("audioRate") == "48000"
    assert assets[0].find("media-rep").get("src") == "file:///shoot/C1.mov"


def test_bad_segments_raise():
    a = make_analysis(isos=[("C1", 0, 1000, 0)], segments=[seg(0, 100, "a1"), seg(120, 1000, "pgm")])
    with pytest.raises(F.FcpxmlError):
        F.Plan(a)
    a = make_analysis(isos=[("C1", 0, 1000, 0)], segments=[seg(0, 100, "zz"), seg(100, 1000, "pgm")])
    with pytest.raises(F.FcpxmlError):
        F.Plan(a)
    a = make_analysis(isos=[("C1", 0, 1000, 0)], segments=[seg(0, 900, "a1")])
    with pytest.raises(F.FcpxmlError):
        F.Plan(a)


def test_write_all_unicode(tmp_path):
    a = make_analysis(isos=[("C1", 0, 1000, 0)])
    a["isos"][0]["path"] = "/shoot/Caf\u00e9 & <cam>.mov"
    a["isos"][0]["name"] = "Caf\u00e9 & <cam>"
    paths, plan = F.write_all(a, str(tmp_path), "Show: 1/2")
    for p in paths.values():
        assert os.path.basename(p).startswith("Show_ 1_2_")
        root = check_file(p)
        assert root.find("library/event/project").get("name").startswith("Show: 1/2 ")
    root = ET.parse(paths["multicam"]).getroot()
    assert root.find("resources/asset").get("src") is None
    assert root.find("resources/asset/media-rep").get("src") == "file:///shoot/Caf%C3%A9%20%26%20%3Ccam%3E.mov"


def test_dtd_negative_control():
    a = make_analysis(isos=[("C1", 0, 1000, 0)])
    root = F.build_multicam(F.Plan(a))
    root.find("resources/asset").set("src", "file:///x.mov")
    with pytest.raises(AssertionError):
        validate_dtd(F.to_string(root))
