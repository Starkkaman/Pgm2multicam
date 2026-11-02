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
from test_fcpxml import check_file, cut_boundaries  # noqa: E402

CAMS = ["cam1", "cam2", "cam3"]
PROGRESS_RE = re.compile(r"^PROGRESS (probe|sync|frames|match|write) (\d|[1-9]\d|100)$")


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
    assert set(res) == {"multicam", "conform", "stacked", "analysis", "warnings", "segments"}
    for key, suffix in (("multicam", "_multicam.fcpxml"), ("conform", "_conform.fcpxml"),
                        ("stacked", "_stacked.fcpxml"), ("analysis", "_analysis.json")):
        assert res[key] == os.path.join(os.path.abspath(outdir), "PGM" + suffix)
        assert os.path.isfile(res[key])
    with open(res["analysis"]) as f:
        analysis = json.load(f)
    assert analysis["version"] == 1
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
