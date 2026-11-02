"""Tests for resolve/PGM to Multicam.py without DaVinci Resolve."""
import importlib.util
import json
import os
import sys
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "resolve", "PGM to Multicam.py")


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("pgm_to_multicam", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)  # must not run main() without `resolve`
    return m


# ---------------------------------------------------------------- pure functions

@pytest.mark.parametrize("name,expected", [
    ("PGM.mov", True), ("Show_Program_Out.mxf", True), ("prog rec", True), ("PVW Out 1", True),
    ("pvw_out", True), ("CAM1.mov", False), ("ISO_wide", False), ("", False),
])
def test_program_regex(mod, name, expected):
    assert mod.is_program_name(name) is expected


def test_guess_program_index(mod):
    assert mod.guess_program_index(["CAM1", "CAM2", "Main PGM"]) == 2
    assert mod.guess_program_index(["CAM1", "CAM2"]) == 0
    assert mod.program_candidates(["PGM", "CAM", "program b"]) == [0, 2]


@pytest.mark.parametrize("t,ok", [
    ("Video + Audio", True), ("Video", True), ("Audio", False), ("Timeline", False),
    ("Multicam", False), ("Compound", False), ("Still", False), ("", False), (None, False),
])
def test_is_video_type(mod, t, ok):
    assert mod.is_video_type(t) is ok


def test_build_argv(mod):
    argv = mod.build_argv("/py", "/m/PGM.mov", ["/m/C1.mov", "/m/C2.mov"], "/out", name="PGM 2",
                          sync="Timecode", audio="Follow camera", min_shot=8, refine=True,
                          ffmpeg="/b/ffmpeg", ffprobe="/b/ffprobe")
    assert argv == ["/py", "-u", "-m", "pgm2mc", "/m/PGM.mov", "/m/C1.mov", "/m/C2.mov", "-o", "/out",
                    "--name", "PGM 2", "--sync", "timecode", "--audio", "follow", "--min-shot", "8",
                    "--refine", "--ffmpeg", "/b/ffmpeg", "--ffprobe", "/b/ffprobe"]
    d = mod.build_argv("/py", "P", ["I"], "/o")
    assert d == ["/py", "-u", "-m", "pgm2mc", "P", "I", "-o", "/o", "--sync", "auto", "--audio", "pgm",
                 "--min-shot", "6"]
    with pytest.raises(ValueError):
        mod.build_argv("/py", "P", ["I"], "/o", sync="manual")


def test_rebuild_flags(mod):
    assert mod.rebuild_flags() == [] and mod.rebuild_flags(mod.DEFAULT_REBUILD) == []
    assert mod.rebuild_flags({"dissolves": False, "stills": False, "stills_min_repeat": 5,
                              "supersource": False}) == ["--no-dissolves", "--no-stills", "--no-supersource"]
    assert mod.rebuild_flags({"stills_min_repeat": 3}) == ["--stills-min-repeat", "3"]
    assert mod.rebuild_flags({"stills_min_repeat": 99}) == ["--stills-min-repeat", "20"]
    argv = mod.build_argv("/py", "P", ["I"], "/o", rebuild={"supersource": False, "stills_min_repeat": 4})
    assert argv[-3:] == ["--stills-min-repeat", "4", "--no-supersource"]


def test_result_summary(mod):
    assert mod.result_summary({"transitions": 12, "stills": 3, "supersources": 2}) == \
        "12 transitions, 3 stills, 2 SuperSource layouts"
    assert mod.result_summary({"transitions": 1, "stills": 1, "supersources": 1}) == \
        "1 transition, 1 still, 1 SuperSource layout"
    assert mod.result_summary({"segments": 3}) == ""
    assert mod.describe_event("result", {"segments": 3}) == "Engine finished: 3 segments."


def test_box_props_worked_example(mod):
    # RESEARCH2 §3.3: ISO 3840x2160 in 1920x1080, crop 10 % left/right -> Pan -480, Zoom 0.45, Crop 192
    p = mod.box_props({"rect": [0.07, 0.275, 0.43, 0.725], "crop": [0.1, 0, 0.1, 0]}, 3840, 2160, 1920, 1080)
    assert p["Pan"] == pytest.approx(-480) and p["Tilt"] == 0 and str(p["Tilt"]) == "0.0"
    assert p["ZoomX"] == pytest.approx(0.45) and p["ZoomY"] == pytest.approx(0.45) and p["ZoomGang"] is True
    assert p["CropLeft"] == pytest.approx(192) and p["CropRight"] == pytest.approx(192)
    assert p["CropTop"] == 0 and p["CropBottom"] == 0
    assert p["Scaling"] == 2 and p["CropRetain"] is True
    right = mod.box_props({"rect": [0.57, 0.275, 0.93, 0.725], "crop": [0.1, 0, 0.1, 0]}, 3840, 2160, 1920, 1080)
    assert right["Pan"] == pytest.approx(480)
    # uncropped quarter box top-left, non-uniform beyond tolerance keeps separate zooms
    q = mod.box_props({"rect": [0, 0, 0.5, 0.5], "crop": [0, 0, 0, 0]}, 1920, 1080, 1920, 1080)
    assert (q["Pan"], q["Tilt"], q["ZoomX"]) == (pytest.approx(-480), pytest.approx(270), pytest.approx(0.5))
    s = mod.box_props({"rect": [0, 0, 0.5, 0.25]}, 1920, 1080, 1920, 1080)
    assert s["ZoomGang"] is False and s["ZoomY"] == pytest.approx(0.25)


def test_parse_line(mod):
    assert mod.parse_line("PROGRESS sync 40\n") == ("progress", ("sync", 40))
    assert mod.parse_line("PROGRESS frames 140") == ("progress", ("frames", 100))
    assert mod.parse_line("WARN low audio level") == ("warn", "low audio level")
    assert mod.parse_line("ERROR no overlap") == ("error", "no overlap")
    kind, data = mod.parse_line('RESULT {"multicam": "/o/a.fcpxml", "segments": 3}')
    assert kind == "result" and data["segments"] == 3
    assert mod.parse_line("RESULT {broken")[0] == "log"
    assert mod.parse_line("hello") == ("log", "hello")
    assert mod.progress_text("sync", 40) == u"Syncing\u2026 40%"


def test_default_output_dir(mod, tmp_path):
    pgm = tmp_path / "shoot" / "PGM.mov"
    pgm.parent.mkdir()
    pgm.write_bytes(b"")
    assert mod.default_output_dir(str(pgm)) == str(tmp_path / "shoot" / "pgm2multicam")
    got = mod.default_output_dir(str(pgm), home="/home/x", writable=lambda d: False)
    assert got == os.path.join("/home/x", "Movies", "pgm2multicam")
    assert mod.default_output_dir("", home="/h") == os.path.join("/h", "Movies", "pgm2multicam")


def test_paths_and_env(mod):
    assert mod.app_dir("darwin", {}, "/Users/e") == "/Users/e/Library/Application Support/pgm2multicam"
    assert mod.app_dir("linux", {}, "/home/e") == "/home/e/.local/share/pgm2multicam"
    assert mod.app_dir("win32", {"APPDATA": "C:/R"}, "/x").endswith("pgm2multicam")
    assert mod.default_python("/a", "darwin") == "/a/venv/bin/python"
    env = mod.child_env("/eng", {"PATH": "/usr/bin:/bin", "PYTHONHOME": "/r", "PYTHONPATH": "/z"}, "darwin")
    assert env["PYTHONPATH"] == "/eng" and "PYTHONHOME" not in env
    assert env["PATH"].split(":")[:2] == ["/opt/homebrew/bin", "/usr/local/bin"]
    assert mod.child_env("/eng", {"PATH": "/usr/bin"}, "linux")["PATH"] == "/usr/bin"


def test_load_config(mod, tmp_path):
    with pytest.raises(mod.EngineNotInstalled):
        mod.load_config(str(tmp_path), "linux")
    (tmp_path / "eng" / "pgm2mc").mkdir(parents=True)
    (tmp_path / "config.json").write_text(json.dumps(
        {"python": sys.executable, "engine_dir": str(tmp_path / "eng"), "ffmpeg": "/f", "ffprobe": "/p"}))
    cfg = mod.load_config(str(tmp_path), "linux")
    assert cfg["python"] == sys.executable and cfg["ffmpeg"] == "/f"


def test_unique_names_and_plan(mod):
    assert mod.unique_base_name("PGM", []) == "PGM"
    assert mod.unique_base_name("PGM", ["PGM Conform"]) == "PGM 2"
    assert mod.unique_base_name("PGM", ["PGM Stacked", "PGM 2 Multicam Cut"]) == "PGM 3"
    res = {"multicam": "/o/m.fcpxml", "conform": "/o/c.fcpxml", "stacked": "/o/s.fcpxml"}
    plan = mod.planned_imports(res, "PGM", {"multicam": True, "conform": False, "stacked": True})
    assert plan == [("multicam", "/o/m.fcpxml", "PGM Multicam Cut"), ("stacked", "/o/s.fcpxml", "PGM Stacked")]


# ---------------------------------------------------------------- fake Resolve smoke test (headless path)

class FakeClip(object):
    def __init__(self, name, path, ctype="Video + Audio"):
        self.name, self.props = name, {"File Path": path, "Type": ctype}

    def GetName(self):
        return self.name

    def GetClipProperty(self, key=None):
        return self.props if key is None else self.props.get(key, "")


class FakeTimeline(object):
    def __init__(self, name):
        self.name = name

    def GetName(self):
        return self.name


class FakeFolder(object):
    def __init__(self, name, clips=None):
        self.name, self.clips, self.subs = name, list(clips or []), []

    def GetName(self):
        return self.name

    def GetClipList(self):
        return list(self.clips)

    def GetSubFolderList(self):
        return list(self.subs)


class FakeMediaPool(object):
    def __init__(self, selected, project, fail=(), mc_type="Multicam"):
        self.selected, self.project, self.fail, self.mc_type = selected, project, fail, mc_type
        self.root = FakeFolder("Master", selected)
        self.current = self.root
        self.imports = []
        self.media_imports = []

    def __dir__(self):
        return ["GetSelectedClips", "ImportTimelineFromFile", "GetRootFolder"]

    def GetSelectedClips(self):
        return {i + 1: c for i, c in enumerate(self.selected)}

    def GetRootFolder(self):
        return self.root

    def GetCurrentFolder(self):
        return self.current

    def AddSubFolder(self, parent, name):
        f = FakeFolder(name)
        parent.subs.append(f)
        return f

    def SetCurrentFolder(self, f):
        self.current = f
        return True

    def ImportMedia(self, paths):
        self.media_imports.append((list(paths), self.current.name))
        clips = [FakeClip(os.path.basename(p), p, "Still") for p in paths]
        self.current.clips += clips
        return clips

    def ImportTimelineFromFile(self, path, opts):
        self.imports.append((path, dict(opts), self.current.name))
        if any(path.endswith(s) for s in self.fail):
            return None
        assert os.path.isfile(path)
        if "multicam" in path:
            self.current.clips.append(FakeClip("PGM Multicam", "", self.mc_type))
        tl = FakeTimeline(opts["timelineName"])
        self.project.timelines.append(tl)
        return tl


class FakeProject(object):
    def __init__(self, selected, fail=(), mc_type="Multicam"):
        self.timelines = [FakeTimeline("PGM Multicam Cut")]
        self.mp = FakeMediaPool(selected, self, fail, mc_type)
        self.current_tl = None

    def GetMediaPool(self):
        return self.mp

    def GetTimelineCount(self):
        return len(self.timelines)

    def GetTimelineByIndex(self, i):
        return self.timelines[i - 1]

    def SetCurrentTimeline(self, tl):
        self.current_tl = tl
        return True


class FakeResolve(object):
    def __init__(self, project):
        self.project = project

    def GetProjectManager(self):
        return self

    def GetCurrentProject(self):
        return self.project


FAKE_ENGINE = textwrap.dedent('''
    import json, os, sys
    args = sys.argv[1:]
    out = args[args.index("-o") + 1]
    name = args[args.index("--name") + 1]
    with open(os.path.join(out, "argv.json"), "w") as fh:
        json.dump(args, fh)
    for stage in ("probe", "sync", "frames", "match", "write"):
        sys.stderr.write("PROGRESS %s 50\\nPROGRESS %s 100\\n" % (stage, stage))
    sys.stderr.write("WARN fake warning\\n")
    res = {"warnings": ["fake warning"], "segments": 4, "transitions": 12, "stills": 3, "supersources": 0}
    sdir = os.path.join(out, name + "_stills")
    os.makedirs(sdir)
    for s in ("s1.png", "s2.png"):
        open(os.path.join(sdir, s), "wb").close()
    res["stills_dir"] = sdir
    for kind in ("multicam", "conform", "stacked"):
        p = os.path.join(out, "%s_%s.fcpxml" % (name, kind))
        open(p, "w").write("<fcpxml/>")
        res[kind] = p
    res["analysis"] = os.path.join(out, name + "_analysis.json")
    print("some log line")
    print("RESULT " + json.dumps(res))
''')


@pytest.fixture
def fake_install(tmp_path):
    eng = tmp_path / "engine"
    (eng / "pgm2mc").mkdir(parents=True)
    (eng / "pgm2mc" / "__init__.py").write_text("")
    (eng / "pgm2mc" / "__main__.py").write_text(FAKE_ENGINE)
    return {"python": sys.executable, "engine_dir": str(eng)}


def _media(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    clips = []
    for n in ("CAM1", "Show PGM", "CAM2"):
        p = media / (n + ".mov")
        p.write_bytes(b"")
        clips.append(FakeClip(n + ".mov", str(p)))
    clips.append(FakeClip("Music.wav", str(media / "Music.wav"), "Audio"))
    clips.append(FakeClip("Old Timeline", "", "Timeline"))
    return media, clips


def test_headless_smoke(mod, tmp_path, fake_install):
    media, clips = _media(tmp_path)
    project = FakeProject(clips)
    lines = []
    ok = mod.run_headless(FakeResolve(project), cfg=fake_install, out=lines.append)
    text = "\n".join(str(x) for x in lines)
    assert ok, text
    argv = json.loads((media / "pgm2multicam" / "argv.json").read_text())
    assert argv[0] == str(media / "Show PGM.mov")
    assert argv[1:3] == [str(media / "CAM1.mov"), str(media / "CAM2.mov")]
    assert argv[argv.index("--name") + 1] == "Show PGM"
    assert u"Syncing\u2026 50%" in text and "Warning: fake warning" in text
    names = [i[1]["timelineName"] for i in project.mp.imports]
    assert names == ["Show PGM Multicam Cut", "Show PGM Conform"]
    assert all(i[1]["importSourceClips"] is True and i[2] == "PGM to Multicam" for i in project.mp.imports)
    assert project.current_tl.GetName() == "Show PGM Multicam Cut"
    assert mod.COMPOUND_HINT not in text
    assert "Engine finished: 4 segments; 12 transitions, 3 stills, 0 SuperSource layouts." in text
    sdir = media / "pgm2multicam" / "Show PGM_stills"
    assert project.mp.media_imports == [([str(sdir / "s1.png"), str(sdir / "s2.png")], "PGM to Multicam")]
    assert "Imported 2 of 2 still image(s)" in text
    assert "SuperSource" not in text.replace("SuperSource layouts", "")
    for flag in ("--no-dissolves", "--no-stills", "--no-supersource", "--stills-min-repeat"):
        assert flag not in argv


def test_headless_import_failure_and_name_clash(mod, tmp_path, fake_install):
    media, clips = _media(tmp_path)
    clips[1] = FakeClip("PGM.mov", clips[1].props["File Path"].replace("Show PGM", "PGM"))
    open(clips[1].props["File Path"], "wb").close()
    project = FakeProject(clips, fail=("_multicam.fcpxml",))
    lines = []
    assert mod.run_headless(FakeResolve(project), cfg=fake_install, out=lines.append)
    text = "\n".join(str(x) for x in lines)
    assert "Import failed" in text and "File > Import > Timeline" in text
    assert project.current_tl.GetName() == "PGM 2 Conform"  # "PGM Multicam Cut" already existed
    assert mod.COMPOUND_HINT not in text


def test_headless_compound_hint(mod, tmp_path, fake_install):
    _, clips = _media(tmp_path)
    project = FakeProject(clips, mc_type="Compound")
    lines = []
    assert mod.run_headless(FakeResolve(project), cfg=fake_install, out=lines.append)
    assert mod.COMPOUND_HINT in lines


def test_headless_requires_single_program(mod, tmp_path, fake_install):
    _, clips = _media(tmp_path)
    lines = []
    assert not mod.run_headless(FakeResolve(FakeProject([clips[0], clips[2]])), cfg=fake_install,
                                out=lines.append)
    assert "no program clip" in lines[-1]
    extra = FakeClip("Program B.mov", "/x/Program B.mov")
    lines = []
    assert not mod.run_headless(FakeResolve(FakeProject(clips + [extra])), cfg=fake_install,
                                out=lines.append)
    assert "more than one" in lines[-1]


def test_main_without_resolve(mod, capsys):
    assert mod.main({}) is False
    assert "run this from DaVinci Resolve" in capsys.readouterr().out


# ---------------------------------------------------------------- SuperSource box fix-up against fake timelines

class FakeItem(object):
    def __init__(self, start, path, name="", refuse=()):
        self.start, self.name, self.refuse, self.set = start, name, refuse, []
        self.mpi = FakeClip(os.path.basename(path) if path else name, path) if path is not None else None

    def GetStart(self):
        return self.start

    def GetName(self):
        return self.name

    def GetMediaPoolItem(self):
        return self.mpi

    def SetProperty(self, key, value):
        self.set.append((key, value))
        return key not in self.refuse


class FakeTrackTimeline(object):
    def __init__(self, tracks, start=90000, name="T"):
        self.tracks, self.start, self.name = tracks, start, name

    def GetName(self):
        return self.name

    def GetStartFrame(self):
        return self.start

    def GetTrackCount(self, kind):
        return len(self.tracks) if kind == "video" else 1

    def GetItemListInTrack(self, kind, idx):
        return list(self.tracks.get(idx, [])) if kind == "video" else []


def _analysis():
    return {
        "version": 2,
        "program": {"path": "/m/PGM.mov", "width": 1920, "height": 1080},
        "isos": [{"id": "a1", "name": "CAM1", "path": "/m/CAM1.mov", "width": 3840, "height": 2160},
                 {"id": "a2", "name": "CAM2", "path": "/m/CAM2.mov", "width": 3840, "height": 2160}],
        "transitions": [{"edit": 300, "duration": 10, "kind": "dissolve"}],
        "supersources": [{"id": "ss1", "background": "s2", "occurrences": [[300, 900], [2000, 2100]],
                          "boxes": [{"angle": "a2", "layer": 2, "rect": [0.57, 0.275, 0.93, 0.725],
                                     "crop": [0.1, 0, 0.1, 0]},
                                    {"angle": "a1", "layer": 1, "rect": [0.07, 0.275, 0.43, 0.725],
                                     "crop": [0.1, 0, 0.1, 0]}]}],
    }


def test_box_targets(mod):
    t = mod.box_targets(_analysis())
    assert [(x["track"], x["name"], x["lo"], x["hi"]) for x in t] == [
        (3, "CAM1", 288, 912), (3, "CAM1", 1988, 2112), (4, "CAM2", 288, 912), (4, "CAM2", 1988, 2112)]
    assert t[0]["props"]["Pan"] == pytest.approx(-480) and t[2]["props"]["Pan"] == pytest.approx(480)
    assert mod.box_targets({"program": {}, "supersources": _analysis()["supersources"]}) == []


def test_find_box_item(mod):
    tgt = mod.box_targets(_analysis())[0]
    wrong_path = FakeItem(90305, "/m/CAM2.mov")
    outside = FakeItem(90000 + 950, "/m/CAM1.mov")
    good = FakeItem(90306, "/m/CAM1.mov")
    tl = FakeTrackTimeline({1: [], 2: [], 3: [wrong_path, outside, good]})
    assert mod.find_box_item(tl, tgt, 90000) is good
    used = {id(good)}
    assert mod.find_box_item(tl, tgt, 90000, used) is None
    # fallback by name when there is no media pool path; and search on another track above V2
    by_name = FakeItem(90310, None, name="CAM1 box")
    tl2 = FakeTrackTimeline({1: [], 2: [], 3: [], 4: [by_name]})
    assert mod.find_box_item(tl2, tgt, 90000) is by_name


def test_apply_box_fixup(mod):
    a1 = FakeItem(90305, "/m/CAM1.mov")
    a2 = FakeItem(90305, "/m/CAM2.mov")
    a1b = FakeItem(92000, "/m/CAM1.mov", refuse=("CropRetain",))
    bg = FakeItem(90305, "/m/s2.png")
    tl = FakeTrackTimeline({1: [], 2: [bg], 3: [a1, a1b], 4: [a2]})
    applied, missing, failed = mod.apply_box_fixup(tl, _analysis())
    assert (applied, missing, failed) == (3, 1, ["CropRetain"])
    assert [k for k, _ in a1.set] == list(mod.BOX_PROP_ORDER)
    got = dict(a1.set)
    assert got["Pan"] == pytest.approx(-480) and got["CropLeft"] == pytest.approx(192)
    assert dict(a2.set)["Pan"] == pytest.approx(480)
    assert bg.set == []


def test_fixup_supersources_logs_and_never_raises(mod, tmp_path):
    path = tmp_path / "X_analysis.json"
    path.write_text(json.dumps(_analysis()))
    tl = FakeTrackTimeline({1: [], 2: [], 3: [FakeItem(90305, "/m/CAM1.mov")]}, name="X Conform")
    lines = []
    mod.fixup_supersources({"conform": tl, "stacked": object()},
                           {"supersources": 1, "analysis": str(path)}, lines.append)
    assert lines == ["SuperSource boxes on X Conform: 1 applied, 3 not found."]
    lines = []
    mod.fixup_supersources({"multicam": tl}, {"supersources": 1, "analysis": str(tmp_path / "nope.json")},
                           lines.append)
    assert len(lines) == 1 and "skipped" in lines[0]
    lines = []
    mod.fixup_supersources({"multicam": tl}, {"supersources": 0}, lines.append)
    assert lines == []

    class Broken(FakeTrackTimeline):
        def GetItemListInTrack(self, kind, idx):
            raise RuntimeError("boom")
    lines = []
    mod.fixup_supersources({"multicam": Broken({3: []})}, {"supersources": 1, "analysis": str(path)},
                           lines.append)
    assert "0 applied, 4 not found" in lines[0]


def test_missing_stills(mod):
    assert mod.missing_stills(["/o/s/s1.png", "/o/s/s2.png"], ["/o/s/s1.png", "", "/m/CAM1.mov"]) == \
        ["/o/s/s2.png"]
