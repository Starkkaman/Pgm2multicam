"""ISO -> PGM sync offsets.

Convention (docs/CONTRACT.md): PGM frame p shows the same instant as ISO-local frame p + offset_frames.
"""
import os
import subprocess
from dataclasses import dataclass, field

import numpy as np

from .probe import EngineError, frames_per_day

AUDIO_RATE = 8000
ENV_BLOCK = 16          # 8 kHz / 16 = 500 Hz envelope for the coarse search
FINE_MARGIN = 0.05      # seconds searched around the coarse lag at full rate
FINE_LENGTH = 30.0      # seconds of overlap used for the fine search
LOW_CONFIDENCE = 0.3


@dataclass
class SyncResult:
    offset_frames: int
    method: str                 # timecode | audio | manual | none
    confidence: float
    detail: dict = field(default_factory=dict)


def timecode_offset(pgm, iso):
    """Offset from start timecodes, or None when either lacks TC or the ranges do not overlap."""
    if not (pgm.has_timecode and iso.has_timecode):
        return None
    off = pgm.start_tc_frames - iso.start_tc_frames
    day = frames_per_day(pgm.fps, pgm.drop_frame)
    if off > day // 2:
        off -= day
    elif off < -(day // 2):
        off += day
    # PGM covers ISO-local [off, off + pgm.duration) ; ISO covers [0, iso.duration)
    if max(0, off) >= min(iso.duration_frames, off + pgm.duration_frames):
        return None
    return off


def decode_audio(ffmpeg, path, seconds=None, rate=AUDIO_RATE):
    """Decode the first audio stream to mono float32 at `rate` Hz (first `seconds` only if given)."""
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
    if seconds:
        cmd += ["-t", "%.3f" % seconds]
    cmd += ["-i", path, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise EngineError("audio decode failed for %s: %s" % (path, p.stderr.decode("utf-8", "replace").strip()))
    return np.frombuffer(p.stdout, dtype="<f4").astype(np.float32)


def _next_pow2(n):
    return 1 << max(1, int(n - 1).bit_length())


def xcorr(a, b):
    """Full cross-correlation c[k] = sum_i a[i] * b[i + k], returned with lags from -(len(a)-1) to len(b)-1."""
    n = _next_pow2(len(a) + len(b))
    fa = np.fft.rfft(a.astype(np.float64), n)
    fb = np.fft.rfft(b.astype(np.float64), n)
    c = np.fft.irfft(np.conj(fa) * fb, n)
    c = np.concatenate([c[n - (len(a) - 1):], c[:len(b)]]) if len(a) > 1 else c[:len(b)]
    lags = np.arange(-(len(a) - 1), len(b))
    return c, lags


def _standardise(x):
    x = x.astype(np.float64)
    x = x - x.mean()
    s = np.sqrt(np.sum(x * x))
    return x / s if s > 1e-12 else x


def _envelope(x, block):
    n = len(x) // block
    env = np.abs(x[:n * block]).reshape(n, block).mean(axis=1)
    return _standardise(env)


def audio_lag(pgm_audio, iso_audio, rate=AUDIO_RATE):
    """Lag in samples such that pgm sample i == iso sample i + lag. Returns (lag, confidence, detail)."""
    min_len = ENV_BLOCK * 64
    if len(pgm_audio) < min_len or len(iso_audio) < min_len:
        raise EngineError("not enough audio to sync")
    ep = _envelope(pgm_audio, ENV_BLOCK)
    ei = _envelope(iso_audio, ENV_BLOCK)
    c, lags = xcorr(ep, ei)
    k = int(np.argmax(c))
    peak = float(c[k])
    excl = int(0.25 * rate / ENV_BLOCK)
    masked = c.copy()
    masked[max(0, k - excl):k + excl + 1] = -np.inf
    second = float(np.max(masked)) if np.isfinite(masked).any() else 0.0
    coarse = int(lags[k]) * ENV_BLOCK
    ratio = peak / second if second > 1e-12 else float("inf")
    confidence = 0.0 if peak <= 0 else float(np.clip(1.0 - max(second, 0.0) / peak, 0.0, 1.0))
    detail = {"coarse_lag_samples": coarse, "peak": round(peak, 4),
              "peak_ratio": round(ratio, 3) if np.isfinite(ratio) else None}

    lag = coarse
    m = int(FINE_MARGIN * rate) + ENV_BLOCK
    a = max(0, m - coarse)
    length = min(int(FINE_LENGTH * rate), len(pgm_audio) - a, len(iso_audio) - (a + coarse + m))
    if length >= rate:
        x = _standardise(pgm_audio[a:a + length])
        y = iso_audio[a + coarse - m:a + coarse + length + m].astype(np.float64)
        y = y - y.mean()
        fc, flags = xcorr(x, y)
        sel = (flags >= 0) & (flags <= 2 * m)
        fc, flags = fc[sel], flags[sel]
        j = int(np.argmax(fc))
        ywin = np.sqrt(np.sum(y[flags[j]:flags[j] + length] ** 2))
        fine_peak = float(fc[j] / ywin) if ywin > 1e-12 else 0.0
        detail["fine_peak"] = round(fine_peak, 4)
        if fine_peak > 0.1:
            lag = coarse - m + int(flags[j])
    detail["lag_samples"] = lag
    return lag, confidence, detail


def audio_offset(ffmpeg, pgm, iso, window=1200.0, pgm_audio=None):
    """Audio cross-correlation sync over the first `window` seconds of the PGM (2x that of the ISO).

    pgm_audio: decoded PGM audio, or a callable returning it (lets callers decode it once, lazily).
    """
    if callable(pgm_audio):
        pgm_audio = pgm_audio()
    if pgm_audio is None:
        pgm_audio = decode_audio(ffmpeg, pgm.path, window)
    # ISOs usually roll before the PGM: give them a longer window so large offsets still overlap
    iso_audio = decode_audio(ffmpeg, iso.path, 2 * window if window else None)
    lag, conf, detail = audio_lag(pgm_audio, iso_audio)
    offset = int(round(lag / float(AUDIO_RATE) * float(pgm.fps)))
    return SyncResult(offset, "audio", round(conf, 3), detail)


def sync_iso(ffmpeg, pgm, iso, mode="auto", manual=None, window=1200.0, pgm_audio=None):
    """Sync one ISO (manual override > timecode > audio). Warnings go in result.detail['warnings']."""
    if manual is not None:
        return SyncResult(int(manual), "manual", 1.0, {"warnings": []})
    if mode not in ("auto", "timecode", "audio"):
        raise EngineError("unknown sync mode %r" % mode)
    if mode in ("auto", "timecode"):
        off = timecode_offset(pgm, iso)
        if off is not None:
            return SyncResult(off, "timecode", 1.0, {"warnings": []})
        if mode == "timecode":
            raise EngineError("%s: timecode sync impossible (missing timecode or no overlap with PGM)" % iso.name)
    if not (pgm.has_audio and iso.has_audio):
        raise EngineError("%s: cannot sync (no usable timecode and no audio); pass a manual offset" % iso.name)
    res = audio_offset(ffmpeg, pgm, iso, window, pgm_audio)
    warnings = []
    if res.confidence < LOW_CONFIDENCE:
        warnings.append("%s: low audio sync confidence %.2f (offset %+d frames); check it or pass --offset"
                        % (iso.name, res.confidence, res.offset_frames))
    res.detail["warnings"] = warnings
    return res


def parse_manual_offsets(items):
    """['CAM1=+120', 'CAM2=-5'] -> {'CAM1': 120, 'CAM2': -5}."""
    out = {}
    for item in items or []:
        if "=" not in item:
            raise EngineError("bad --offset %r (expected NAME=+FRAMES)" % item)
        name, val = item.rsplit("=", 1)
        try:
            out[name.strip()] = int(val.strip())
        except ValueError:
            raise EngineError("bad --offset %r (frames must be an integer)" % item)
    return out


def lookup_manual(offsets, iso, iso_id):
    """Match a manual offset by ISO id, file stem or file name."""
    if not offsets:
        return None
    for key in (iso_id, iso.name, os.path.basename(iso.path)):
        if key in offsets:
            return offsets[key]
    return None
