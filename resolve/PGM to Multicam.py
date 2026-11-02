#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PGM to Multicam: DaVinci Resolve script (Workspace > Scripts > PGM to Multicam).

Pick the live-cut program clip and the ISO clips in the Media Pool, run the pgm2multicam engine in a
subprocess, and import the resulting FCPXML timelines. Stdlib only, Python 3.6+.
"""
from __future__ import print_function

import io
import json
import os
import re
import subprocess
import sys
import threading
import time

try:
    import queue
except ImportError:  # pragma: no cover
    import Queue as queue  # noqa: N813

TITLE = "PGM to Multicam"
BIN_NAME = "PGM to Multicam"
PROGRAM_RE = re.compile(r"pgm|program|prog|pvw[ _-]?out", re.IGNORECASE)
SYNC_CHOICES = ["Auto", "Timecode", "Audio"]
AUDIO_CHOICES = ["Program audio", "Follow camera"]
STAGE_LABELS = {
    "probe": "Probing",
    "sync": "Syncing",
    "frames": "Reading frames",
    "match": "Matching",
    "write": "Writing",
}
TIMELINE_SUFFIX = {"multicam": " Multicam Cut", "conform": " Conform", "stacked": " Stacked"}
IMPORT_ORDER = ["multicam", "conform", "stacked"]
COMPOUND_HINT = ("Tip: if the multicam arrives as a compound clip, right-click it in the Media Pool > "
                 "\"Convert Compound Clips to Multicam Clips\" (or use the Conform timeline).")
NON_VIDEO_TYPES = ("timeline", "multicam", "compound", "fusion", "generator", "still", "subtitle")


class EngineNotInstalled(Exception):
    pass


# ---------------------------------------------------------------- pure helpers (tested without Resolve)

def is_program_name(name):
    return bool(PROGRAM_RE.search(name or ""))


def program_candidates(names):
    """Indices of names that look like the program recording."""
    return [i for i, n in enumerate(names) if is_program_name(n)]


def guess_program_index(names):
    idx = program_candidates(names)
    return idx[0] if idx else 0


def is_video_type(clip_type):
    t = (clip_type or "").strip().lower()
    if not t or "video" not in t:
        return False
    return not any(bad in t for bad in NON_VIDEO_TYPES)


def file_stem(path):
    return os.path.splitext(os.path.basename(path))[0]


def default_output_dir(pgm_path, home=None, writable=None):
    """A 'pgm2multicam' folder next to the PGM file, else ~/Movies/pgm2multicam."""
    writable = writable or (lambda d: os.path.isdir(d) and os.access(d, os.W_OK))
    home = home or os.path.expanduser("~")
    if pgm_path:
        parent = os.path.dirname(os.path.abspath(pgm_path))
        target = os.path.join(parent, "pgm2multicam")
        if (os.path.isdir(target) and writable(target)) or writable(parent):
            return target
    return os.path.join(home, "Movies", "pgm2multicam")


def app_dir(platform=None, env=None, home=None):
    platform = platform or sys.platform
    env = os.environ if env is None else env
    home = home or os.path.expanduser("~")
    if platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "pgm2multicam")
    if platform.startswith("win"):
        base = env.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        return os.path.join(base, "pgm2multicam")
    base = env.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return os.path.join(base, "pgm2multicam")


def default_python(appdir, platform=None):
    platform = platform or sys.platform
    if platform.startswith("win"):
        return os.path.join(appdir, "venv", "Scripts", "python.exe")
    return os.path.join(appdir, "venv", "bin", "python")


def load_config(appdir=None, platform=None):
    """Read config.json written by the installer. Raises EngineNotInstalled with a readable message."""
    platform = platform or sys.platform
    appdir = appdir or app_dir(platform)
    cfg = {}
    path = os.path.join(appdir, "config.json")
    if os.path.isfile(path):
        try:
            with io.open(path, "r", encoding="utf-8-sig") as fh:
                cfg = json.load(fh) or {}
        except (IOError, OSError, ValueError) as exc:
            raise EngineNotInstalled("Cannot read %s: %s" % (path, exc))
    cfg.setdefault("python", default_python(appdir, platform))
    cfg.setdefault("engine_dir", os.path.join(appdir, "engine"))
    hint = ("Run the installer (install/install_mac.sh on macOS, install/install_windows.ps1 on Windows) "
            "and restart Resolve.")
    if not os.path.isfile(cfg["python"]):
        raise EngineNotInstalled("pgm2multicam engine is not installed: no Python at %s. %s"
                                 % (cfg["python"], hint))
    if not os.path.isdir(os.path.join(cfg["engine_dir"], "pgm2mc")):
        raise EngineNotInstalled("pgm2multicam engine is not installed: no pgm2mc package in %s. %s"
                                 % (cfg["engine_dir"], hint))
    return cfg


def child_env(engine_dir, base_env=None, platform=None):
    platform = platform or sys.platform
    env = dict(os.environ if base_env is None else base_env)
    env.pop("PYTHONHOME", None)
    env["PYTHONPATH"] = engine_dir
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if platform == "darwin":
        parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
        extra = [p for p in ("/opt/homebrew/bin", "/usr/local/bin") if p not in parts]
        env["PATH"] = os.pathsep.join(extra + parts) if parts or extra else "/usr/bin:/bin"
    return env


def build_argv(python, pgm, isos, outdir, name=None, sync="auto", audio="pgm", min_shot=6,
               refine=False, ffmpeg=None, ffprobe=None, rebuild=None):
    sync = (sync or "auto").lower()
    if sync not in ("auto", "timecode", "audio"):
        raise ValueError("sync must be auto, timecode or audio")
    audio = audio_flag(audio)
    argv = [python, "-u", "-m", "pgm2mc", pgm] + list(isos) + ["-o", outdir]
    if name:
        argv += ["--name", name]
    argv += ["--sync", sync, "--audio", audio, "--min-shot", str(int(min_shot))]
    if refine:
        argv.append("--refine")
    if ffmpeg:
        argv += ["--ffmpeg", ffmpeg]
    if ffprobe:
        argv += ["--ffprobe", ffprobe]
    return argv + rebuild_flags(rebuild)


DEFAULT_REBUILD = {"dissolves": True, "stills": True, "stills_min_repeat": 2, "supersource": True}


def rebuild_flags(rebuild=None):
    """CLI flags for the v2 rebuild options; defaults add nothing (works with older engines)."""
    opts = dict(DEFAULT_REBUILD)
    opts.update(rebuild or {})
    flags = []
    if not opts["dissolves"]:
        flags.append("--no-dissolves")
    if not opts["stills"]:
        flags.append("--no-stills")
    else:
        n = max(1, min(20, int(opts["stills_min_repeat"] or 2)))
        if n != DEFAULT_REBUILD["stills_min_repeat"]:
            flags += ["--stills-min-repeat", str(n)]
    if not opts["supersource"]:
        flags.append("--no-supersource")
    return flags


def plural(n, word, many=None):
    return "%d %s" % (n, word if n == 1 else (many or word + "s"))


def result_summary(result):
    """'12 transitions, 3 stills, 2 SuperSource layouts' (only the counts the engine reported)."""
    parts = []
    for key, word, many in (("transitions", "transition", None), ("stills", "still", None),
                            ("supersources", "SuperSource layout", None)):
        try:
            if result.get(key) is not None:
                parts.append(plural(int(result[key]), word, many))
        except (TypeError, ValueError):
            pass
    return ", ".join(parts)


def audio_flag(choice):
    c = (choice or "pgm").strip().lower()
    if c in ("pgm", "program audio", "program"):
        return "pgm"
    if c in ("follow", "follow camera"):
        return "follow"
    raise ValueError("audio must be pgm or follow")


def parse_line(line):
    """Parse one engine output line -> (kind, payload).

    kinds: progress (stage, pct), warn text, error text, result dict, log text.
    """
    line = (line or "").rstrip("\r\n")
    s = line.strip()
    if s.startswith("PROGRESS "):
        parts = s.split()
        if len(parts) >= 3:
            try:
                return "progress", (parts[1], max(0, min(100, int(float(parts[2])))))
            except ValueError:
                pass
    elif s.startswith("WARN "):
        return "warn", s[5:].strip()
    elif s.startswith("ERROR "):
        return "error", s[6:].strip()
    elif s.startswith("RESULT "):
        try:
            data = json.loads(s[7:])
            if isinstance(data, dict):
                return "result", data
        except ValueError:
            pass
    return "log", line


def progress_text(stage, pct):
    label = STAGE_LABELS.get(stage, stage.capitalize() if stage else "Working")
    return u"%s… %d%%" % (label, pct)


def unique_base_name(base, existing_timeline_names):
    """Base name so that none of the generated timeline names collides with an existing timeline."""
    existing = set(existing_timeline_names or [])
    candidate, n = base, 1
    while any(candidate + suf in existing for suf in TIMELINE_SUFFIX.values()):
        n += 1
        candidate = "%s %d" % (base, n)
    return candidate


def planned_imports(result, base_name, wanted):
    """[(kind, fcpxml path, timeline name)] in import order, for the kinds the user ticked."""
    out = []
    for kind in IMPORT_ORDER:
        if wanted.get(kind) and result.get(kind):
            out.append((kind, result[kind], base_name + TIMELINE_SUFFIX[kind]))
    return out


def as_list(x):
    if isinstance(x, dict):
        return list(x.values())
    return list(x or [])


def has_api(obj, name):
    # bare hasattr() is always True on Resolve objects
    try:
        return name in dir(obj)
    except Exception:
        return False


# ---------------------------------------------------------------- SuperSource box fix-up (RESEARCH2 §5.2/§7)

BOX_PROP_ORDER = ("Scaling", "ZoomGang", "ZoomX", "ZoomY", "Pan", "Tilt", "CropRetain",
                  "CropLeft", "CropRight", "CropTop", "CropBottom")
FIXUP_KINDS = ("multicam", "conform")


def _clamp_pair(a, b):
    a, b = max(0.0, float(a)), max(0.0, float(b))
    if a + b >= 0.99:
        k = 0.99 / (a + b)
        a, b = a * k, b * k
    return a, b


def box_props(box, iso_w, iso_h, seq_w, seq_h, uniform_tol=0.02):
    """TimelineItem.SetProperty values for one box (spatial conform = fit, so Zoom 1 = fitted ISO).

    Pan/Tilt: timeline px from centre (+right / +up); Crop*: px of the fitted image."""
    x0, y0, x1, y1 = [float(v) for v in box["rect"]]
    l, t, r, b = [float(v) for v in (box.get("crop") or (0, 0, 0, 0))]
    l, r = _clamp_pair(l, r)
    t, b = _clamp_pair(t, b)
    fit = min(float(seq_w) / iso_w, float(seq_h) / iso_h)
    cw, ch = iso_w * fit, iso_h * fit
    sx = (x1 - x0) * seq_w / (cw * (1 - l - r))
    sy = (y1 - y0) * seq_h / (ch * (1 - t - b))
    if abs(sx - sy) <= uniform_tol * max(sx, sy):
        sx = sy = (sx + sy) / 2
    dcx, dcy = cw * (l - r) / 2, ch * (t - b) / 2
    tcx, tcy = ((x0 + x1) / 2 - 0.5) * seq_w, ((y0 + y1) / 2 - 0.5) * seq_h
    px = tcx - sx * dcx
    py = -(tcy - sy * dcy)
    return {"Scaling": 2, "ZoomGang": sx == sy, "ZoomX": sx, "ZoomY": sy, "Pan": px + 0.0, "Tilt": py + 0.0,
            "CropRetain": True, "CropLeft": l * cw, "CropRight": r * cw, "CropTop": t * ch, "CropBottom": b * ch}


def box_targets(analysis):
    """One entry per (SuperSource occurrence, box): where to look and what to set, in PGM frames.

    track = Resolve video track index (spine V1, background V2, boxes V3..)."""
    prog = analysis.get("program") or {}
    seq_w, seq_h = prog.get("width"), prog.get("height")
    isos = dict((i.get("id"), i) for i in analysis.get("isos") or [])
    tol = max([int(tr.get("duration") or 0) for tr in analysis.get("transitions") or []] + [0]) + 2
    out = []
    if not seq_w or not seq_h:
        return out
    for ss in analysis.get("supersources") or []:
        boxes = sorted(ss.get("boxes") or [], key=lambda bx: bx.get("layer", 0))
        for rank, box in enumerate(boxes):
            iso = isos.get(box.get("angle"))
            if not iso or not iso.get("width") or not iso.get("height"):
                continue
            props = box_props(box, iso["width"], iso["height"], seq_w, seq_h)
            for occ in ss.get("occurrences") or []:
                out.append({"ss": ss.get("id"), "track": rank + 3, "lo": int(occ[0]) - tol,
                            "hi": int(occ[1]) + tol, "path": iso.get("path") or "",
                            "name": iso.get("name") or "", "props": props})
    return out


def norm_path(p):
    return os.path.normcase(os.path.normpath(p)) if p else ""


def _call(obj, name, *args):
    try:
        return getattr(obj, name)(*args)
    except Exception:
        return None


def item_matches(item, path, name):
    mpi = _call(item, "GetMediaPoolItem")
    if mpi is not None:
        fp = _call(mpi, "GetClipProperty", "File Path")
        if isinstance(fp, dict):
            fp = fp.get("File Path")
        if fp and path:
            return norm_path(fp) == norm_path(path)
    if not name:
        return False
    names = [_call(item, "GetName")] + ([_call(mpi, "GetName")] if mpi is not None else [])
    return any(n and str(n).startswith(name) for n in names)


def find_box_item(timeline, target, start_frame, used=None):
    """Timeline item for a box target: its track first, then the other tracks above V2."""
    used = used if used is not None else set()
    try:
        ntracks = int(timeline.GetTrackCount("video") or 0)
    except Exception:
        ntracks = target["track"]
    order = [target["track"]] + [k for k in range(3, ntracks + 1) if k != target["track"]]
    lo, hi = start_frame + target["lo"], start_frame + target["hi"]
    for track in order:
        for item in as_list(_call(timeline, "GetItemListInTrack", "video", track)):
            if id(item) in used:
                continue
            st = _call(item, "GetStart")
            try:
                st = int(st)
            except (TypeError, ValueError):
                continue
            if lo <= st < hi and item_matches(item, target["path"], target["name"]):
                used.add(id(item))
                return item
    return None


def apply_box_fixup(timeline, analysis):
    """SetProperty on every box found; returns (applied, not_found, failed_keys)."""
    try:
        start = int(timeline.GetStartFrame() or 0)
    except Exception:
        start = 0
    applied, missing, failed, used = 0, 0, set(), set()
    for target in box_targets(analysis):
        item = find_box_item(timeline, target, start, used)
        if item is None:
            missing += 1
            continue
        ok = True
        for key in BOX_PROP_ORDER:
            try:
                res = item.SetProperty(key, target["props"][key])
            except Exception:
                res = False
            if res is False:
                failed.add(key)
                ok = ok and key not in ("ZoomX", "ZoomY", "Pan", "Tilt")
        applied += 1 if ok else 0
        missing += 0 if ok else 1
    return applied, missing, sorted(failed)


def fixup_supersources(created, result, log):
    """Re-apply box geometry on the imported Multicam Cut / Conform timelines. Never raises."""
    try:
        if not int(result.get("supersources") or 0):
            return
        targets = [created[k] for k in FIXUP_KINDS if created.get(k) is not None]
        if not targets:
            return
        with io.open(result["analysis"], "r", encoding="utf-8") as fh:
            analysis = json.load(fh)
        for tl in targets:
            try:
                name = tl.GetName()
            except Exception:
                name = "timeline"
            try:
                applied, missing, failed = apply_box_fixup(tl, analysis)
            except Exception as exc:
                log("SuperSource box fix-up failed on %s: %s" % (name, exc))
                continue
            msg = "SuperSource boxes on %s: %d applied, %d not found." % (name, applied, missing)
            if failed:
                msg += " Resolve refused: %s." % ", ".join(failed)
            log(msg)
    except Exception as exc:
        log("SuperSource box fix-up skipped: %s" % exc)


def missing_stills(still_files, pool_paths):
    """Still files (abs paths) that are not in the media pool."""
    have = set(norm_path(p) for p in pool_paths if p)
    return [f for f in still_files if norm_path(f) not in have]


# ---------------------------------------------------------------- engine subprocess

class EngineRun(object):
    """Popen + reader threads; poll() returns parsed events without blocking."""

    def __init__(self, argv, env, cwd=None):
        self.argv = argv
        self.events = queue.Queue()
        self.result = None
        self.errors = []
        self.warnings = []
        self.returncode = None
        kwargs = {}
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL if hasattr(subprocess, "DEVNULL") else None,
                                     env=env, cwd=cwd, **kwargs)
        self._threads = [threading.Thread(target=self._reader, args=(self.proc.stdout, "stdout")),
                         threading.Thread(target=self._reader, args=(self.proc.stderr, "stderr"))]
        for t in self._threads:
            t.daemon = True
            t.start()

    def _reader(self, stream, name):
        try:
            for raw in iter(stream.readline, b""):
                self.events.put((name, raw.decode("utf-8", "replace")))
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def poll(self):
        out = []
        while True:
            try:
                _, line = self.events.get_nowait()
            except queue.Empty:
                break
            kind, payload = parse_line(line)
            if kind == "log" and not payload.strip():
                continue
            if kind == "result":
                self.result = payload
            elif kind == "error":
                self.errors.append(payload)
            elif kind == "warn":
                self.warnings.append(payload)
            out.append((kind, payload))
        return out

    def finished(self):
        if any(t.is_alive() for t in self._threads):
            return False
        if self.proc.poll() is None:
            return False
        if not self.events.empty():
            return False
        self.returncode = self.proc.returncode
        return True

    def cancel(self):
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass


# ---------------------------------------------------------------- Resolve access

def get_resolve(namespace=None):
    ns = globals() if namespace is None else namespace
    r = ns.get("resolve")
    if r:
        return r
    b = ns.get("bmd")
    if b is not None:
        try:
            r = b.scriptapp("Resolve")
            if r:
                return r
        except Exception:
            pass
    try:
        import DaVinciResolveScript as dvr  # noqa: N813
        return dvr.scriptapp("Resolve")
    except Exception:
        return None


def get_fusion(resolve, namespace=None):
    ns = globals() if namespace is None else namespace
    f = ns.get("fu") or ns.get("fusion")
    if f:
        return f
    try:
        return resolve.Fusion()
    except Exception:
        return None


def clip_info(clip):
    """(name, path) for a usable video clip, else None."""
    try:
        ctype = clip.GetClipProperty("Type")
        path = clip.GetClipProperty("File Path")
    except Exception:
        return None
    if isinstance(ctype, dict):
        ctype = ctype.get("Type")
    if isinstance(path, dict):
        path = path.get("File Path")
    if not path or not is_video_type(ctype):
        return None
    name = ""
    try:
        name = clip.GetName() or ""
    except Exception:
        pass
    return {"name": name or os.path.basename(path), "path": path}


def candidate_clips(media_pool):
    """(clips, source) where source is 'selection' or 'bin'."""
    clips, source = [], "selection"
    if has_api(media_pool, "GetSelectedClips"):
        try:
            clips = as_list(media_pool.GetSelectedClips())
        except Exception:
            clips = []
    if not clips:
        source = "bin"
        try:
            clips = as_list(media_pool.GetCurrentFolder().GetClipList())
        except Exception:
            clips = []
    infos, seen = [], set()
    for c in clips:
        info = clip_info(c)
        if info and info["path"] not in seen:
            seen.add(info["path"])
            infos.append(info)
    return infos, source


def timeline_names(project):
    names = []
    try:
        for i in range(1, int(project.GetTimelineCount() or 0) + 1):
            tl = project.GetTimelineByIndex(i)
            if tl:
                names.append(tl.GetName())
    except Exception:
        pass
    return names


def ensure_bin(media_pool, name=BIN_NAME):
    root = media_pool.GetRootFolder()
    target = None
    for f in as_list(root.GetSubFolderList()):
        try:
            if f.GetName() == name:
                target = f
                break
        except Exception:
            pass
    if target is None:
        target = media_pool.AddSubFolder(root, name)
    if target is not None:
        media_pool.SetCurrentFolder(target)
    return target


def pool_file_paths(folder, depth=0):
    paths = []
    if folder is None or depth > 8:
        return paths
    for c in as_list(_call(folder, "GetClipList")):
        fp = _call(c, "GetClipProperty", "File Path")
        if isinstance(fp, dict):
            fp = fp.get("File Path")
        if fp:
            paths.append(fp)
    for sub in as_list(_call(folder, "GetSubFolderList")):
        paths += pool_file_paths(sub, depth + 1)
    return paths


def import_missing_stills(media_pool, folder, result, log):
    """Best effort: ImportMedia the still PNGs the FCPXML import did not bring in. Never raises."""
    try:
        sdir = result.get("stills_dir")
        if not sdir or not os.path.isdir(sdir):
            return
        files = sorted(os.path.join(sdir, f) for f in os.listdir(sdir) if f.lower().endswith(".png"))
        if not files:
            return
        missing = missing_stills(files, pool_file_paths(media_pool.GetRootFolder()))
        if not missing:
            return
        if folder is not None:
            _call(media_pool, "SetCurrentFolder", folder)
        got = as_list(media_pool.ImportMedia(missing))
        log("Imported %d of %d still image(s) missing from the Media Pool." % (len(got), len(missing)))
    except Exception as exc:
        log("Could not import the still images (%s); import %s by hand if needed."
            % (exc, result.get("stills_dir")))


def import_timelines(project, media_pool, result, base_name, wanted, log):
    """Import the chosen FCPXML files; returns {kind: timeline}."""
    plan = planned_imports(result, base_name, wanted)
    if not plan:
        log("Nothing to import.")
        return {}
    folder = ensure_bin(media_pool)
    if folder is None:
        log("Could not create the Media Pool bin \"%s\"; importing into the current bin." % BIN_NAME)
    created = {}
    for kind, path, tl_name in plan:
        tl = None
        try:
            tl = media_pool.ImportTimelineFromFile(path, {"timelineName": tl_name, "importSourceClips": True})
        except Exception as exc:
            log("Import error for %s: %s" % (path, exc))
        if tl:
            created[kind] = tl
            try:
                log("Created timeline: %s" % tl.GetName())
            except Exception:
                log("Created timeline: %s" % tl_name)
        else:
            log("Import failed: %s\n  Import it manually via File > Import > Timeline." % path)
    main_tl = created.get("multicam") or created.get("conform") or created.get("stacked")
    if main_tl is not None:
        try:
            project.SetCurrentTimeline(main_tl)
        except Exception:
            pass
    if "multicam" in created:
        types = []
        if folder is not None:
            try:
                types = [str(c.GetClipProperty("Type") or "") for c in as_list(folder.GetClipList())]
            except Exception:
                types = []
        if not types or not any("multicam" in t.lower() for t in types) or \
                any("compound" in t.lower() for t in types):
            log(COMPOUND_HINT)
    if created:
        import_missing_stills(media_pool, folder, result, log)
        fixup_supersources(created, result, log)
    return created


# ---------------------------------------------------------------- run orchestration

def prepare_run(cfg, pgm, isos, outdir, name, sync, audio, min_shot, refine, rebuild=None):
    try:
        if not os.path.isdir(outdir):
            os.makedirs(outdir)
    except OSError as exc:
        raise RuntimeError("Cannot create output folder %s: %s" % (outdir, exc))
    argv = build_argv(cfg["python"], pgm, isos, outdir, name=name, sync=sync, audio=audio,
                      min_shot=min_shot, refine=refine, ffmpeg=cfg.get("ffmpeg"), ffprobe=cfg.get("ffprobe"),
                      rebuild=rebuild)
    return argv, child_env(cfg["engine_dir"])


def describe_event(kind, payload):
    if kind == "progress":
        return None
    if kind == "warn":
        return "Warning: " + payload
    if kind == "error":
        return "Error: " + payload
    if kind == "result":
        extra = result_summary(payload)
        return "Engine finished: %s segments%s." % (payload.get("segments", "?"), "; " + extra if extra else "")
    return payload


def run_headless(resolve, cfg=None, sleep=time.sleep, out=print):
    """Non-interactive path (no UIManager): selection + defaults, progress to the console."""
    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        out("%s: open a project first." % TITLE)
        return False
    mp = project.GetMediaPool()
    infos, source = candidate_clips(mp)
    names = [i["name"] for i in infos]
    progs = program_candidates(names)
    help_txt = ("Select the program clip (its name must contain PGM, Program, Prog or 'PVW out') and the ISO "
                "clips in the Media Pool, then run Workspace > Scripts > %s again." % TITLE)
    if len(progs) != 1:
        if not progs:
            out("%s: no program clip found among %d video clip(s) (%s). %s"
                % (TITLE, len(infos), source, help_txt))
        else:
            out("%s: more than one clip looks like the program: %s. %s"
                % (TITLE, ", ".join(names[i] for i in progs), help_txt))
        return False
    pgm = infos[progs[0]]
    isos = [i for k, i in enumerate(infos) if k != progs[0]]
    if not isos:
        out("%s: no ISO clips selected. %s" % (TITLE, help_txt))
        return False
    try:
        cfg = cfg or load_config()
    except EngineNotInstalled as exc:
        out("%s: %s" % (TITLE, exc))
        return False
    outdir = default_output_dir(pgm["path"])
    base = unique_base_name(file_stem(pgm["path"]), timeline_names(project))
    out("%s: program = %s; ISOs = %s; output = %s"
        % (TITLE, pgm["name"], ", ".join(i["name"] for i in isos), outdir))
    try:
        argv, env = prepare_run(cfg, pgm["path"], [i["path"] for i in isos], outdir, base,
                                "auto", "pgm", 6, False, dict(DEFAULT_REBUILD))
        run = EngineRun(argv, env)
    except (OSError, RuntimeError, ValueError) as exc:
        out("%s: could not start the engine: %s" % (TITLE, exc))
        return False
    last = None
    while True:
        for kind, payload in run.poll():
            if kind == "progress":
                txt = progress_text(*payload)
                if txt != last:
                    out(txt)
                    last = txt
            else:
                out(describe_event(kind, payload))
        if run.finished():
            break
        sleep(0.2)
    if run.returncode != 0 or run.result is None:
        out("%s: engine failed (exit code %s)." % (TITLE, run.returncode))
        return False
    created = import_timelines(project, mp, run.result, base,
                               {"multicam": True, "conform": True, "stacked": False}, out)
    return bool(created)


class Dialog(object):
    """UIManager dialog (Resolve Studio, or free <= 19.0)."""

    def __init__(self, resolve, fusion, bmd_mod):
        self.resolve = resolve
        self.project = resolve.GetProjectManager().GetCurrentProject()
        self.mp = self.project.GetMediaPool()
        self.infos, self.source = candidate_clips(self.mp)
        self.ui = fusion.UIManager
        self.disp = bmd_mod.UIDispatcher(self.ui)
        self.run = None
        self.base = None
        self.timer = None
        self.last_default_out = ""
        self._build()

    def _build(self):
        ui = self.ui

        def row(label, widget):
            return ui.HGroup({"Weight": 0}, [ui.Label({"Text": label, "Weight": 0.25}), widget])

        iso_boxes = [ui.CheckBox({"ID": "Iso%d" % k, "Text": "%s   (%s)" % (i["name"], i["path"]),
                                  "Checked": True, "Weight": 0}) for k, i in enumerate(self.infos)]
        if not iso_boxes:
            iso_boxes = [ui.Label({"Text": "No video clips selected.", "Weight": 0})]
        self.win = self.disp.AddWindow(
            {"ID": "PGM2MC", "WindowTitle": TITLE, "Geometry": [200, 120, 720, 720]},
            ui.VGroup({"Spacing": 6}, [
                row("Program clip", ui.ComboBox({"ID": "Pgm", "Weight": 0.75})),
                ui.Label({"Text": "ISO clips", "Weight": 0}),
                ui.VGroup({"Weight": 0, "Spacing": 2}, iso_boxes),
                row("Sync", ui.ComboBox({"ID": "Sync", "Weight": 0.75})),
                row("Audio in cut", ui.ComboBox({"ID": "Audio", "Weight": 0.75})),
                row("Min shot (frames)", ui.SpinBox({"ID": "MinShot", "Minimum": 1, "Maximum": 250,
                                                     "Value": 6, "Weight": 0.75})),
                ui.CheckBox({"ID": "Refine", "Text": "Refine sync ±3 frames", "Checked": False,
                             "Weight": 0}),
                ui.HGroup({"Weight": 0}, [
                    ui.Label({"Text": "Rebuild", "Weight": 0.25}),
                    ui.CheckBox({"ID": "RbDissolves", "Text": "Dissolves & dips", "Checked": True}),
                    ui.CheckBox({"ID": "RbStills", "Text": "Repeating stills", "Checked": True}),
                    ui.Label({"Text": "min repeats", "Weight": 0}),
                    ui.SpinBox({"ID": "RbMinRepeat", "Minimum": 1, "Maximum": 20, "Value": 2, "Weight": 0}),
                    ui.CheckBox({"ID": "RbSuperSource", "Text": "SuperSource", "Checked": True}),
                ]),
                ui.HGroup({"Weight": 0}, [
                    ui.Label({"Text": "Import", "Weight": 0.25}),
                    ui.CheckBox({"ID": "ImpMulticam", "Text": "Multicam cut", "Checked": True}),
                    ui.CheckBox({"ID": "ImpConform", "Text": "Conform", "Checked": True}),
                    ui.CheckBox({"ID": "ImpStacked", "Text": "Stacked", "Checked": False}),
                ]),
                row("Output folder", ui.LineEdit({"ID": "OutDir", "Weight": 0.75})),
                ui.Label({"ID": "Progress", "Text": "", "Weight": 0}),
                ui.TextEdit({"ID": "Log", "ReadOnly": True, "Weight": 1}),
                ui.HGroup({"Weight": 0}, [ui.Button({"ID": "Run", "Text": "Analyze & Build"}),
                                          ui.Button({"ID": "Close", "Text": "Close"})]),
            ]))
        self.itm = self.win.GetItems()
        for i in self.infos:
            self.itm["Pgm"].AddItem(i["name"])
        for s in SYNC_CHOICES:
            self.itm["Sync"].AddItem(s)
        for a in AUDIO_CHOICES:
            self.itm["Audio"].AddItem(a)
        if self.infos:
            self.itm["Pgm"].CurrentIndex = guess_program_index([i["name"] for i in self.infos])
        self._program_changed()
        self.win.On.Run.Clicked = self.on_run
        self.win.On.Close.Clicked = self.on_close
        self.win.On.PGM2MC.Close = self.on_close
        self.win.On.Pgm.CurrentIndexChanged = lambda ev: self._program_changed()
        self.win.On.RbStills.Clicked = lambda ev: setattr(self.itm["RbMinRepeat"], "Enabled",
                                                          bool(self.itm["RbStills"].Checked))
        try:
            self.timer = ui.Timer({"ID": "PollTimer", "Interval": 200})
        except Exception:
            self.timer = None
        if self.timer is not None:
            for target in (self.disp, self.win):
                try:
                    target.On.PollTimer.Timeout = self.on_poll
                except Exception:
                    pass
        src = "Media Pool selection" if self.source == "selection" else "clips in the current bin"
        self.log("%d video clip(s) from the %s." % (len(self.infos), src))

    def log(self, msg):
        if msg is None:
            return
        print("%s: %s" % (TITLE, msg))
        try:
            self.itm["Log"].Append(msg)
        except Exception:
            try:
                self.itm["Log"].PlainText = (self.itm["Log"].PlainText or "") + msg + "\n"
            except Exception:
                pass

    def _rebuild_options(self):
        return {"dissolves": bool(self.itm["RbDissolves"].Checked),
                "stills": bool(self.itm["RbStills"].Checked),
                "stills_min_repeat": int(self.itm["RbMinRepeat"].Value or 2),
                "supersource": bool(self.itm["RbSuperSource"].Checked)}

    def _program_index(self):
        try:
            return int(self.itm["Pgm"].CurrentIndex)
        except Exception:
            return 0

    def _program_changed(self):
        if not self.infos:
            return
        p = self._program_index()
        for k in range(len(self.infos)):
            box = self.itm.get("Iso%d" % k) if hasattr(self.itm, "get") else self.itm["Iso%d" % k]
            if box is None:
                continue
            box.Checked = (k != p)
            box.Enabled = (k != p)
        new_default = default_output_dir(self.infos[p]["path"])
        current = self.itm["OutDir"].Text or ""
        if not current or current == self.last_default_out:
            self.itm["OutDir"].Text = new_default
        self.last_default_out = new_default

    def on_run(self, ev):
        if self.run is not None or not self.infos:
            return
        p = self._program_index()
        pgm = self.infos[p]
        isos = [i for k, i in enumerate(self.infos) if k != p and self.itm["Iso%d" % k].Checked]
        if not isos:
            self.log("Tick at least one ISO clip.")
            return
        try:
            cfg = load_config()
        except EngineNotInstalled as exc:
            self.log(str(exc))
            return
        outdir = (self.itm["OutDir"].Text or "").strip() or default_output_dir(pgm["path"])
        self.wanted = {"multicam": bool(self.itm["ImpMulticam"].Checked),
                       "conform": bool(self.itm["ImpConform"].Checked),
                       "stacked": bool(self.itm["ImpStacked"].Checked)}
        self.base = unique_base_name(file_stem(pgm["path"]), timeline_names(self.project))
        try:
            argv, env = prepare_run(cfg, pgm["path"], [i["path"] for i in isos], outdir, self.base,
                                    self.itm["Sync"].CurrentText or "Auto",
                                    self.itm["Audio"].CurrentText or "Program audio",
                                    int(self.itm["MinShot"].Value or 6), bool(self.itm["Refine"].Checked),
                                    self._rebuild_options())
            self.log("Running engine: program %s, %d ISO(s), output %s" % (pgm["name"], len(isos), outdir))
            self.run = EngineRun(argv, env)
        except (OSError, RuntimeError, ValueError) as exc:
            self.log("Could not start the engine: %s" % exc)
            self.run = None
            return
        self.itm["Run"].Enabled = False
        self.itm["Progress"].Text = "Starting…"
        if self.timer is not None:
            try:
                self.timer.Start()
                return
            except Exception:
                pass
        while not self.on_poll(None):  # no timer: block, UI updates when done
            time.sleep(0.2)

    def on_poll(self, ev):
        if self.run is None:
            return True
        for kind, payload in self.run.poll():
            if kind == "progress":
                self.itm["Progress"].Text = progress_text(*payload)
            else:
                self.log(describe_event(kind, payload))
        if not self.run.finished():
            return False
        if self.timer is not None:
            try:
                self.timer.Stop()
            except Exception:
                pass
        run, self.run = self.run, None
        if run.returncode == 0 and run.result is not None:
            self.itm["Progress"].Text = "Importing…"
            created = import_timelines(self.project, self.mp, run.result, self.base, self.wanted, self.log)
            self.itm["Progress"].Text = "Done: %d timeline(s) imported." % len(created)
        else:
            self.itm["Progress"].Text = "Failed (exit code %s)." % run.returncode
        self.itm["Run"].Enabled = True
        return True

    def on_close(self, ev):
        if self.run is not None:
            self.run.cancel()
        self.disp.ExitLoop()

    def show(self):
        self.win.Show()
        self.disp.RunLoop()
        self.win.Hide()


def main(namespace=None):
    ns = globals() if namespace is None else namespace
    resolve = get_resolve(ns)
    if resolve is None:
        print("%s: run this from DaVinci Resolve (Workspace > Scripts)." % TITLE)
        return False
    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        print("%s: open a project first." % TITLE)
        return False
    fusion = get_fusion(resolve, ns)
    bmd_mod = ns.get("bmd")
    if bmd_mod is None:
        try:
            import DaVinciResolveScript as bmd_mod  # noqa: N813
        except Exception:
            bmd_mod = None
    ui_ok = fusion is not None and bmd_mod is not None and getattr(fusion, "UIManager", None) is not None
    if ui_ok:
        try:
            dlg = Dialog(resolve, fusion, bmd_mod)
        except Exception as exc:
            print("%s: dialog unavailable (%s); using the Media Pool selection with defaults." % (TITLE, exc))
            dlg = None
        if dlg is not None:
            dlg.show()
            return True
    return run_headless(resolve)


if __name__ == "__main__" or "resolve" in globals():
    main()
