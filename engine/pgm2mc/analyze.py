"""Orchestrate probe -> sync -> thumbnails/scores -> Viterbi into the Analysis dict (docs/CONTRACT.md v1)."""
import sys

import numpy as np

from . import match
from .frames import DEFAULT_SIZE, ThumbReader
from .probe import EngineError, find_tool, probe
from .sync import decode_audio, lookup_manual, sync_iso

REFINE_RADIUS = 3


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
            allow_rate_mismatch=False, refine=False, progress=stderr_progress):
    """Run the whole analysis and return the Analysis dict.

    offsets: {ISO id | file stem | file name: frames} manual overrides. sync: auto | timecode | audio.
    progress: callable(stage, pct) or None; default prints `PROGRESS <stage> <pct>` to stderr.
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

    # 3+4. thumbnails and scores
    radius = REFINE_RADIUS if refine else 0
    prog("frames", 0)
    est = max(1, pgm.duration_frames)
    readers = []
    try:
        pgm_reader = ThumbReader(ffmpeg_exe, pgm.path, pgm.fps, 0, thumb_size, hwaccel)
        readers.append(pgm_reader)
        iso_readers = []
        for iso, res in zip(isos, results):
            r = ThumbReader(ffmpeg_exe, iso.path, iso.fps, res.offset_frames - radius, thumb_size, hwaccel)
            readers.append(r)
            iso_readers.append(r)
        scores = match.compute_scores(pgm_reader, iso_readers, radius,
                                      progress=lambda n: prog("frames", min(99.0, 100.0 * n / est)))
    finally:
        for r in readers:
            r.close()
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
    prog("match", 100)

    iso_dicts = []
    for i, (iso, res) in enumerate(zip(isos, results)):
        d = _media_dict(iso, ids[i], iso.name)
        d.update({"offset_frames": int(res.offset_frames), "sync_method": res.method,
                  "sync_confidence": float(res.confidence)})
        iso_dicts.append(d)
    return {
        "version": 1,
        "fps": {"num": pgm.fps.numerator, "den": pgm.fps.denominator},
        "drop_frame": bool(pgm.drop_frame if pgm.has_timecode else any(i.drop_frame for i in isos)),
        "program": _media_dict(pgm, "pgm", pgm.name),
        "isos": iso_dicts,
        "segments": segments,
        "warnings": warnings,
    }
