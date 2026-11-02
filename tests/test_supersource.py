"""SuperSource detection: synthetic composites from tests/gen_ss.py, plus a graphics-only regression on the
v2 scenario C of tests/gen_media.py."""
import json
import os
import subprocess
import sys

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "engine"))

import gen_ss  # noqa: E402
from pgm2mc.supersource import detect_supersources  # noqa: E402

IDS = {"cam1": "a1", "cam2": "a2", "cam3": "a3"}


def make_analysis(truth, width, height, frames):
    return {"version": 2, "fps": {"num": 25, "den": 1}, "drop_frame": False,
            "program": {"id": "pgm", "path": truth["pgm"]["path"], "name": "PGM", "start_tc_frames": 0,
                        "duration_frames": frames, "width": width, "height": height},
            "isos": [{"id": IDS[c], "path": v["path"], "name": c, "start_tc_frames": v["start_tc_frames"],
                      "duration_frames": v["frames"], "width": 1920, "height": 1080,
                      "offset_frames": v["offset"]} for c, v in sorted(truth["isos"].items())],
            "segments": [], "warnings": []}


def read_png(path, w, h):
    p = subprocess.run([gen_ss.ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error", "-i", path,
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE, check=True)
    assert len(p.stdout) == w * h * 3, "PNG is not %dx%d" % (w, h)
    return np.frombuffer(p.stdout, dtype=np.uint8).reshape(h, w, 3)


@pytest.fixture(scope="module")
def ss_run(tmp_path_factory):
    with open(gen_ss.generate()) as f:
        truth = json.load(f)
    pw, ph = truth["pgm_size"]
    analysis = make_analysis(truth, pw, ph, truth["pgm_frames"])
    # every non-camera range, a full-frame camera range, and one range shorter than min_len
    cands = [[e["start"], e["end"]] for e in truth["timeline"] if e["kind"] != "cam"]
    cands += [[150, 200], [0, 20]]
    assets = str(tmp_path_factory.mktemp("ss_assets"))
    progress = []
    ss, stills = detect_supersources(gen_ss.ffmpeg_exe(), analysis, cands, assets, progress=progress.append)
    return {"truth": truth, "ss": ss, "stills": stills, "assets": assets, "progress": progress,
            "analysis": analysis}


def truth_entries(truth, arg):
    return [e for e in truth["timeline"] if e["kind"] == "ss" and e["arg"] == arg]


def find_ss(ss, start):
    hits = [s for s in ss if any(o[0] == start for o in s["occurrences"])]
    assert len(hits) == 1, "no single supersource for the range starting at %d: %s" % (start, ss)
    return hits[0]


def test_same_layout_grouped(ss_run):
    truth, ss = ss_run["truth"], ss_run["ss"]
    assert len(ss) == 3
    a = truth_entries(truth, "A")
    sa = find_ss(ss, a[0]["start"])
    assert sorted(sa["occurrences"]) == sorted([e["start"], e["end"]] for e in a)
    b = truth_entries(truth, "B")
    sb = find_ss(ss, b[0]["start"])
    assert sb["occurrences"] == [[b[0]["start"], b[0]["end"]]]
    assert [s["id"] for s in ss] == ["ss1", "ss2", "ss3"]
    for s in (sa, sb):
        assert s["animated"] is False
        assert 0.6 <= s["confidence"] <= 1.0


def test_animated_box(ss_run):
    entry = truth_entries(ss_run["truth"], "M")[0]
    s = find_ss(ss_run["ss"], entry["start"])
    assert s["animated"] is True
    assert s["occurrences"] == [[entry["start"], entry["end"]]]
    assert s["confidence"] < min(x["confidence"] for x in ss_run["ss"] if not x["animated"])
    # the layout kept is the one of the middle sample frame
    mid = entry["start"] + int(round((entry["end"] - entry["start"]) * 0.5))
    dx = entry["move_px"] * (mid - entry["start"]) / float(ss_run["truth"]["pgm_size"][0])
    want = entry["layout"][0]
    want_rect = [want["rect"][0] + dx, want["rect"][1], want["rect"][2] + dx, want["rect"][3]]
    assert [b["angle"] for b in s["boxes"]] == [IDS[want["cam"]]]
    assert max(abs(x - y) for x, y in zip(s["boxes"][0]["rect"], want_rect)) <= 0.01
    assert max(abs(x - y) for x, y in zip(s["boxes"][0]["crop"], want["crop"])) <= 0.02


@pytest.mark.parametrize("arg", ["A", "B"])
def test_box_geometry(ss_run, arg):
    entry = truth_entries(ss_run["truth"], arg)[0]
    s = find_ss(ss_run["ss"], entry["start"])
    got = {b["angle"]: b for b in s["boxes"]}
    want = {IDS[b["cam"]]: b for b in entry["layout"]}
    assert sorted(got) == sorted(want)
    for angle, w in want.items():
        g = got[angle]
        rect_err = max(abs(x - y) for x, y in zip(g["rect"], w["rect"]))
        crop_err = max(abs(x - y) for x, y in zip(g["crop"], w["crop"]))
        assert rect_err <= 0.01, (angle, g["rect"], w["rect"])
        assert crop_err <= 0.02, (angle, g["crop"], w["crop"])
    assert sorted(b["layer"] for b in s["boxes"]) == list(range(1, len(s["boxes"]) + 1))


def test_layer_order(ss_run):
    entry = truth_entries(ss_run["truth"], "B")[0]
    s = find_ss(ss_run["ss"], entry["start"])
    layer = {b["angle"]: b["layer"] for b in s["boxes"]}
    boxes = entry["layout"]
    checked = 0
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i]["rect"], boxes[j]["rect"]
            if min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1]):
                lo, hi = IDS[boxes[i]["cam"]], IDS[boxes[j]["cam"]]  # truth lists bottom first
                assert layer[lo] < layer[hi], (lo, hi, layer)
                checked += 1
    assert checked == 2


def test_non_composites_ignored(ss_run):
    occ = [o for s in ss_run["ss"] for o in s["occurrences"]]
    gfx = [e for e in ss_run["truth"]["timeline"] if e["kind"] == "graphic"][0]
    for o in occ:
        assert o[1] <= gfx["start"] or o[0] >= gfx["end"], "graphic range taken as supersource"
        assert o != [150, 200], "full-frame camera taken as supersource"
        assert o != [0, 20], "range shorter than min_len analysed"


def test_background_still(ss_run):
    truth, ss, stills = ss_run["truth"], ss_run["ss"], ss_run["stills"]
    pw, ph = truth["pgm_size"]
    assert len(stills) == len(ss)
    by_id = {st["id"]: st for st in stills}
    gt = np.fromfile(truth["background"], dtype=np.uint8).reshape(ph, pw, 3).astype(np.float64)
    for s in ss:
        st = by_id[s["background"]]
        assert st["id"].startswith("ssbg") and st["role"] == "background"
        assert st["occurrences"] == s["occurrences"]
        assert (st["width"], st["height"]) == (pw, ph)
        assert os.path.isabs(st["path"]) and os.path.dirname(st["path"]) == os.path.abspath(ss_run["assets"])
        assert os.path.basename(st["path"]) == st["id"] + ".png"
        img = read_png(st["path"], pw, ph).astype(np.float64)
        outside = np.ones((ph, pw), dtype=bool)
        for b in s["boxes"]:
            x0, y0, x1, y1 = b["rect"]
            outside[int(round(y0 * ph)):int(round(y1 * ph)), int(round(x0 * pw)):int(round(x1 * pw))] = False
        mae = float(np.abs(img - gt).mean(axis=-1)[outside].mean())
        assert mae < 8.0, "background MAE %.2f" % mae


def test_progress(ss_run):
    p = ss_run["progress"]
    assert p and p[-1] == 100.0
    assert all(b >= a for a, b in zip(p, p[1:]))


def test_never_overwrites_existing_files(ss_run, tmp_path):
    entry = truth_entries(ss_run["truth"], "A")[0]
    taken = tmp_path / "ssbg1.png"
    taken.write_bytes(b"not mine")
    ss, stills = detect_supersources(gen_ss.ffmpeg_exe(), ss_run["analysis"], [[entry["start"], entry["end"]]],
                                     str(tmp_path))
    assert taken.read_bytes() == b"not mine"
    assert len(ss) == 1 and len(stills) == 1
    assert stills[0]["id"] == ss[0]["background"] == "ssbg2"
    assert stills[0]["path"] == str(tmp_path / "ssbg2.png")


def test_scenario_c_graphics_are_not_supersources(tmp_path):
    import gen_media
    if not hasattr(gen_media, "generate_c"):
        pytest.skip("gen_media has no v2 scenario C")
    with open(gen_media.generate_c("C")) as f:
        truth = json.load(f)
    analysis = make_analysis(truth, 1280, 720, truth["pgm_frames"])
    cands = [[sh["start"], sh["end"]] for sh in truth["shots"] if sh["source"].startswith("gfx")]
    assert any(sh["source"] == "gfx2" for sh in truth["shots"])
    ss, stills = detect_supersources(gen_media.ffmpeg_exe(), analysis, cands, str(tmp_path))
    assert ss == [] and stills == []
    assert os.listdir(str(tmp_path)) == []
