import json
import os
import re
import subprocess
import sys
import types
from fractions import Fraction

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(os.path.dirname(HERE), "engine")
sys.path.insert(0, ENGINE)
sys.path.insert(0, HERE)

import gen_media  # noqa: E402
from pgm2mc import match, probe as P, sync as S  # noqa: E402
from pgm2mc.analyze import analyze  # noqa: E402
from pgm2mc.probe import EngineError  # noqa: E402

CAMS = ["cam1", "cam2", "cam3"]
CAM_ID = {"cam1": "a1", "cam2": "a2", "cam3": "a3", "ext": "pgm"}


@pytest.fixture(scope="session")
def ffmpeg():
    exe = gen_media.ffmpeg_exe()
    os.environ["PGM2MC_FFMPEG"] = exe
    return exe


@pytest.fixture(scope="session")
def media(ffmpeg):
    out = {}
    for name in gen_media.SCENARIOS:
        with open(gen_media.generate(name)) as f:
            out[name] = json.load(f)
    return out


def iso_paths(truth):
    return [truth["isos"][c]["path"] for c in CAMS]


@pytest.fixture(scope="session")
def results(media, ffmpeg, tmp_path_factory):
    return {name: analyze(t["pgm"]["path"], iso_paths(t), ffmpeg=ffmpeg, progress=None,
                          assets_dir=str(tmp_path_factory.mktemp("assets%s" % name)))
            for name, t in media.items()}


def frame_labels(segments, n):
    lab = np.empty(n, dtype=object)
    for s in segments:
        lab[s["start"]:s["end"]] = s["angle"]
    return lab


def truth_labels(truth):
    return frame_labels([{"start": s, "end": e, "angle": CAM_ID[src]} for s, e, src in truth["segments"]],
                        truth["pgm_frames"])


def check_cut_accuracy(result, truth):
    n = truth["pgm_frames"]
    segs = result["segments"]
    assert result["program"]["duration_frames"] == n
    assert segs[0]["start"] == 0 and segs[-1]["end"] == n
    for a, b in zip(segs, segs[1:]):
        assert a["end"] == b["start"] and a["angle"] != b["angle"]
    for s in segs:
        assert 0.0 <= s["score"] <= 1.0
    got, want = frame_labels(segs, n), truth_labels(truth)
    acc = float(np.mean(got == want))
    true_cuts = [s for s, _, _ in truth["segments"][1:]]
    found_cuts = [s["start"] for s in segs[1:]]
    assert len(found_cuts) == len(true_cuts), (found_cuts, true_cuts)
    for t, f in zip(true_cuts, found_cuts):
        assert abs(t - f) <= 1, (t, f)
    ext = [(s, e) for s, e, src in truth["segments"] if src == "ext"]
    for s, e in ext:
        assert np.all(got[s + 1:e - 1] == "pgm")
    assert acc >= 0.98
    return acc


# ---------------------------------------------------------------- probe

def test_tc_to_frames():
    assert P.tc_to_frames("01:00:00:00", Fraction(25)) == 90000
    assert P.tc_to_frames("00:00:01:05", Fraction(30000, 1001)) == 35
    assert P.tc_to_frames("00:01:00;02", Fraction(30000, 1001)) == 1800
    assert P.tc_to_frames("00:10:00;00", Fraction(30000, 1001)) == 17982
    assert P.tc_to_frames("01:00:00;00", Fraction(30000, 1001)) == 107892
    assert P.tc_to_frames("01:00:00;00", Fraction(60000, 1001)) == 215784
    assert P.frames_per_day(Fraction(25), False) == 2160000
    with pytest.raises(EngineError):
        P.tc_to_frames("garbage", Fraction(25))


FFPROBE_MOV_DF = {
    "streams": [
        {"index": 0, "codec_name": "prores", "codec_type": "video", "width": 1920, "height": 1080,
         "r_frame_rate": "30000/1001", "avg_frame_rate": "30000/1001", "time_base": "1/30000",
         "duration": "3600.033367", "nb_frames": "107893",
         "disposition": {"default": 1, "attached_pic": 0},
         "tags": {"language": "eng", "handler_name": "Core Media Video", "timecode": "01:00:00;00"}},
        {"index": 1, "codec_name": "pcm_s24le", "codec_type": "audio", "sample_rate": "48000", "channels": 2,
         "channel_layout": "stereo", "duration": "3600.033354"},
        {"index": 2, "codec_type": "data", "codec_tag_string": "tmcd", "tags": {"timecode": "01:00:00;00"}},
    ],
    "format": {"filename": "/x/CAM1.mov", "nb_streams": 3, "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
               "duration": "3600.033367", "tags": {"major_brand": "qt  "}},
}

FFPROBE_MXF_FORMAT_TC = {
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 3840, "height": 2160,
         "r_frame_rate": "25/1", "avg_frame_rate": "25/1", "duration": "120.000000"},
        {"index": 1, "codec_type": "audio", "sample_rate": "48000", "channels": 4},
    ],
    "format": {"duration": "120.040000", "tags": {"timecode": "10:00:00:12"}},
}

FFPROBE_MP4_ODD_RATE = {
    "streams": [
        {"index": 0, "codec_type": "video", "width": 1280, "height": 720, "r_frame_rate": "60000/1001",
         "avg_frame_rate": "1798200/60001", "tags": {}},
    ],
    "format": {"duration": "10.010000"},
}


def test_ffprobe_json_mov_drop_frame():
    info = P.parse_ffprobe_json(json.dumps(FFPROBE_MOV_DF), "/x/CAM1.mov")
    assert info.fps == Fraction(30000, 1001)
    assert info.duration_frames == 107893
    assert info.start_tc == "01:00:00;00" and info.drop_frame and info.start_tc_frames == 107892
    assert (info.width, info.height) == (1920, 1080)
    assert info.has_audio and info.audio_rate == 48000 and info.audio_channels == 2
    assert info.name == "CAM1"


def test_ffprobe_json_format_timecode():
    info = P.parse_ffprobe_json(FFPROBE_MXF_FORMAT_TC, "/x/B.mxf")
    assert info.fps == 25 and info.duration_frames == 3000
    assert info.start_tc_frames == 900012 and not info.drop_frame
    assert info.audio_channels == 4


def test_ffprobe_json_odd_avg_rate_no_audio():
    info = P.parse_ffprobe_json(FFPROBE_MP4_ODD_RATE, "/x/c.mp4")
    assert info.fps == Fraction(30000, 1001)
    assert info.duration_frames == 300
    assert not info.has_audio and not info.has_timecode and info.start_tc_frames == 0


STDERR_2398 = """Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'x.mov':
  Metadata:
    major_brand     : qt
  Duration: 00:01:00.06, start: 0.000000, bitrate: 2256 kb/s
  Stream #0:0[0x1](eng): Video: prores (HQ) (apch / 0x68637061), yuv422p10le(tv, bt709, progressive), 3840x2160, 700000 kb/s, SAR 1:1 DAR 16:9, 23.98 fps, 23.98 tbr, 24k tbn (default)
      Metadata:
        timecode        : 00:59:58:00
  Stream #0:1[0x2](eng): Audio: pcm_s24le (in24 / 0x34326E69), 48000 Hz, 5.1(side), s32 (24 bit), 6912 kb/s (default)
"""


def test_ffmpeg_stderr_parse():
    info = P.parse_ffmpeg_stderr(STDERR_2398, "/x/x.mov")
    assert info.fps == Fraction(24000, 1001)
    assert (info.width, info.height) == (3840, 2160)
    assert info.duration_frames == 1440
    assert info.start_tc_frames == P.tc_to_frames("00:59:58:00", Fraction(24))
    assert info.audio_rate == 48000 and info.audio_channels == 6


def test_probe_fallback_real_files(media, ffmpeg):
    t = media["A"]
    info = P.probe(t["isos"]["cam1"]["path"], None, ffmpeg)
    assert info.fps == 25 and info.duration_frames == t["isos"]["cam1"]["frames"]
    assert info.start_tc_frames == t["isos"]["cam1"]["start_tc_frames"]
    assert (info.width, info.height) == (1920, 1080)
    assert info.has_audio and info.audio_rate == 48000 and info.audio_channels == 2
    b = P.probe(media["B"]["pgm"]["path"], None, ffmpeg)
    assert b.fps == Fraction(30000, 1001) and not b.has_timecode and (b.width, b.height) == (1280, 720)


def test_thumb_reader_seek_is_frame_exact(media, ffmpeg):
    from pgm2mc.frames import ThumbReader
    t = media["B"]
    path = t["isos"]["cam1"]["path"]
    fps = Fraction(*t["fps"])
    full, _ = ThumbReader(ffmpeg, path, fps, 0).read(200)
    for q in (1, 37, 100):
        r = ThumbReader(ffmpeg, path, fps, q)
        f, v = r.read(5)
        r.close()
        assert v.all()
        for i in range(5):
            mad = np.abs(full.astype(int) - f[i].astype(int)).reshape(len(full), -1).mean(axis=1)
            assert int(mad.argmin()) == q + i
    r = ThumbReader(ffmpeg, path, fps, -3)
    f, v = r.read(6)
    r.close()
    assert list(v) == [False, False, False, True, True, True] and np.array_equal(f[3], full[0])


def test_find_tool_env(tmp_path, monkeypatch, ffmpeg):
    monkeypatch.setenv("PGM2MC_FFMPEG", ffmpeg)
    assert P.find_tool("ffmpeg") == ffmpeg
    assert P.find_tool("ffmpeg", ffmpeg) == ffmpeg


# ---------------------------------------------------------------- sync / match units

def test_audio_lag_sign():
    rng = np.random.default_rng(5)
    world = rng.standard_normal(8000 * 30) * np.repeat(rng.random(30 * 5), 1600)
    pgm = world[8000 * 5:8000 * 25]
    for start in (0, 8000 * 2 + 123, 8000 * 8):          # ISO starts before / at / after the PGM
        iso = 0.4 * world[start:] + 0.05 * rng.standard_normal(len(world) - start)
        lag, conf, _ = S.audio_lag(pgm.astype(np.float32), iso.astype(np.float32))
        assert lag == 8000 * 5 - start
        assert conf > 0.3


def test_audio_lag_unrelated_is_low_confidence():
    rng = np.random.default_rng(6)
    a = rng.standard_normal(8000 * 20).astype(np.float32)
    b = rng.standard_normal(8000 * 20).astype(np.float32)
    _, conf, _ = S.audio_lag(a, b)
    assert conf < S.LOW_CONFIDENCE


def test_timecode_offset_rules():
    pgm = P.MediaInfo("/p.mov", Fraction(25), 1000, start_tc="01:00:00:00", start_tc_frames=90000)
    iso = P.MediaInfo("/i.mov", Fraction(25), 500, start_tc="00:59:50:00", start_tc_frames=89750)
    assert S.timecode_offset(pgm, iso) == 250
    iso.duration_frames = 200                           # ends before PGM starts
    assert S.timecode_offset(pgm, iso) is None
    iso.start_tc = None
    assert S.timecode_offset(pgm, iso) is None
    assert S.parse_manual_offsets(["CAM1=+120", "X=-3"]) == {"CAM1": 120, "X": -3}


def test_viterbi_min_shot_and_floor():
    T = 100
    sc = np.full((T, 2), 0.1, dtype=np.float32)
    sc[:40, 0] = 0.9
    sc[40:43, 1] = 0.9          # 3-frame blip: shorter than min_shot
    sc[40:43, 0] = 0.6
    sc[43:60, 0] = 0.9
    sc[60:68, 1] = 0.9          # 8-frame shot
    sc[68:80, 0] = 0.9
    # 80..100: nothing matches -> pgm
    lab = match.viterbi(sc, pgm_floor=0.5, switch_penalty=1.0, min_shot=6)
    segs = match.segments_from_labels(lab, sc, ["a1", "a2"])
    assert [(s["start"], s["end"], s["angle"]) for s in segs] == [
        (0, 60, "a1"), (60, 68, "a2"), (68, 80, "a1"), (80, 100, "pgm")]
    lab1 = match.viterbi(sc, pgm_floor=0.5, switch_penalty=0.1, min_shot=1)
    assert (lab1[40:43] == 1).all()


# ---------------------------------------------------------------- end to end

@pytest.mark.parametrize("name", ["A", "B"])
def test_scenario(name, media, results):
    truth, res = media[name], results[name]
    fps = Fraction(*truth["fps"])
    assert Fraction(res["fps"]["num"], res["fps"]["den"]) == fps
    want_method = "timecode" if gen_media.SCENARIOS[name]["timecode"] else "audio"
    for iso, cam in zip(res["isos"], CAMS):
        assert iso["name"] == cam
        assert iso["offset_frames"] == truth["isos"][cam]["offset"], (cam, iso)
        assert iso["sync_method"] == want_method
        assert iso["start_tc_frames"] == truth["isos"][cam]["start_tc_frames"]
        assert iso["duration_frames"] == truth["isos"][cam]["frames"]
    for s in res["segments"]:
        if s["angle"] != "pgm":
            assert s["score"] > 0.99, s       # off-by-one alignment would drop this to ~0.95
        assert s["kind"] == ("pgm" if s["angle"] == "pgm" else "camera")
    assert res["version"] == 2
    assert res["transitions"] == [] and res["stills"] == [] and res["supersources"] == []
    acc = check_cut_accuracy(res, truth)
    print("scenario %s accuracy %.4f" % (name, acc))


def test_manual_offsets_and_refine(media, ffmpeg):
    t = media["B"]
    offs = {c: t["isos"][c]["offset"] for c in CAMS}
    offs["cam1"] += 2                                   # deliberately wrong; refine must fix it
    res = analyze(t["pgm"]["path"], iso_paths(t), ffmpeg=ffmpeg, offsets=offs, refine=True, progress=None)
    for iso, cam in zip(res["isos"], CAMS):
        assert iso["sync_method"] == "manual"
        assert iso["offset_frames"] == t["isos"][cam]["offset"]
    check_cut_accuracy(res, t)


def test_rate_mismatch_is_an_error(media, ffmpeg):
    with pytest.raises(EngineError, match="frame rate"):
        analyze(media["B"]["pgm"]["path"], [media["A"]["isos"]["cam1"]["path"]], ffmpeg=ffmpeg, progress=None)


def test_cli_progress_and_json(media, ffmpeg, tmp_path):
    t = media["A"]
    out = tmp_path / "a.json"
    env = dict(os.environ, PYTHONPATH=ENGINE, PGM2MC_FFMPEG=ffmpeg)
    p = subprocess.run([sys.executable, "-m", "pgm2mc._analyze_cli", t["pgm"]["path"]] + iso_paths(t) +
                       ["--json", str(out)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert p.returncode == 0, p.stderr.decode()
    lines = p.stderr.decode().splitlines()
    prog = [ln for ln in lines if ln.startswith("PROGRESS")]
    for ln in prog:
        assert re.match(r"^PROGRESS (probe|sync|frames|match) (\d|[1-9]\d|100)$", ln), ln
    for stage in ("probe", "sync", "frames", "match"):
        assert "PROGRESS %s 100" % stage in prog
    res = json.loads(out.read_text())
    assert res["version"] == 1 and res["program"]["id"] == "pgm"
    check_cut_accuracy(res, t)


# ---------------------------------------------------------------- scenario C (v2: dissolves, dips, stills)

C_ID = {"cam1": "a1", "cam2": "a2", "cam3": "a3"}


@pytest.fixture(scope="session")
def media_c(ffmpeg):
    with open(gen_media.generate_c("C")) as f:
        return json.load(f)


@pytest.fixture(scope="session")
def result_c(media_c, ffmpeg, tmp_path_factory):
    """Scenario C analysed with a stand-in SuperSource module (records its call, finds nothing), so this test
    does not depend on the real detector; sys.modules is restored afterwards."""
    stages, ss_calls = [], []

    def fake_ss(ffmpeg_, analysis, candidates, assets_dir, min_len=25, progress=None):
        ss_calls.append([list(c) for c in candidates])
        return [], []

    key = "pgm2mc.supersource"
    saved = sys.modules.get(key)
    sys.modules[key] = types.SimpleNamespace(detect_supersources=fake_ss)
    try:
        assets = str(tmp_path_factory.mktemp("assetsC"))
        res = analyze(media_c["pgm"]["path"], iso_paths(media_c), ffmpeg=ffmpeg, assets_dir=assets,
                      progress=lambda st, p: stages.append((st, p)))
    finally:
        if saved is None:
            del sys.modules[key]
        else:
            sys.modules[key] = saved
    return res, stages, ss_calls, assets


def decode_rgb(ffmpeg, path):
    p = subprocess.run([ffmpeg, "-v", "error", "-i", path, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       stdout=subprocess.PIPE, check=True)
    return np.frombuffer(p.stdout, dtype=np.uint8).astype(np.int16)


def test_scenario_c_structure(media_c, result_c):
    res, stages, ss_calls, _ = result_c
    n = media_c["pgm_frames"]
    segs = res["segments"]
    assert res["version"] == 2 and res["program"]["duration_frames"] == n
    assert segs[0]["start"] == 0 and segs[-1]["end"] == n
    for a, b in zip(segs, segs[1:]):
        assert a["end"] == b["start"]
        assert (a["angle"], a["kind"], a.get("still")) != (b["angle"], b["kind"], b.get("still"))
    for s in segs:
        assert s["kind"] in ("camera", "pgm", "still", "supersource")
        assert (s["angle"] != "pgm") == (s["kind"] == "camera")
        assert 0.0 <= s["score"] <= 1.0
    for iso, cam in zip(res["isos"], CAMS):
        assert iso["offset_frames"] == media_c["isos"][cam]["offset"] and iso["sync_method"] == "timecode"
    for stage in ("probe", "sync", "frames", "match", "stills", "supersource", "dissolves"):
        assert (stage, 100) in stages, stage
    order = [st for st, _ in stages]
    assert order.index("stills") < order.index("supersource") < order.index("dissolves")
    gfx2 = [sh for sh in media_c["shots"] if sh["source"] == "gfx2"][0]
    assert ss_calls == [[[gfx2["start"], gfx2["end"]]]]     # only pgm segments >= 25 frames are candidates


def test_scenario_c_transitions(media_c, result_c):
    res = result_c[0]
    segs = res["segments"]
    bounds = {s["start"]: i for i, s in enumerate(segs)}
    got = sorted(res["transitions"], key=lambda t: t["edit"])
    want = media_c["transitions"]
    assert len(got) == len(want), got
    last = 0
    for g, w in zip(got, want):
        assert g["kind"] == w["kind"], (g, w)
        assert abs(g["edit"] - w["centre"]) <= 2, (g, w)
        assert abs(g["duration"] - w["duration"]) <= 3, (g, w)
        assert 0 < g["confidence"] <= 1
        if g["kind"] == "dip":
            assert g["color"] == [0, 0, 0]
        i = bounds[g["edit"]]
        lo = g["edit"] - g["duration"] // 2
        hi = lo + g["duration"]
        assert lo >= max(last, segs[i - 1]["start"]) and hi <= segs[i]["end"]
        last = hi
        for s in segs:                       # no spurious pgm segment inside a transition
            if s["kind"] == "pgm":
                assert s["end"] <= w["start"] or s["start"] >= w["start"] + w["duration"], (s, w)
        print("transition %s edit %d (truth %.1f) duration %d (truth %d) conf %.3f"
              % (g["kind"], g["edit"], w["centre"], g["duration"], w["duration"], g["confidence"]))


def test_scenario_c_labels(media_c, result_c):
    res = result_c[0]
    n = media_c["pgm_frames"]
    got = frame_labels(res["segments"], n)
    want = np.empty(n, dtype=object)
    for sh in media_c["shots"]:
        want[sh["start"]:sh["end"]] = C_ID.get(sh["source"], "pgm")
    keep = np.ones(n, dtype=bool)
    for tr in media_c["transitions"]:
        keep[tr["start"]:tr["start"] + tr["duration"]] = False
    acc = float(np.mean(got[keep] == want[keep]))
    print("scenario C camera-label accuracy outside transitions %.4f" % acc)
    assert acc >= 0.98


def test_scenario_c_stills(media_c, result_c, ffmpeg):
    res, _, _, assets = result_c
    assert len(res["stills"]) == 1
    st = res["stills"][0]
    assert st["role"] == "still" and st["id"] == "s1"
    assert os.path.dirname(st["path"]) == os.path.abspath(assets) and os.path.isfile(st["path"])
    assert (st["width"], st["height"]) == (res["program"]["width"], res["program"]["height"]) == (1280, 720)
    img = decode_rgb(ffmpeg, st["path"])
    src = decode_rgb(ffmpeg, media_c["gfx"]["gfx1"])
    assert img.size == src.size == 1280 * 720 * 3
    mae = float(np.abs(img - src).mean())
    print("still s1 mean abs error %.2f/255" % mae)
    assert mae < 6
    want = [[sh["start"], sh["end"]] for sh in media_c["shots"] if sh["source"] == "gfx1"]
    assert len(st["occurrences"]) == len(want) == 3
    for (gs, ge), (ws, we) in zip(st["occurrences"], want):
        assert abs(gs - ws) <= 2 and abs(ge - we) <= 2
    still_segs = [[s["start"], s["end"]] for s in res["segments"] if s["kind"] == "still"]
    assert still_segs == st["occurrences"]
    gfx2 = [sh for sh in media_c["shots"] if sh["source"] == "gfx2"][0]
    kinds = set(s["kind"] for s in res["segments"] if s["start"] < gfx2["end"] and s["end"] > gfx2["start"])
    assert kinds <= {"pgm", "camera"}
    lab = frame_labels(res["segments"], media_c["pgm_frames"])
    assert np.all(lab[gfx2["start"] + 1:gfx2["end"] - 1] == "pgm")


def test_scenario_c_v1_mode_and_switches(media_c, ffmpeg, tmp_path):
    """Without assets_dir the output stays version 1 (no PNGs, no stills) but cuts still follow dissolves."""
    res = analyze(media_c["pgm"]["path"], iso_paths(media_c), ffmpeg=ffmpeg, progress=None)
    assert res["version"] == 1 and res["stills"] == [] and res["supersources"] == []
    assert all(s["kind"] in ("camera", "pgm") for s in res["segments"])
    cam_trans = [t for t in media_c["transitions"] if t["boundary"] != 450]     # 450 needs the still template
    for w in cam_trans:
        assert any(abs(t["edit"] - w["centre"]) <= 2 and t["kind"] == w["kind"] for t in res["transitions"]), w
    off = analyze(media_c["pgm"]["path"], iso_paths(media_c), ffmpeg=ffmpeg, progress=None, dissolves=False,
                  stills=False, supersource=False, assets_dir=str(tmp_path))
    assert off["version"] == 2 and off["transitions"] == [] and off["stills"] == [] and not os.listdir(str(tmp_path))
