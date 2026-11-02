import json
import os
import re
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(os.path.dirname(HERE), "engine")
sys.path.insert(0, HERE)

import gen_media  # noqa: E402
import gen_ss  # noqa: E402
from test_fcpxml import check_file, cut_boundaries  # noqa: E402

CAMS = ["cam1", "cam2", "cam3"]
PROGRESS_RE = re.compile(r"^PROGRESS (probe|sync|frames|match|write|stills|supersource|dissolves) (\d|[1-9]\d|100)$")
RESULT_KEYS = {"multicam", "conform", "stacked", "analysis", "warnings", "segments", "stills_dir", "transitions",
               "supersources", "stills"}
SPINE = "library/event/project/sequence/spine"


@pytest.fixture(scope="module")
def ffmpeg():
    return gen_media.ffmpeg_exe()


@pytest.fixture(scope="module")
def media(ffmpeg):
    os.environ["PGM2MC_FFMPEG"] = ffmpeg
    out = {}
    for name in gen_media.SCENARIOS:
        with open(gen_media.generate(name)) as f:
            out[name] = json.load(f)
    return out


def run_cli(args, ffmpeg):
    env = dict(os.environ, PYTHONPATH=ENGINE, PGM2MC_FFMPEG=ffmpeg)
    p = subprocess.run([sys.executable, "-m", "pgm2mc"] + args, env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
    return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")


def parse_result(stdout):
    last = stdout.strip().splitlines()[-1]
    assert last.startswith("RESULT "), stdout
    return json.loads(last[len("RESULT "):])


@pytest.fixture(scope="module")
def runs(media, ffmpeg, tmp_path_factory):
    out = {}
    for name, t in media.items():
        outdir = str(tmp_path_factory.mktemp("out%s" % name) / "new" / "dir")
        args = [t["pgm"]["path"]] + [t["isos"][c]["path"] for c in CAMS] + ["-o", outdir]
        out[name] = (outdir,) + run_cli(args, ffmpeg)
    return out


@pytest.mark.parametrize("name", sorted(gen_media.SCENARIOS))
def test_cli_end_to_end(name, runs):
    outdir, rc, stdout, stderr = runs[name]
    assert rc == 0, stderr
    for ln in stderr.splitlines():
        assert PROGRESS_RE.match(ln) or ln.startswith("WARN "), ln
    for stage in ("probe", "sync", "frames", "match", "write"):
        assert "PROGRESS %s 100" % stage in stderr
    res = parse_result(stdout)
    assert set(res) == RESULT_KEYS
    assert res["stills_dir"] == os.path.join(os.path.abspath(outdir), "PGM_stills")
    assert os.path.isdir(res["stills_dir"])
    assert (res["transitions"], res["supersources"], res["stills"]) == (0, 0, 0)
    for key, suffix in (("multicam", "_multicam.fcpxml"), ("conform", "_conform.fcpxml"),
                        ("stacked", "_stacked.fcpxml"), ("analysis", "_analysis.json")):
        assert res[key] == os.path.join(os.path.abspath(outdir), "PGM" + suffix)
        assert os.path.isfile(res[key])
    with open(res["analysis"]) as f:
        analysis = json.load(f)
    assert analysis["version"] == 2
    segs = [(s["start"], s["end"], s["angle"]) for s in analysis["segments"]]
    assert res["segments"] == len(segs)
    mc = check_file(res["multicam"])
    assert cut_boundaries(mc) == segs
    angles = mc.findall("resources/media/multicam/mc-angle")
    assert [a.get("name") for a in angles] == CAMS + ["PGM"]
    conform = check_file(res["conform"])
    spine = conform.find("library/event/project/sequence/spine")
    assert len(spine.findall("asset-clip")) == len(segs)
    assert spine.find("asset-clip/asset-clip").get("lane") == "-1"
    stacked = check_file(res["stacked"])
    assert len(stacked.find("library/event/project/sequence/spine/gap")) == len(CAMS) + 1


def test_from_json_regenerates(runs, ffmpeg, tmp_path):
    outdir, rc, stdout, _ = runs["B"]
    assert rc == 0
    src = parse_result(stdout)["analysis"]
    rc, stdout, stderr = run_cli(["--from-json", src, "-o", str(tmp_path), "--name", "My Show",
                                  "--audio", "follow"], ffmpeg)
    assert rc == 0, stderr
    assert "PROGRESS write 100" in stderr and "PROGRESS probe" not in stderr
    res = parse_result(stdout)
    assert res["multicam"] == str(tmp_path / "My Show_multicam.fcpxml")
    mc = check_file(res["multicam"])
    assert mc.find("library/event/project").get("name") == "My Show Multicam Cut"
    assert all(len(c.findall("mc-source")) == 1 for c in mc.iter("mc-clip"))
    with open(src) as f, open(res["analysis"]) as g:
        assert json.load(f) == json.load(g)


def test_usage_and_runtime_errors(ffmpeg, tmp_path):
    rc, stdout, stderr = run_cli(["only_pgm.mov", "-o", str(tmp_path)], ffmpeg)
    assert rc == 2 and "ERROR " in stderr and "RESULT" not in stdout
    rc, stdout, stderr = run_cli(["a.mov", "b.mov"], ffmpeg)
    assert rc == 2 and "ERROR " in stderr
    rc, stdout, stderr = run_cli([str(tmp_path / "missing.mov"), str(tmp_path / "x.mov"), "-o", str(tmp_path)],
                                 ffmpeg)
    assert rc == 1 and re.search(r"^ERROR .*missing\.mov", stderr, re.M) and "RESULT" not in stdout
    bad = tmp_path / "bad.json"
    bad.write_text('{"version": 7}')
    rc, stdout, stderr = run_cli(["--from-json", str(bad), "-o", str(tmp_path)], ffmpeg)
    assert rc == 1 and "ERROR " in stderr


def test_bad_stills_min_repeat_is_usage_error(ffmpeg, tmp_path):
    rc, stdout, stderr = run_cli(["a.mov", "b.mov", "-o", str(tmp_path), "--stills-min-repeat", "0"], ffmpeg)
    assert rc == 2 and "ERROR " in stderr and "RESULT" not in stdout


@pytest.fixture(scope="module")
def scenario_c(ffmpeg):
    os.environ["PGM2MC_FFMPEG"] = ffmpeg
    with open(gen_media.generate_c("C")) as f:
        return json.load(f)


def _c_args(t):
    return [t["pgm"]["path"]] + [t["isos"][c]["path"] for c in CAMS]


def test_cli_scenario_c_v2(scenario_c, ffmpeg, tmp_path):
    rc, stdout, stderr = run_cli(_c_args(scenario_c) + ["-o", str(tmp_path)], ffmpeg)
    assert rc == 0, stderr
    for ln in stderr.splitlines():
        assert PROGRESS_RE.match(ln) or ln.startswith("WARN "), ln
    for stage in ("stills", "supersource", "dissolves"):
        assert "PROGRESS %s 100" % stage in stderr
    res = parse_result(stdout)
    assert set(res) == RESULT_KEYS
    assert res["transitions"] >= 3 and res["stills"] >= 1 and res["supersources"] == 0
    with open(res["analysis"]) as f:
        analysis = json.load(f)
    assert analysis["version"] == 2
    kinds = set(tr["kind"] for tr in analysis["transitions"])
    assert kinds == {"dissolve", "dip"}
    pngs = sorted(os.listdir(res["stills_dir"]))
    assert "dip_000000.png" in pngs and any(st["path"] == os.path.join(res["stills_dir"], "s1.png")
                                            for st in analysis["stills"])
    mc, conform = check_file(res["multicam"]), check_file(res["conform"])
    check_file(res["stacked"])
    for root, tag in ((mc, "mc-clip"), (conform, "asset-clip")):
        spine = root.find(SPINE)
        assert len(spine.findall("transition")) >= 4  # >= 2 dissolves + the dip's pair
        slugs = spine.findall("video")
        assert slugs and all(v.get("name") == "dip to black" for v in slugs)
        lanes = [v for c in spine.findall(tag) for v in c.findall("video") if v.get("lane") == "1"]
        assert len(lanes) == sum(1 for s in analysis["segments"] if s["kind"] == "still")
        # the dissolve into the 450 graphic touches a still: no <transition>, a marker instead
        assert any(m.get("value") == "PGM transition" for m in spine.iter("marker"))
    rc, stdout2, stderr = run_cli(["--from-json", res["analysis"], "-o", str(tmp_path / "again")], ffmpeg)
    assert rc == 0, stderr
    res2 = parse_result(stdout2)
    assert [res2[k] for k in ("transitions", "stills", "segments")] == [res[k] for k in
                                                                      ("transitions", "stills", "segments")]
    assert os.path.isfile(os.path.join(res2["stills_dir"], "dip_000000.png"))
    check_file(res2["multicam"])


def test_cli_scenario_c_feature_flags(scenario_c, ffmpeg, tmp_path):
    rc, stdout, stderr = run_cli(_c_args(scenario_c) + ["-o", str(tmp_path), "--no-dissolves", "--no-stills",
                                                        "--no-supersource", "--name", "Flat"], ffmpeg)
    assert rc == 0, stderr
    res = parse_result(stdout)
    assert (res["transitions"], res["stills"], res["supersources"]) == (0, 0, 0)
    assert res["stills_dir"] == str(tmp_path / "Flat_stills") and os.listdir(res["stills_dir"]) == []
    mc = check_file(res["multicam"])
    assert mc.find(SPINE + "/transition") is None and mc.find(SPINE + "/mc-clip/video") is None


def test_cli_supersource_shoot(ffmpeg, tmp_path):
    os.environ["PGM2MC_FFMPEG"] = ffmpeg
    with open(gen_ss.generate()) as f:
        truth = json.load(f)
    args = [truth["pgm"]["path"]] + [truth["isos"][c]["path"] for c in CAMS] + ["-o", str(tmp_path)]
    rc, stdout, stderr = run_cli(args, ffmpeg)
    assert rc == 0, stderr
    res = parse_result(stdout)
    assert res["supersources"] >= 2
    for path, tag in ((res["multicam"], "mc-clip"), (res["conform"], "asset-clip")):
        root = check_file(path)
        ss_clips = [c for c in root.find(SPINE).findall(tag) if "SuperSource" in c.get("name")]
        assert len(ss_clips) >= 3
        for clip in ss_clips:
            bg = clip.find("video")
            assert bg.get("lane") == "1" and bg.get("name") == "PGM SuperSource BG"
            boxes = [c for c in clip.findall("asset-clip") if c.get("lane") not in (None, "-1")]
            assert boxes and [b.get("lane") for b in boxes] == [str(i) for i in range(2, len(boxes) + 2)]
            for b in boxes:
                assert b.get("name").endswith(" box") and b.get("srcEnable") == "video"
                assert b.find("adjust-crop").get("mode") == "trim" and b.find("adjust-transform") is not None
    # first SuperSource (layout A): cam1 box centred at x = 388 px of 1280, scale 456/1013.3 = 0.45 of fit
    mc = check_file(res["multicam"])
    first = [c for c in mc.find(SPINE).findall("mc-clip") if "SuperSource" in c.get("name")][0]
    box = [b for b in first.findall("asset-clip") if b.get("name") == "cam1 box"][0]
    x, y = (float(v) for v in box.find("adjust-transform").get("position").split())
    sx = float(box.find("adjust-transform").get("scale").split()[0])
    assert abs(x - (388 - 640) / 720.0 * 100) < 1.0 and abs(y) < 1.0 and abs(sx - 0.45) < 0.01
    # each supersource holds single shots: graphic outside, layout A grouped, layout B separate and static
    with open(res["analysis"]) as f:
        analysis = json.load(f)
    tl = truth["timeline"]
    gfx = [e for e in tl if e["kind"] == "graphic"][0]
    for ss in analysis["supersources"]:
        for s, e in ss["occurrences"]:
            assert e <= gfx["start"] + 2 or s >= gfx["end"] - 2, ("graphic inside", ss["id"], s, e)

    def ss_at(entry):
        hits = [ss for ss in analysis["supersources"]
                if any(abs(s - entry["start"]) <= 2 and abs(e - entry["end"]) <= 2 for s, e in ss["occurrences"])]
        assert len(hits) == 1, (entry, analysis["supersources"])
        return hits[0]

    a_ss = [ss_at(e) for e in tl if e["kind"] == "ss" and e["arg"] == "A"]
    b_ss = ss_at([e for e in tl if e["kind"] == "ss" and e["arg"] == "B"][0])
    assert len(set(ss["id"] for ss in a_ss)) == 1 and len(a_ss[0]["occurrences"]) == 2
    assert b_ss["id"] != a_ss[0]["id"] and b_ss["animated"] is False and not a_ss[0]["animated"]
    layer = {"cam%s" % b["angle"][1:]: b["layer"] for b in b_ss["boxes"]}  # a1..a3 = CAMS order
    lay = [e for e in tl if e["arg"] == "B"][0]["layout"]  # bottom first; only overlapping pairs are ordered
    pairs = [(p["cam"], q["cam"]) for i, p in enumerate(lay) for q in lay[i + 1:]
             if min(p["rect"][2], q["rect"][2]) > max(p["rect"][0], q["rect"][0])
             and min(p["rect"][3], q["rect"][3]) > max(p["rect"][1], q["rect"][1])]
    assert len(pairs) == 2 and all(layer[lo] < layer[hi] for lo, hi in pairs), (pairs, layer)
    assert not any(st["role"] == "still" for st in analysis["stills"])  # no SS layout taken for a still
