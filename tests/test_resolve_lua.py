"""Tests for resolve/PGM to Multicam.lua (via lupa, LuaJIT 2.1 when available) and the macOS packaging scripts."""
import importlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time

import pytest

pytest.importorskip("lupa")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LUA_SCRIPT = os.path.join(ROOT, "resolve", "PGM to Multicam.lua")
PY_SCRIPT = os.path.join(ROOT, "resolve", "PGM to Multicam.py")
FAKES = os.path.join(HERE, "lua", "fake_resolve.lua")
FAKE_UI = os.path.join(HERE, "lua", "fake_ui.lua")
FAKE_ENGINE = os.path.join(HERE, "lua", "fake_engine.py")
PKG = os.path.join(ROOT, "packaging", "macos")
SHELL_FILES = [os.path.join(PKG, f) for f in ("Install.command", "Uninstall.command", "make_release.sh")]
FLAVOURS = ("luajit21", "lua51", "lua54")
NASTY = "Shoot 'é\" $HOME `x` 1"


def _flavour_module(names=FLAVOURS):
    for name in names:
        try:
            return name, importlib.import_module("lupa." + name)
        except ImportError:
            continue
    pytest.skip("no Lua runtime in lupa")


def _load(mod, test_flag=True):
    rt = mod.LuaRuntime(unpack_returned_tuples=True)
    if test_flag:
        rt.execute("PGM2MC_TEST = true")
    return rt, rt.eval("dofile")(LUA_SCRIPT)


class Lua(object):
    def __init__(self):
        forced = os.environ.get("PGM2MC_LUA")  # e.g. PGM2MC_LUA=lua54 to run everything on another runtime
        self.flavour, mod = _flavour_module((forced,) if forced else FLAVOURS)
        self.LuaError = mod.LuaError
        self.rt, self.M = _load(mod)
        self.rawequal = self.rt.eval("rawequal")
        self.F = self.rt.eval("dofile")(FAKES)
        self.U = self.rt.eval("dofile")(FAKE_UI)

    def table(self, x):
        if isinstance(x, dict):
            return self.rt.table_from({k: self.table(v) for k, v in x.items()})
        if isinstance(x, (list, tuple)):
            return self.rt.table_from([self.table(v) for v in x])
        return x


def to_py(x):
    if type(x).__name__ != "_LuaTable":  # works for every lupa runtime module
        return x
    items = list(x.items())
    keys = [k for k, _ in items]
    if keys and all(isinstance(k, int) and not isinstance(k, bool) for k in keys) and \
            sorted(keys) == list(range(1, len(keys) + 1)):
        return [to_py(x[i]) for i in range(1, len(keys) + 1)]
    return {k: to_py(v) for k, v in items}


def same(a, b):
    """Deep equality where an empty Lua table matches [] or {} and floats compare exactly."""
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(same(a[k], b[k]) for k in a)
    if (a == [] and b == {}) or (a == {} and b == []):
        return True
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    return a == b


@pytest.fixture(scope="module")
def L():
    return Lua()


@pytest.fixture(scope="module")
def pymod():
    spec = importlib.util.spec_from_file_location("pgm_to_multicam_py", PY_SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def pair(ret):
    """(value, message) from a Lua function returning `v` or `nil, msg`."""
    return ret if isinstance(ret, tuple) else (ret, None)


# ---------------------------------------------------------------- loading

def test_runtime_is_luajit(L):
    print("Lua runtime:", L.flavour, L.rt.eval("_VERSION"), L.rt.eval("jit and jit.version"))
    assert L.flavour in FLAVOURS


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_loads_outside_resolve_in_each_runtime(flavour):
    try:
        mod = importlib.import_module("lupa." + flavour)
    except ImportError:
        pytest.skip(flavour + " not built into lupa")
    rt, M = _load(mod, test_flag=False)  # no resolve/fu: main must not run, module table returned
    assert M.TITLE == "PGM to Multicam"
    assert M.is_program_name("Show PGM") and not M.is_program_name("CAM1")
    p = M.box_props(rt.table_from({"rect": rt.table_from([0.07, 0.275, 0.43, 0.725]),
                                   "crop": rt.table_from([0.1, 0, 0.1, 0])}), 3840, 2160, 1920, 1080)
    assert p.Pan == pytest.approx(-480) and p.ZoomGang is True
    assert to_py(M.json.decode('{"a": [1, 2.5, "\\u00e9"]}')) == {"a": [1, 2.5, "é"]}
    lines = []
    assert M.main(rt.table_from({}), lines.append) is False
    assert "run this from DaVinci Resolve" in lines[0]


# ---------------------------------------------------------------- pure helpers, same answers as the Python script

@pytest.mark.parametrize("name,expected", [
    ("PGM.mov", True), ("Show_Program_Out.mxf", True), ("prog rec", True), ("PVW Out 1", True),
    ("pvw_out", True), ("pvw-out", True), ("PVWOUT", True), ("CAM1.mov", False), ("ISO_wide", False), ("", False),
    ("pvw  out", False),
])
def test_program_name(L, pymod, name, expected):
    assert L.M.is_program_name(name) is expected
    assert pymod.is_program_name(name) is expected


def test_guess_program_index(L, pymod):
    for names in (["CAM1", "CAM2", "Main PGM"], ["CAM1", "CAM2"], ["PGM", "CAM", "program b"], []):
        assert L.M.guess_program_index(L.table(names)) == pymod.guess_program_index(names) + 1
        assert (to_py(L.M.program_candidates(L.table(names))) or []) == \
            [i + 1 for i in pymod.program_candidates(names)]


@pytest.mark.parametrize("t", ["Video + Audio", "Video", "Audio", "Timeline", "Multicam", "Compound", "Still",
                               "", None, " video ", "Fusion Video"])
def test_is_video_type(L, pymod, t):
    assert L.M.is_video_type(t) is pymod.is_video_type(t)


def test_build_argv_matches_python(L, pymod):
    cmd = ["/py", "-u", "-m", "pgm2mc"]
    py = pymod.build_argv("/py", "/m/PGM.mov", ["/m/C1.mov", "/m/C2.mov"], "/out", name="PGM 2",
                          sync="Timecode", audio="Follow camera", min_shot=8, refine=True,
                          ffmpeg="/b/ffmpeg", ffprobe="/b/ffprobe", rebuild={"stills_min_repeat": 4})
    lua = L.M.build_argv(L.table(cmd), "/m/PGM.mov", L.table(["/m/C1.mov", "/m/C2.mov"]), "/out", L.table(
        {"name": "PGM 2", "sync": "Timecode", "audio": "Follow camera", "min_shot": 8, "refine": True,
         "ffmpeg": "/b/ffmpeg", "ffprobe": "/b/ffprobe", "rebuild": {"stills_min_repeat": 4}}))
    assert to_py(lua) == py
    assert to_py(L.M.build_argv(L.table(cmd), "P", L.table(["I"]), "/o")) == pymod.build_argv("/py", "P", ["I"], "/o")
    bundle = to_py(L.M.build_argv(L.table(["/e/pgm2mc"]), "P", L.table(["I"]), "/o",
                                  L.table({"ffmpeg": "/e/bin/ffmpeg", "ffprobe": "/e/bin/ffprobe"})))
    assert bundle == ["/e/pgm2mc", "P", "I", "-o", "/o", "--sync", "auto", "--audio", "pgm", "--min-shot", "6",
                      "--ffmpeg", "/e/bin/ffmpeg", "--ffprobe", "/e/bin/ffprobe"]
    with pytest.raises(L.LuaError, match="sync must be"):
        L.M.build_argv(L.table(cmd), "P", L.table(["I"]), "/o", L.table({"sync": "manual"}))
    with pytest.raises(L.LuaError, match="audio must be"):
        L.M.build_argv(L.table(cmd), "P", L.table(["I"]), "/o", L.table({"audio": "iso"}))


@pytest.mark.parametrize("opts", [
    {}, {"dissolves": False, "stills": False, "stills_min_repeat": 5, "supersource": False},
    {"stills_min_repeat": 3}, {"stills_min_repeat": 99}, {"stills_min_repeat": 0}, {"supersource": False},
])
def test_rebuild_flags_match_python(L, pymod, opts):
    assert (to_py(L.M.rebuild_flags(L.table(opts))) or []) == pymod.rebuild_flags(opts)


def test_shell_quoting_roundtrip(L):
    args = ["/Volumes/My Disk/Show PGM.mov", "it's", 'say "hi"', "café 日本", "$HOME `id` \\n",
            "a\nb", "", "-o", "'", "''x''"]
    joined = L.M.shell_join(L.table(args))
    out = subprocess.run(["sh", "-c", "printf '%s\\0' " + joined], stdout=subprocess.PIPE, check=True).stdout
    assert out.decode("utf-8").split("\0")[:-1] == args
    assert L.M.shell_quote("it's") == "'it'\\''s'"


def test_parse_line(L, pymod):
    for line in ("PROGRESS sync 40\n", "PROGRESS frames 140", "PROGRESS match -5", "PROGRESS write 33.9",
                 "PROGRESS write x", "PROGRESS solo", "WARN low audio level", "ERROR no overlap", "  hello \r\n",
                 "RESULT {broken", "RESULT [1, 2]", "hello"):
        kind, payload = L.M.parse_line(line)
        pk, pp = pymod.parse_line(line)
        if pk == "result":
            continue
        assert kind == pk, line
        assert (to_py(payload) == list(pp)) if kind == "progress" else (payload == pp), line
    kind, data = L.M.parse_line('RESULT {"multicam": "/o/a b.fcpxml", "segments": 3, "warnings": []}')
    assert kind == "result" and data.segments == 3 and data.multicam == "/o/a b.fcpxml"
    assert L.M.progress_text("sync", 40) == "Syncing… 40%"
    assert L.M.progress_text("stills", 5) == pymod.progress_text("stills", 5) == "Stills… 5%"


def test_result_summary_and_events(L, pymod):
    for res in ({"transitions": 12, "stills": 3, "supersources": 2}, {"transitions": 1, "stills": 1,
                                                                       "supersources": 1},
                {"segments": 3}, {"transitions": "x", "stills": 0}):
        assert L.M.result_summary(L.table(res)) == pymod.result_summary(res)
        assert L.M.describe_event("result", L.table(res)) == pymod.describe_event("result", res)
    assert L.M.describe_event("warn", "w") == "Warning: w" and L.M.describe_event("error", "e") == "Error: e"
    assert L.M.describe_event("progress", L.table(["sync", 1])) is None


def test_names_paths_and_plan(L, pymod, tmp_path):
    for base, existing in (("PGM", []), ("PGM", ["PGM Conform"]), ("PGM", ["PGM Stacked", "PGM 2 Multicam Cut"])):
        assert L.M.unique_base_name(base, L.table(existing)) == pymod.unique_base_name(base, existing)
    res = {"multicam": "/o/m.fcpxml", "conform": "/o/c.fcpxml", "stacked": "/o/s.fcpxml"}
    wanted = {"multicam": True, "conform": False, "stacked": True}
    assert to_py(L.M.planned_imports(L.table(res), "PGM", L.table(wanted))) == \
        [list(x) for x in pymod.planned_imports(res, "PGM", wanted)]
    for p in ("/a/b/PGM.mov", "/a/b/PGM.tar.gz", "/a/.hidden", "noext", "/x/a."):
        assert L.M.file_stem(p) == pymod.file_stem(p), p
    for p in ("/a//b/./c/../d.mov", "/a/b/", "a/../../b", "/../x"):
        assert L.M.norm_path(p) == os.path.normpath(p), p
    stills = ["/o/s/s1.png", "/o/s/s2.png"]
    pool = ["/o/s/s1.png", "", "/m/CAM1.mov"]
    assert to_py(L.M.missing_stills(L.table(stills), L.table(pool))) == pymod.missing_stills(stills, pool)
    pgm = tmp_path / "shoot" / "PGM.mov"
    pgm.parent.mkdir()
    pgm.write_bytes(b"")
    assert L.M.default_output_dir(str(pgm)) == str(tmp_path / "shoot" / "pgm2multicam")
    assert L.M.default_output_dir(str(pgm), "/home/x", lambda d: False) == "/home/x/Movies/pgm2multicam"
    assert L.M.default_output_dir("", "/h") == "/h/Movies/pgm2multicam"
    assert L.M.app_dir("darwin", L.table({}), "/Users/e") == "/Users/e/Library/Application Support/pgm2multicam"


# ---------------------------------------------------------------- JSON decoder

def test_json_decoder_basics(L):
    d = L.M.json.decode
    text = ('{"s": "a\\"b\\\\c\\/d\\n\\t\\r\\b\\f", "u": "\\u00e9\\u65e5\\ud83c\\udfac", "raw": "café", '
            '"n": [0, -1, 2.5, -0.25, 1e3, 1.5E-2, 12345678901], "t": true, "f": false, "z": null, '
            '"arr": [null, 1, null], "nested": {"a": {"b": [[], {}]}}, "empty": ""}')
    raw = d(text)
    got = to_py(raw)
    want = json.loads(text)
    want.pop("z")
    got.pop("arr")
    want.pop("arr")
    assert same(got, want)
    assert raw.arr[2] == 1 and L.rawequal(raw.arr[1], L.M.json.null) and L.rawequal(raw.arr[3], L.M.json.null)
    assert d("  42 ") == 42 and d('"x"') == "x" and d("[]") is not None
    for bad in ('{"a": 1', '[1, 2', '{"a" 1}', '"abc', "tru", "{} x", '{"a": 01x}', '"\\x"', '"\\u12"', ""):
        with pytest.raises(L.LuaError, match="JSON error"):
            d(bad)


@pytest.fixture(scope="module")
def real_cli_run(tmp_path_factory):
    sys.path.insert(0, HERE)
    import gen_media
    ffmpeg = gen_media.ffmpeg_exe()
    os.environ["PGM2MC_FFMPEG"] = ffmpeg
    with open(gen_media.generate_c("C")) as f:
        truth = json.load(f)
    out = str(tmp_path_factory.mktemp("lua_cli") / "out dir")
    args = [truth["pgm"]["path"]] + [truth["isos"][c]["path"] for c in ("cam1", "cam2", "cam3")] + ["-o", out]
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "engine"), PGM2MC_FFMPEG=ffmpeg)
    p = subprocess.run([sys.executable, "-m", "pgm2mc"] + args, env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    return p.stdout.decode("utf-8"), p.stderr.decode("utf-8")


def test_json_decoder_on_real_cli_output(L, real_cli_run):
    stdout, stderr = real_cli_run
    last = stdout.strip().splitlines()[-1]
    kind, res = L.M.parse_line(last)
    assert kind == "result"
    assert same(to_py(res), json.loads(last[len("RESULT "):]))
    with open(res.analysis, encoding="utf-8") as fh:
        text = fh.read()
    analysis = json.loads(text)
    got = to_py(L.M.json.decode(text))
    assert analysis["version"] == 2 and analysis["transitions"]
    assert same(got, analysis)
    for ln in stderr.splitlines():
        k, payload = L.M.parse_line(ln)
        assert k in ("progress", "warn"), ln


# ---------------------------------------------------------------- SuperSource box geometry

BOXES = [
    ({"rect": [0.07, 0.275, 0.43, 0.725], "crop": [0.1, 0, 0.1, 0]}, 3840, 2160, 1920, 1080),
    ({"rect": [0.57, 0.275, 0.93, 0.725], "crop": [0.1, 0, 0.1, 0]}, 3840, 2160, 1920, 1080),
    ({"rect": [0, 0, 0.5, 0.5], "crop": [0, 0, 0, 0]}, 1920, 1080, 1920, 1080),
    ({"rect": [0, 0, 0.5, 0.25]}, 1920, 1080, 1920, 1080),
    ({"rect": [0.6, 0.55, 0.95, 0.95], "crop": [0.05, 0.1, 0.2, 0.0]}, 1280, 720, 1920, 1080),
    ({"rect": [0.1, 0.1, 0.4, 0.9], "crop": [0.6, 0.0, 0.6, 0.3]}, 1920, 1080, 3840, 2160),
    ({"rect": [0.2, 0.3, 0.45, 0.6], "crop": [0, 0.05, 0, 0.05]}, 1440, 1080, 1920, 1080),
]


@pytest.mark.parametrize("case", range(len(BOXES)))
def test_box_props_equal_python(L, pymod, case):
    box, iw, ih, sw, sh = BOXES[case]
    want = pymod.box_props(box, iw, ih, sw, sh)
    got = to_py(L.M.box_props(L.table(box), iw, ih, sw, sh))
    assert set(got) == set(want)
    for k, v in want.items():
        if isinstance(v, bool):
            assert got[k] is v, k
        else:
            assert got[k] == pytest.approx(v, rel=1e-12, abs=1e-9), k


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


def test_box_targets_match_python(L, pymod):
    want = pymod.box_targets(_analysis())
    got = to_py(L.M.box_targets(L.table(_analysis())))
    assert [(x["track"], x["name"], x["lo"], x["hi"], x["path"]) for x in got] == \
        [(x["track"], x["name"], x["lo"], x["hi"], x["path"]) for x in want]
    assert to_py(L.M.box_targets(L.table({"program": {}, "supersources": _analysis()["supersources"]}))) in ([], {})


def test_find_and_apply_box_fixup(L, pymod):
    F, M = L.F, L.M
    tgt = M.box_targets(L.table(_analysis()))[1]
    good = F.item(90306, "/m/CAM1.mov")
    tl = F.track_timeline(L.table({1: [], 2: [], 3: [F.item(90305, "/m/CAM2.mov"), F.item(90950, "/m/CAM1.mov"),
                                                    good]}))
    assert L.rawequal(M.find_box_item(tl, tgt, 90000), good)
    used = L.rt.table()
    used[good] = True
    assert M.find_box_item(tl, tgt, 90000, used) is None
    by_name = F.item(90310, None, "CAM1 box")
    assert L.rawequal(M.find_box_item(F.track_timeline(L.table({1: [], 2: [], 3: [], 4: [by_name]})), tgt, 90000),
                      by_name)

    a1, a2 = F.item(90305, "/m/CAM1.mov"), F.item(90305, "/m/CAM2.mov")
    a1b = F.item(92000, "/m/CAM1.mov", None, L.table(["CropRetain"]))
    bg = F.item(90305, "/m/s2.png")
    tl = F.track_timeline(L.table({1: [], 2: [bg], 3: [a1, a1b], 4: [a2]}))
    applied, missing, failed = M.apply_box_fixup(tl, L.table(_analysis()))
    assert (applied, missing, to_py(failed)) == (3, 1, ["CropRetain"])
    sets = to_py(a1.set)
    assert [k for k, _ in sets] == list(pymod.BOX_PROP_ORDER)
    got = dict(sets)
    assert got["Pan"] == pytest.approx(-480) and got["CropLeft"] == pytest.approx(192)
    assert got["ZoomGang"] is True and got["CropRetain"] is True and got["Scaling"] == 2
    assert dict(to_py(a2.set))["Pan"] == pytest.approx(480)
    assert to_py(bg.set) in ([], {})


def test_fixup_supersources_logs_and_never_raises(L, tmp_path):
    F, M = L.F, L.M
    path = tmp_path / "X_analysis.json"
    path.write_text(json.dumps(_analysis()))
    tl = F.track_timeline(L.table({1: [], 2: [], 3: [F.item(90305, "/m/CAM1.mov")]}), 90000, "X Conform")
    lines = []
    M.fixup_supersources(L.table({"conform": tl, "stacked": L.rt.table()}),
                         L.table({"supersources": 1, "analysis": str(path)}), lines.append)
    assert lines == ["SuperSource boxes on X Conform: 1 applied, 3 not found."]
    lines = []
    M.fixup_supersources(L.table({"multicam": tl}), L.table({"supersources": 1, "analysis": str(tmp_path / "no")}),
                         lines.append)
    assert len(lines) == 1 and "skipped" in lines[0]
    lines = []
    M.fixup_supersources(L.table({"multicam": tl}), L.table({"supersources": 0}), lines.append)
    assert lines == []
    broken = F.track_timeline(L.table({3: []}), 90000, "B", True)
    lines = []
    M.fixup_supersources(L.table({"multicam": broken}), L.table({"supersources": 1, "analysis": str(path)}),
                         lines.append)
    assert "0 applied, 4 not found" in lines[0]


# ---------------------------------------------------------------- engine resolution

def _write_exe(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(body)
    os.chmod(path, 0o755)


def _fake_bundle(folder):
    """Engine bundle whose pgm2mc runs tests/lua/fake_engine.py."""
    q = lambda s: "'" + s.replace("'", "'\\''") + "'"  # noqa: E731
    _write_exe(os.path.join(folder, "pgm2mc"), "#!/bin/sh\nexec %s %s \"$@\"\n" % (q(sys.executable), q(FAKE_ENGINE)))
    for tool in ("ffmpeg", "ffprobe"):
        _write_exe(os.path.join(folder, "bin", tool), "#!/bin/sh\nexit 0\n")
    os.makedirs(os.path.join(folder, "_internal"), exist_ok=True)
    return folder


def _legacy_engine(folder):
    pkg = os.path.join(folder, "pgm2mc")
    os.makedirs(pkg)
    open(os.path.join(pkg, "__init__.py"), "w").close()
    with open(os.path.join(pkg, "__main__.py"), "w") as fh:
        fh.write("import runpy\nrunpy.run_path(%r, run_name='__main__')\n" % FAKE_ENGINE)
    return folder


def test_load_config(L, tmp_path):
    M = L.M
    app = tmp_path / "app"
    app.mkdir()
    engine, msg = pair(M.load_config(str(app)))
    assert engine is None and "not installed" in msg and "Run Install.command" in msg
    default = _fake_bundle(str(app / "engine" / "pgm2mc"))
    engine, _ = pair(M.load_config(str(app)))
    assert engine.kind == "bundle" and to_py(engine.cmd) == [default + "/pgm2mc"]
    assert engine.ffmpeg == default + "/bin/ffmpeg" and engine.ffprobe == default + "/bin/ffprobe"

    custom = _fake_bundle(str(tmp_path / ("custom " + NASTY)))
    (app / "config.json").write_text(json.dumps({"engine": custom + "/"}), encoding="utf-8")
    engine, _ = pair(M.load_config(str(app)))
    assert engine.dir == custom and engine.exe == custom + "/pgm2mc"

    (app / "config.json").write_text("﻿" + json.dumps({"engine": str(tmp_path / "nope")}), encoding="utf-8")
    engine, _ = pair(M.load_config(str(app)))
    assert engine.dir == default  # broken config entry falls back to the default bundle
    shutil.rmtree(default)
    engine, msg = pair(M.load_config(str(app)))
    assert engine is None and "config.json points to" in msg and "Run Install.command" in msg

    legacy = _legacy_engine(str(tmp_path / "legacy eng"))
    (app / "config.json").write_text(json.dumps({"python": sys.executable, "engine_dir": legacy, "ffmpeg": "/f",
                                                 "ffprobe": "/p"}))
    engine, _ = pair(M.load_config(str(app)))
    assert engine.kind == "python" and to_py(engine.cmd) == [sys.executable, "-u", "-m", "pgm2mc"]
    assert engine.env.PYTHONPATH == legacy and engine.ffmpeg == "/f" and engine.ffprobe == "/p"

    (app / "config.json").write_text("{not json")
    engine, msg = pair(M.load_config(str(app)))
    assert engine is None and msg.startswith("Cannot read")


# ---------------------------------------------------------------- fake-Resolve headless runs (real background shell)

def _media(L, tmp_path):
    media = tmp_path / NASTY
    media.mkdir()
    clips = []
    for n in ("CAM1 é", "Show PGM", "CAM2 'b'"):
        p = media / (n + ".mov")
        p.write_bytes(b"")
        clips.append(L.F.clip(n + ".mov", str(p)))
    clips.append(L.F.clip("Music.wav", str(media / "Music.wav"), "Audio"))
    clips.append(L.F.clip("Old Timeline", "", "Timeline"))
    return media, clips


def _run(L, project, engine, mode=""):
    os.environ["FAKE_ENGINE_MODE"] = mode
    lines = []
    try:
        ok = L.M.run_headless(L.F.resolve(project), engine, lambda s: time.sleep(0.02), lines.append)
    finally:
        os.environ.pop("FAKE_ENGINE_MODE", None)
    return ok, lines


@pytest.fixture
def bundle_engine(L, tmp_path):
    app = tmp_path / "app"
    _fake_bundle(str(app / "engine" / "pgm2mc"))
    engine, msg = pair(L.M.load_config(str(app)))
    assert engine is not None, msg
    return engine


def test_headless_smoke(L, tmp_path, bundle_engine):
    media, clips = _media(L, tmp_path)
    project = L.F.project(L.table(clips))
    ok, lines = _run(L, project, bundle_engine)
    text = "\n".join(str(x) for x in lines)
    assert ok, text
    outdir = media / "pgm2multicam"
    rec = json.loads((outdir / "argv.json").read_text(encoding="utf-8"))
    argv = rec["argv"]
    assert argv[0] == str(media / "Show PGM.mov")
    assert argv[1:3] == [str(media / "CAM1 é.mov"), str(media / "CAM2 'b'.mov")]
    assert argv[argv.index("--name") + 1] == "Show PGM" and argv[argv.index("-o") + 1] == str(outdir)
    assert argv[argv.index("--ffmpeg") + 1] == bundle_engine.ffmpeg
    assert argv[argv.index("--ffprobe") + 1] == bundle_engine.ffprobe
    assert rec["env"]["PYTHONPATH"] is None and rec["env"]["PYTHONHOME"] is None
    for flag in ("--no-dissolves", "--no-stills", "--no-supersource", "--stills-min-repeat", "--refine"):
        assert flag not in argv
    assert lines[0].startswith("PGM to Multicam: program = Show PGM.mov; ISOs = CAM1 é.mov, CAM2 'b'.mov")
    assert "Syncing… 50%" in lines and "Syncing… 100%" in lines
    assert lines.count("Syncing… 50%") == 1
    assert "Warning: fake warning é" in lines and "some log line" in lines
    assert "Engine finished: 4 segments; 12 transitions, 3 stills, 0 SuperSource layouts." in lines
    imports = to_py(project.mp.imports)
    assert [(i["path"], i["timelineName"]) for i in imports] == [
        (str(outdir / "Show PGM_multicam.fcpxml"), "Show PGM Multicam Cut"),
        (str(outdir / "Show PGM_conform.fcpxml"), "Show PGM Conform")]
    assert all(i["importSourceClips"] is True and i["bin"] == "PGM to Multicam" for i in imports)
    assert "Created timeline: Show PGM Multicam Cut" in lines and "Created timeline: Show PGM Conform" in lines
    assert project.current_tl.GetName(project.current_tl) == "Show PGM Multicam Cut"
    assert L.M.COMPOUND_HINT not in lines
    sdir = outdir / "Show PGM_stills"
    assert to_py(project.mp.media_imports) == [{"paths": [str(sdir / "s1.png"), str(sdir / "s2.png")],
                                               "bin": "PGM to Multicam"}]
    assert "Imported 2 of 2 still image(s) missing from the Media Pool." in lines
    assert "SuperSource" not in text.replace("SuperSource layouts", "")
    assert (outdir / "Show PGM_engine_exit.txt").read_text().strip() == "0"
    assert "RESULT" in (outdir / "Show PGM_engine_stdout.log").read_text(encoding="utf-8")


def test_headless_supersource_fixup(L, tmp_path, bundle_engine):
    media, clips = _media(L, tmp_path)
    cam1 = str(media / "CAM1 é.mov")
    made = []

    def make_timeline(name):
        tl = L.F.track_timeline(L.table({1: [], 2: [], 3: [L.F.item(90305, cam1)]}), 90000, name)
        made.append(tl)
        return tl
    project = L.F.project(L.table(clips), L.table({"make_timeline": make_timeline}))
    ok, lines = _run(L, project, bundle_engine, mode="ss")
    assert ok, lines
    assert "SuperSource boxes on Show PGM Multicam Cut: 1 applied, 0 not found." in lines
    assert "SuperSource boxes on Show PGM Conform: 1 applied, 0 not found." in lines
    sets = dict(to_py(made[0].tracks[3][1].set))
    assert sets["Pan"] == pytest.approx(-480) and sets["ZoomX"] == pytest.approx(0.45)


def test_headless_import_failure_and_name_clash(L, tmp_path, bundle_engine):
    media, clips = _media(L, tmp_path)
    p = media / "PGM.mov"
    p.write_bytes(b"")
    clips[1] = L.F.clip("PGM.mov", str(p))
    project = L.F.project(L.table(clips), L.table({"fail": ["_multicam.fcpxml"]}))
    ok, lines = _run(L, project, bundle_engine)
    text = "\n".join(lines)
    assert ok, text
    assert "Import failed" in text and "File > Import > Timeline" in text
    assert project.current_tl.GetName(project.current_tl) == "PGM 2 Conform"
    assert L.M.COMPOUND_HINT not in lines


def test_headless_compound_hint_and_bin_fallback(L, tmp_path, bundle_engine):
    _, clips = _media(L, tmp_path)
    project = L.F.project(L.table(clips), L.table({"mc_type": "Compound", "no_selection_api": True}))
    ok, lines = _run(L, project, bundle_engine)
    assert ok and L.M.COMPOUND_HINT in lines


def test_headless_requires_single_program(L, tmp_path, bundle_engine):
    _, clips = _media(L, tmp_path)
    ok, lines = _run(L, L.F.project(L.table([clips[0], clips[2]])), bundle_engine)
    assert not ok and "no program clip found among 2 video clip(s) (selection)" in lines[-1]
    extra = L.F.clip("Program B.mov", "/x/Program B.mov")
    ok, lines = _run(L, L.F.project(L.table(clips + [extra])), bundle_engine)
    assert not ok and "more than one clip looks like the program: Show PGM.mov, Program B.mov" in lines[-1]
    ok, lines = _run(L, L.F.project(L.table([clips[1]])), bundle_engine)
    assert not ok and "no ISO clips selected" in lines[-1]


def test_headless_engine_failure(L, tmp_path, bundle_engine):
    _, clips = _media(L, tmp_path)
    project = L.F.project(L.table(clips))
    ok, lines = _run(L, project, bundle_engine, mode="fail")
    assert not ok
    assert "Error: no overlap between PGM and ISOs" in lines
    assert lines[-1] == "PGM to Multicam: engine failed (exit code 1)."
    assert to_py(project.mp.imports) in ([], {})


def test_headless_missing_engine_executable(L, tmp_path, bundle_engine):
    _, clips = _media(L, tmp_path)
    os.remove(bundle_engine.exe)
    ok, lines = _run(L, L.F.project(L.table(clips)), bundle_engine)
    assert not ok and "engine failed (exit code 127)" in lines[-1]


def test_headless_legacy_python_engine(L, tmp_path):
    media, clips = _media(L, tmp_path)
    app = tmp_path / "app"
    app.mkdir()
    legacy = _legacy_engine(str(tmp_path / "legacy eng"))
    (app / "config.json").write_text(json.dumps({"python": sys.executable, "engine_dir": legacy}))
    engine, msg = pair(L.M.load_config(str(app)))
    assert engine is not None, msg
    ok, lines = _run(L, L.F.project(L.table(clips)), engine)
    assert ok, lines
    rec = json.loads((media / "pgm2multicam" / "argv.json").read_text(encoding="utf-8"))
    assert rec["env"]["PYTHONPATH"] == legacy and rec["env"]["PYTHONUNBUFFERED"] == "1"
    assert "--ffmpeg" not in rec["argv"]


def test_headless_without_engine_reports_install_hint(L, tmp_path, monkeypatch):
    _, clips = _media(L, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    lines = []
    assert L.M.run_headless(L.F.resolve(L.F.project(L.table(clips))), None, None, lines.append) is False
    assert "Run Install.command" in lines[-1]


def test_main_without_resolve(L):
    lines = []
    assert L.M.main(L.rt.table(), lines.append) is False
    assert lines == ["PGM to Multicam: run this from DaVinci Resolve (Workspace > Scripts)."]


def test_main_headless_when_no_ui(L, tmp_path, monkeypatch):
    _, clips = _media(L, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    lines = []
    ns = L.table({"resolve": L.F.resolve(L.F.project(L.table(clips)))})
    assert L.M.main(ns, lines.append) is False
    assert "Run Install.command" in lines[-1]


def _dialog_env(L, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    _fake_bundle(str(tmp_path / "xdg" / "pgm2multicam" / "engine" / "pgm2mc"))
    media, clips = _media(L, tmp_path)
    return media, L.F.project(L.table(clips))


@pytest.mark.parametrize("with_timer", [True, False])
def test_dialog_run_and_close(L, tmp_path, monkeypatch, with_timer):
    media, project = _dialog_env(L, tmp_path, monkeypatch)
    seen = {}

    def script(disp, win, itm, ui):
        assert win.props.WindowTitle == "PGM to Multicam" and win.shown
        assert win.root.kind == "VGroup"
        assert to_py(itm.Pgm.entries) == ["CAM1 \u00e9.mov", "Show PGM.mov", "CAM2 'b'.mov"]
        assert itm.Pgm.CurrentIndex == 1
        assert itm.Iso2.Checked is False and itm.Iso2.Enabled is False and itm.Iso1.Checked is True
        assert itm.OutDir.Text == str(media / "pgm2multicam")
        assert to_py(itm.Sync.entries) == ["Auto", "Timecode", "Audio"]
        assert itm.MinShot.Value == 6 and itm.RbMinRepeat.Value == 2 and itm.ImpStacked.Checked is False
        assert itm.Refine.Text == "Refine sync \u00b13 frames"
        assert "3 video clip(s) from the Media Pool selection." in itm.Log.PlainText
        itm.Sync.CurrentIndex = 1
        itm.Audio.CurrentIndex = 1
        itm.RbSuperSource.Checked = False
        itm.RbMinRepeat.Value = 3
        itm.ImpStacked.Checked = True
        itm.Iso3.Checked = False
        win.On.Run.Clicked(None)
        if with_timer:
            assert ui.timer.running and itm.Run.Enabled is False
            deadline = time.time() + 30
            while ui.timer.running and time.time() < deadline:
                disp.On.PollTimer.Timeout(None)
                time.sleep(0.02)
        seen["progress"] = itm.Progress.Text
        seen["log"] = itm.Log.PlainText
        seen["run_enabled"] = itm.Run.Enabled
        win.On.Close.Clicked(None)
        assert disp.exited

    fusion, bmd = L.U.new(with_timer, script)
    lines = []
    assert L.M.main(L.table({"resolve": L.F.resolve(project), "fu": fusion, "bmd": bmd}), lines.append) is True
    assert seen["progress"] == "Done: 3 timeline(s) imported." and seen["run_enabled"] is True
    rec = json.loads((media / "pgm2multicam" / "argv.json").read_text(encoding="utf-8"))
    argv = rec["argv"]
    assert argv[1:argv.index("-o")] == [str(media / "CAM1 \u00e9.mov")]
    for a, b in (("--sync", "timecode"), ("--audio", "follow"), ("--stills-min-repeat", "3")):
        assert argv[argv.index(a) + 1] == b
    assert "--no-supersource" in argv and "--refine" not in argv
    names = [i["timelineName"] for i in to_py(project.mp.imports)]
    assert names == ["Show PGM Multicam Cut", "Show PGM Conform", "Show PGM Stacked"]
    assert "Running engine: program Show PGM.mov, 1 ISO(s), output " in seen["log"]
    assert "Warning: fake warning \u00e9" in seen["log"]
    assert any(ln.startswith("PGM to Multicam: Created timeline: Show PGM Stacked") for ln in lines)


def test_dialog_failure_falls_back_to_headless(L, tmp_path, monkeypatch):
    _, project = _dialog_env(L, tmp_path, monkeypatch)
    lines = []
    ns = L.table({"resolve": L.F.resolve(project), "fu": L.table({"UIManager": L.table({})}),
                  "bmd": L.table({})})
    assert L.M.main(ns, lines.append) is True
    assert "dialog unavailable" in lines[0]
    assert "Created timeline: Show PGM Multicam Cut" in lines


# ---------------------------------------------------------------- packaging scripts

@pytest.mark.parametrize("path", SHELL_FILES)
def test_shell_syntax(path):
    subprocess.run(["bash", "-n", path], check=True)
    assert os.stat(path).st_mode & stat.S_IXUSR


def test_shellcheck():
    sc = shutil.which("shellcheck") or os.path.join(os.path.dirname(sys.executable), "shellcheck")
    if not os.path.exists(sc):
        pytest.skip("shellcheck not installed")
    p = subprocess.run([sc, "-x"] + SHELL_FILES, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert p.returncode == 0, p.stdout.decode()


def _release_folder(tmp_path, help_ok=True):
    rel = tmp_path / "rel" / "PGM to Multicam"
    rel.mkdir(parents=True)
    for f in ("Install.command", "Uninstall.command"):
        shutil.copy2(os.path.join(PKG, f), rel / f)
    shutil.copy2(LUA_SCRIPT, rel / "PGM to Multicam.lua")
    eng = rel / "engine" / "pgm2mc"
    _write_exe(str(eng / "pgm2mc"), "#!/bin/sh\n[ \"$1\" = --help ] && { echo usage; exit %d; }\nexit 3\n"
               % (0 if help_ok else 1))
    for tool in ("ffmpeg", "ffprobe"):
        _write_exe(str(eng / "bin" / tool), "#!/bin/sh\nexit 0\n")
    (eng / "_internal").mkdir()
    (eng / "_internal" / "lib.txt").write_text("x")
    return rel


def _sh(script, home):
    env = dict(os.environ, HOME=str(home))
    return subprocess.run(["bash", str(script)], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=60)


def test_install_and_uninstall_dry_run(tmp_path):
    home = tmp_path / ("home " + NASTY)
    rel = _release_folder(tmp_path)
    app = home / "Library" / "Application Support" / "pgm2multicam"
    scripts = home / "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
    scripts.mkdir(parents=True)
    (scripts / "PGM to Multicam.py").write_text("# old")
    (scripts / "Other.lua").write_text("-- keep")
    (app / "venv").mkdir(parents=True)  # left over from the Python install: must survive
    (app / "engine" / "pgm2mc").mkdir(parents=True)
    (app / "engine" / "pgm2mc" / "__init__.py").write_text("")  # old Python engine: replaced

    p = _sh(rel / "Install.command", home)
    out = p.stdout.decode()
    assert p.returncode == 0, out
    assert "SUCCESS" in out and "PGM to Multicam.py.disabled" in out
    eng = app / "engine" / "pgm2mc"
    assert os.access(eng / "pgm2mc", os.X_OK) and (eng / "_internal" / "lib.txt").is_file()
    assert not (eng / "__init__.py").exists()
    assert json.loads((app / "config.json").read_text()) == {"engine": str(eng)}
    assert (scripts / "PGM to Multicam.lua").read_bytes() == open(LUA_SCRIPT, "rb").read()
    assert (scripts / "PGM to Multicam.py.disabled").read_text() == "# old"
    assert not (scripts / "PGM to Multicam.py").exists()
    assert sorted(os.listdir(app / "engine")) == ["pgm2mc"]

    p = _sh(rel / "Install.command", home)  # re-run = update
    assert p.returncode == 0 and "SUCCESS" in p.stdout.decode()
    assert sorted(os.listdir(app / "engine")) == ["pgm2mc"]

    p = _sh(rel / "Uninstall.command", home)
    out = p.stdout.decode()
    assert p.returncode == 0, out
    assert "DONE" in out and "disabled" in out
    assert not (scripts / "PGM to Multicam.lua").exists() and not (app / "config.json").exists()
    assert not (app / "engine").exists()
    assert (app / "venv").is_dir() and (scripts / "Other.lua").is_file()
    assert (scripts / "PGM to Multicam.py.disabled").is_file()


def test_install_keeps_old_engine_when_new_one_fails(tmp_path):
    home = tmp_path / "home"
    good = _release_folder(tmp_path / "good")
    assert _sh(good / "Install.command", home).returncode == 0
    bad = _release_folder(tmp_path / "bad", help_ok=False)
    (bad / "engine" / "pgm2mc" / "_internal" / "lib.txt").write_text("new")
    p = _sh(bad / "Install.command", home)
    assert p.returncode == 1 and "INSTALL FAILED" in p.stdout.decode()
    eng = home / "Library" / "Application Support" / "pgm2multicam" / "engine"
    assert sorted(os.listdir(eng)) == ["pgm2mc"]
    assert (eng / "pgm2mc" / "_internal" / "lib.txt").read_text() == "x"
    p = _sh(tmp_path / "good" / "rel" / "PGM to Multicam" / "Uninstall.command", home)
    assert p.returncode == 0


def test_make_release(tmp_path):
    bundle = tmp_path / "dist" / "pgm2mc"
    _write_exe(str(bundle / "pgm2mc"), "#!/bin/sh\necho usage\n")
    for tool in ("ffmpeg", "ffprobe"):
        _write_exe(str(bundle / "bin" / tool), "#!/bin/sh\nexit 0\n")
    (bundle / "_internal").mkdir()
    os.symlink("../bin/ffmpeg", str(bundle / "_internal" / "ffmpeg-link"))
    env = dict(os.environ)
    if not shutil.which("zip") and not shutil.which("ditto"):
        # stand-in zip: records what it was given and copies the staged folder next to the zip
        stub = tmp_path / "stub"
        _write_exe(str(stub / "zip"), "#!/bin/sh\nzip_out=\"$2\"; shift 2\ncp -RPp \"$1\" \"$zip_out.d\"\n"
                   "echo zipped > \"$zip_out\"\n")
        env["PATH"] = str(stub) + os.pathsep + env["PATH"]
    out = tmp_path / "out"
    p = subprocess.run(["bash", os.path.join(PKG, "make_release.sh"), str(bundle), "1.2.3", str(out)], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert p.returncode == 0, p.stdout.decode()
    zips = [f for f in os.listdir(out) if f.endswith(".zip")]
    assert len(zips) == 1 and zips[0].startswith("PGM-to-Multicam-macos-") and zips[0].endswith("-1.2.3.zip")
    zpath = out / zips[0]
    staged = out / (zips[0] + ".d")
    if staged.is_dir():
        root = staged
    else:
        import zipfile
        root = tmp_path / "unzipped"
        with zipfile.ZipFile(zpath) as zf:
            names = zf.namelist()
        assert all(n.startswith("PGM to Multicam/") for n in names)
        subprocess.run(["unzip", "-q", str(zpath), "-d", str(root)], check=True)
        root = root / "PGM to Multicam"
    for f in ("Install.command", "Uninstall.command", "README.txt", "PGM to Multicam.lua"):
        assert (root / f).is_file(), f
    assert os.access(root / "Install.command", os.X_OK) and os.access(root / "Uninstall.command", os.X_OK)
    assert os.access(root / "engine" / "pgm2mc" / "pgm2mc", os.X_OK)
    assert os.path.islink(root / "engine" / "pgm2mc" / "_internal" / "ffmpeg-link")
    p = subprocess.run(["bash", os.path.join(PKG, "make_release.sh"), str(tmp_path / "missing")],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert p.returncode != 0
