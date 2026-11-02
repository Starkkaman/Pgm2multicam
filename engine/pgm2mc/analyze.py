"""Orchestrate probe -> sync -> thumbnails/scores -> Viterbi -> stills -> supersource -> dissolves into the
Analysis dict (docs/CONTRACT.md, version 2; version 1 compatible output when no assets_dir is given)."""
import os
import sys

import numpy as np

from . import dissolve as dissolve_mod
from . import match
from . import stills as stills_mod
from .frames import DEFAULT_SIZE, ThumbReader, ThumbStore
from .probe import EngineError, find_tool, probe
from .sync import decode_audio, lookup_manual, sync_iso

REFINE_RADIUS = 3
SS_MIN_LEN = 25


def stderr_progress(stage, pct):
    sys.stderr.write("PROGRESS %s %d\n" % (stage, pct))
    sys.stderr.flush()


class _Progress:
    def __init__(self, cb):
        self.cb = cb
        self.last = {}

    def __call__(self, stage, pct):
        pct = int(max(0, min(100, pct)))
        if self.cb and self.last.get(stage) != pct:
            self.last[stage] = pct
            self.cb(stage, pct)


def _media_dict(info, id_, name):
    return {"id": id_, "path": info.path, "name": name, "start_tc_frames": int(info.start_tc_frames),
            "duration_frames": int(info.duration_frames), "width": int(info.width), "height": int(info.height),
            "has_audio": bool(info.has_audio), "audio_rate": int(info.audio_rate),
            "audio_channels": int(info.audio_channels)}


def analyze(pgm_path, iso_paths, ffmpeg=None, ffprobe=None, sync="auto", offsets=None,
            min_shot=match.DEFAULT_MIN_SHOT, switch_penalty=match.DEFAULT_SWITCH_PENALTY,
            pgm_floor=match.DEFAULT_PGM_FLOOR, thumb_size=DEFAULT_SIZE, hwaccel=None, audio_window=1200.0,
            allow_rate_mismatch=False, refine=False, progress=stderr_progress, dissolves=True, stills=True,
            stills_min_repeat=2, supersource=True, assets_dir=None):
    """Run the whole analysis and return the Analysis dict.

    offsets: {ISO id | file stem | file name: frames} manual overrides. sync: auto | timecode | audio.
    progress: callable(stage, pct) or None; default prints `PROGRESS <stage> <pct>` to stderr.
    assets_dir: where still PNGs go. Given -> version 2 analysis with stills / supersource detection and the
    extra PROGRESS stages (stills, supersource, dissolves). None (v1 callers) -> no PNGs and no SuperSource,
    "version": 1 and only the v1 stages; dissolves still move cuts and are listed in "transitions".
    """
    prog = _Progress(progress)
    if not iso_paths:
        raise EngineError("at least one ISO is required")
    ffmpeg_exe = find_tool("ffmpeg", ffmpeg)
    if not ffmpeg_exe:
        raise EngineError("ffmpeg not found (use --ffmpeg, PGM2MC_FFMPEG or install it)")
    ffprobe_exe = find_tool("ffprobe", ffprobe)
    warnings = []

    # 1. probe
    prog("probe", 0)
    pgm = probe(pgm_path, ffprobe_exe, ffmpeg_exe)
    isos = []
    for i, path in enumerate(iso_paths):
        isos.append(probe(path, ffprobe_exe, ffmpeg_exe))
        prog("probe", 100.0 * (i + 1) / len(iso_paths))
    for info in [pgm] + isos:
        warnings.extend(info.warnings)
    for iso in isos:
        if iso.fps != pgm.fps:
            msg = "%s: frame rate %s differs from PGM %s" % (iso.name, iso.fps, pgm.fps)
            if not allow_rate_mismatch:
                raise EngineError(msg + " (use --allow-rate-mismatch to continue anyway)")
            warnings.append(msg)
    ids = ["a%d" % (i + 1) for i in range(len(isos))]

    # 2. sync
    prog("sync", 0)
    results = []
    pgm_audio = []

    def get_pgm_audio():
        if not pgm_audio:
            pgm_audio.append(decode_audio(ffmpeg_exe, pgm.path, audio_window))
        return pgm_audio[0]

    for i, iso in enumerate(isos):
        manual = lookup_manual(offsets, iso, ids[i])
        res = sync_iso(ffmpeg_exe, pgm, iso, sync, manual, audio_window, get_pgm_audio)
        warnings.extend(res.detail.get("warnings", []))
        if res.offset_frames >= iso.duration_frames or res.offset_frames + pgm.duration_frames <= 0:
            warnings.append("%s: offset %+d puts the ISO outside the PGM range" % (iso.name, res.offset_frames))
        results.append(res)
        prog("sync", 100.0 * (i + 1) / len(isos))

    # 3+4. thumbnails and scores (32x18 copies go to a disk-backed store for the post-passes)
    radius = REFINE_RADIUS if refine else 0
    prog("frames", 0)
    est = max(1, pgm.duration_frames)
    store = ThumbStore()
    try:
        readers = []
        try:
            pgm_reader = ThumbReader(ffmpeg_exe, pgm.path, pgm.fps, 0, thumb_size, hwaccel)
            readers.append(pgm_reader)
            iso_readers = []
            for iso, res in zip(isos, results):
                r = ThumbReader(ffmpeg_exe, iso.path, iso.fps, res.offset_frames - radius, thumb_size, hwaccel)
                readers.append(r)
                iso_readers.append(r)
            scores = match.compute_scores(pgm_reader, iso_readers, radius, store=store,
                                          progress=lambda n: prog("frames", min(99.0, 100.0 * n / est)))
        finally:
            for r in readers:
                r.close()
            store.finish()
        return _finish(store, scores, pgm, isos, results, ids, warnings, prog, ffmpeg_exe, radius, refine,
                       min_shot, switch_penalty, pgm_floor, dissolves, stills, stills_min_repeat, supersource,
                       assets_dir)
    finally:
        store.close()


def _finish(store, scores, pgm, isos, results, ids, warnings, prog, ffmpeg_exe, radius, refine, min_shot,
            switch_penalty, pgm_floor, dissolves, stills, stills_min_repeat, supersource, assets_dir):
    prog("frames", 100)
    T = scores.shape[0]
    if T == 0:
        raise EngineError("no video frames decoded from PGM %s" % pgm.path)
    if abs(T - pgm.duration_frames) > 1:
        warnings.append("PGM decoded %d frames, probe said %d; using %d" % (T, pgm.duration_frames, T))
    pgm.duration_frames = T

    # 5. match
    prog("match", 0)
    chosen = np.full(len(isos), radius, dtype=int)
    base = scores[:, :, radius]
    labels = match.viterbi(base, pgm_floor, switch_penalty, min_shot)
    prog("match", 50 if refine else 90)
    if refine:
        changed = False
        for k in range(len(isos)):
            live = labels == k
            if live.sum() < 10:
                continue
            means = scores[live, k, :].mean(axis=0)
            j = int(np.argmax(means))
            if j != radius and means[j] > means[radius] + 0.01:
                chosen[k] = j
                changed = True
                d = j - radius
                warnings.append("%s: refine moved offset by %+d frames (score %.3f -> %.3f)"
                                % (isos[k].name, d, means[radius], means[j]))
                results[k].offset_frames += d
        if changed:
            base = scores[:, np.arange(len(isos)), chosen]
            labels = match.viterbi(base, pgm_floor, switch_penalty, min_shot)
    segments = match.segments_from_labels(labels, base, ids)
    for seg in segments:
        seg["kind"] = "pgm" if seg["angle"] == "pgm" else "camera"
    prog("match", 100)

    iso_dicts = []
    for i, (iso, res) in enumerate(zip(isos, results)):
        d = _media_dict(iso, ids[i], iso.name)
        d.update({"offset_frames": int(res.offset_frames), "sync_method": res.method,
                  "sync_confidence": float(res.confidence)})
        iso_dicts.append(d)
    v2 = assets_dir is not None
    analysis = {
        "version": 2 if v2 else 1,
        "fps": {"num": pgm.fps.numerator, "den": pgm.fps.denominator},
        "drop_frame": bool(pgm.drop_frame if pgm.has_timecode else any(i.drop_frame for i in isos)),
        "program": _media_dict(pgm, "pgm", pgm.name),
        "isos": iso_dicts,
        "segments": segments,
        "transitions": [],
        "stills": [],
        "supersources": [],
        "warnings": warnings,
    }
    pgm_small, _ = store.get("pgm")
    pgm_full, _ = store.get("pgm_full")
    templates = {}

    # 6. repeating stills (needs assets_dir for the PNGs)
    if v2 and stills:
        prog("stills", 0)
        found, segments, templates = stills_mod.detect_stills(
            ffmpeg_exe, analysis["program"], pgm.fps, segments, pgm_small, pgm_full, assets_dir,
            stills_min_repeat, progress=lambda p: prog("stills", min(99.0, p)))
        analysis["stills"].extend(found)
        analysis["segments"] = segments
        prog("stills", 100)

    # 7. SuperSource (separate module, optional)
    if v2 and supersource:
        prog("supersource", 0)
        segments = _supersource(ffmpeg_exe, analysis, segments, assets_dir, warnings,
                                lambda p: prog("supersource", min(99.0, p)))
        analysis["segments"] = segments
        prog("supersource", 100)

    # 8. dissolves / dips (after stills so they can involve still templates)
    if dissolves:
        if v2:
            prog("dissolves", 0)
        streams = []
        for k in range(len(isos)):
            fr, va = store.get("iso%d" % k)
            streams.append((fr, va, int(chosen[k])))
        src = dissolve_mod.Sources(pgm_small, streams, ids, templates)
        segments, transitions = dissolve_mod.detect_dissolves(
            segments, src, progress=(lambda p: prog("dissolves", min(99.0, p))) if v2 else None)
        analysis["segments"] = segments
        analysis["transitions"] = transitions
        if v2:
            prog("dissolves", 100)
    stills_mod.sync_occurrences(analysis["stills"], analysis["segments"])
    return analysis


def _supersource(ffmpeg_exe, analysis, segments, assets_dir, warnings, progress):
    try:
        from .supersource import detect_supersources
    except ImportError:
        return segments
    candidates = [[s["start"], s["end"]] for s in segments
                  if s.get("kind") == "pgm" and s["end"] - s["start"] >= SS_MIN_LEN]
    if not candidates:
        return segments
    try:
        found, bg_stills = detect_supersources(ffmpeg_exe, analysis, candidates, os.path.abspath(assets_dir),
                                               min_len=SS_MIN_LEN, progress=progress)
    except Exception as e:  # a detector bug or decode error must not lose the rest of the analysis
        warnings.append("SuperSource detection failed: %s" % e)
        return segments
    used = set(s["id"] for s in analysis["stills"])
    renamed = {}
    for st in bg_stills or []:
        st = dict(st)
        if st["id"] in used:
            n = len(used) + 1
            while "s%d" % n in used:
                n += 1
            renamed[st["id"]] = "s%d" % n
            st["id"] = renamed[st["id"]]
        used.add(st["id"])
        analysis["stills"].append(st)
    for ss in found or []:
        if ss.get("background") in renamed:
            ss["background"] = renamed[ss["background"]]
        analysis["supersources"].append(ss)
        for s, e in ss.get("occurrences", []):
            segments = stills_mod.relabel(segments, int(s), int(e), {"kind": "supersource",
                                                                     "supersource": ss["id"]})
    return _merge(segments)


def _merge(segments):
    """Join neighbours that carry the same label."""
    out = []
    keys = ("angle", "kind", "still", "supersource")
    for seg in segments:
        if out and all(out[-1].get(k) == seg.get(k) for k in keys):
            prev = out[-1]
            a, b = prev["end"] - prev["start"], seg["end"] - seg["start"]
            prev["score"] = round((prev["score"] * a + seg["score"] * b) / float(a + b), 4)
            prev["end"] = seg["end"]
        else:
            out.append(seg)
    return out
