"""Repeating static program images (graphics, still store): detection on thumbnails, clustering, PNG extraction."""
import os
import subprocess

import numpy as np

from .probe import EngineError

MIN_RUN = 12            # frames a static run must last
DIFF_THR = 1.0          # mean abs diff between consecutive 32x18 frames (grey levels)
DRIFT_THR = 2.5         # mean abs diff to the first frame of the run (stops slow fades)
FLAT_STD = 4.0          # runs whose image has less spatial std are solid colours (dips), never stills
NCC_SMALL = 0.97        # clustering: NCC of the 32x18 templates
NCC_FULL = 0.95         # clustering: NCC of the full-size (64x36) templates
MEAN_DIFF = 8.0         # clustering: mean abs diff of the 32x18 templates
EDGE_ABSORB = 3         # pgm leftovers shorter than this next to a still join the still
JOIN_GAP = 3            # runs of one still this close together in one segment are a single occurrence
EXTRACT_FRAMES = 5


def _ncc(a, b):
    a = a.ravel().astype(np.float64) - a.mean()
    b = b.ravel().astype(np.float64) - b.mean()
    den = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / den) if den > 0 else 0.0


def _median(frames, s, e, cap=200):
    """Per-pixel median of frames[s:e] (at most `cap` evenly spaced frames, so long runs stay cheap)."""
    idx = np.unique(np.linspace(s, e - 1, min(cap, e - s)).astype(int))
    if e - s <= 4 * cap:
        block = np.asarray(frames[s:e])[idx - s]
    else:
        block = np.stack([np.asarray(frames[i]) for i in idx])
    return np.median(block.astype(np.float32), axis=0)


def static_runs(frames, min_len=MIN_RUN, chunk=4096):
    """Static runs in (n, h, w) uint8 frames (array or memmap) -> list of (start, end) local frame ranges."""
    n = len(frames)
    runs = []
    i0 = 0
    ref = prev = None
    for c0 in range(0, n, chunk):
        x = np.asarray(frames[c0:c0 + chunk]).reshape(-1, frames.shape[1] * frames.shape[2]).astype(np.float32)
        for k in range(len(x)):
            cur = x[k]
            if prev is not None and np.abs(cur - prev).mean() < DIFF_THR and np.abs(cur - ref).mean() < DRIFT_THR:
                prev = cur
                continue
            i = c0 + k
            if prev is not None and i - i0 >= min_len:
                runs.append((i0, i))
            i0, ref, prev = i, cur, cur
    if n - i0 >= min_len:
        runs.append((i0, n))
    return runs


def find_runs(pgm_small, ranges, min_len=MIN_RUN):
    """Static, non-flat runs inside the given [start, end) PGM ranges -> list of (start, end, template)."""
    out = []
    for s, e in ranges:
        for a, b in static_runs(pgm_small[s:e], min_len):
            tpl = _median(pgm_small, s + a, s + b)
            if tpl.std() >= FLAT_STD:
                out.append((s + a, s + b, tpl))
    return out


def cluster_runs(runs, pgm_full=None):
    """Greedy clustering of runs by template similarity -> list of lists of run indices."""
    fulls = []
    for s, e, _ in runs:
        if pgm_full is not None:
            fulls.append(_median(pgm_full, s, e))
        else:
            fulls.append(None)
    clusters = []
    for i, (_, _, tpl) in enumerate(runs):
        for c in clusters:
            j = c[0]
            rt = runs[j][2]
            if np.abs(tpl - rt).mean() >= MEAN_DIFF or _ncc(tpl, rt) < NCC_SMALL:
                continue
            if fulls[i] is not None and _ncc(fulls[i], fulls[j]) < NCC_FULL:
                continue
            c.append(i)
            break
        else:
            clusters.append([i])
    return clusters


def extract_png(ffmpeg, path, fps, width, height, first, out_png, count=EXTRACT_FRAMES):
    """Median of `count` full-resolution frames starting at PGM frame `first`, written as an RGB PNG."""
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
    if first > 0:
        cmd += ["-ss", "%.6f" % ((first - 0.5) / float(fps))]
    cmd += ["-i", path, "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", "setpts=PTS-STARTPTS,scale=%d:%d" % (
        width, height), "-frames:v", str(count), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    p = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    size = width * height * 3
    got = len(p.stdout) // size
    if p.returncode != 0 or got == 0:
        raise EngineError("ffmpeg could not decode still frames from %s: %s"
                          % (path, p.stderr.decode("utf-8", "replace").strip()[-400:]))
    frames = np.frombuffer(p.stdout[:got * size], dtype=np.uint8).reshape(got, height, width, 3)
    img = np.median(frames, axis=0).astype(np.uint8) if got > 1 else frames[0]
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", "%dx%d" % (width, height), "-i", "-", "-frames:v", "1", "-pix_fmt", "rgb24", out_png]
    p = subprocess.run(cmd, input=img.tobytes(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise EngineError("ffmpeg could not write %s: %s" % (out_png, p.stderr.decode("utf-8", "replace")[-400:]))


def relabel(segments, start, end, fields, only_kinds=("pgm",)):
    """Split segments so that the parts of [start, end) whose kind is in `only_kinds` get `fields`."""
    out = []
    for seg in segments:
        s, e = seg["start"], seg["end"]
        if seg.get("kind") not in only_kinds or e <= start or s >= end:
            out.append(seg)
            continue
        a, b = max(s, start), min(e, end)
        if s < a:
            out.append(dict(seg, end=a))
        mid = dict(seg, start=a, end=b)
        mid.update(fields)
        out.append(mid)
        if b < e:
            out.append(dict(seg, start=b))
    return out


def _absorb_leftovers(segments):
    """Tiny pgm pieces squeezed between a still and its neighbour become part of the still."""
    out = []
    for i, seg in enumerate(segments):
        if seg["kind"] == "pgm" and seg["end"] - seg["start"] < EDGE_ABSORB:
            prev = out[-1] if out else None
            nxt = segments[i + 1] if i + 1 < len(segments) else None
            if prev is not None and prev["kind"] == "still":
                prev["end"] = seg["end"]
                continue
            if nxt is not None and nxt["kind"] == "still":
                nxt["start"] = seg["start"]
                continue
        out.append(seg)
    return out


def detect_stills(ffmpeg, program, fps, segments, pgm_small, pgm_full, assets_dir, min_repeat=2, progress=None):
    """Find repeating static images in pgm segments, write their PNGs and relabel those parts as stills.

    Returns (stills entries, new segments, {still id: 32x18 float32 template}).
    """
    ranges = [(s["start"], s["end"]) for s in segments if s.get("kind") == "pgm"]
    runs = find_runs(pgm_small, ranges)
    if progress:
        progress(40)
    clusters = [c for c in cluster_runs(runs, pgm_full)
                if count_occurrences([runs[i][:2] for i in c], ranges) >= max(1, int(min_repeat))]
    clusters.sort(key=lambda c: runs[c[0]][0])
    stills, templates = [], {}
    os.makedirs(assets_dir, exist_ok=True)
    for n, c in enumerate(clusters):
        sid = "s%d" % (n + 1)
        longest = max(c, key=lambda i: runs[i][1] - runs[i][0])
        s, e, _ = runs[longest]
        first = max(s, (s + e) // 2 - EXTRACT_FRAMES // 2)
        count = min(EXTRACT_FRAMES, e - first)
        png = os.path.join(os.path.abspath(assets_dir), sid + ".png")
        extract_png(ffmpeg, program["path"], fps, program["width"], program["height"], first, png, count)
        tpl = np.median(np.stack([runs[i][2] for i in c]), axis=0)
        templates[sid] = tpl
        for i in c:
            rs, re_ = runs[i][0], runs[i][1]
            segments = relabel(segments, rs, re_, {"kind": "still", "still": sid,
                                                   "score": still_score(pgm_small[rs:re_], tpl)})
        stills.append({"id": sid, "path": png, "width": int(program["width"]),
                       "height": int(program["height"]), "occurrences": [], "role": "still"})
        if progress:
            progress(40 + 60.0 * (n + 1) / len(clusters))
    segments = _absorb_leftovers(segments)
    return stills, segments, templates


def count_occurrences(spans, ranges, gap=JOIN_GAP):
    """Times a still goes on air: its runs, where runs in the same range at most `gap` frames apart count once
    (a quasi-static shot broken into several runs by small motion is one occurrence)."""
    def range_of(f):
        for k, (s, e) in enumerate(ranges):
            if s <= f < e:
                return k
        return -1
    n, prev = 0, None
    for s, e in sorted(spans):
        if prev is None or range_of(s) != range_of(prev[0]) or s - prev[1] > gap:
            n += 1
        prev = (s, e)
    return n


def still_score(frames, tpl):
    """Mean max(0, NCC) of frames against a template, 0..1."""
    step = max(1, len(frames) // 100)
    vals = [_ncc(np.asarray(f, dtype=np.float32), tpl) for f in frames[::step]]
    return round(float(np.clip(np.mean(vals), 0.0, 1.0)), 4) if vals else 0.0


def sync_occurrences(stills, segments):
    """Rewrite each still's `occurrences` from the final segments (stills with no segment keep theirs)."""
    occ = {}
    for s in segments:
        if s.get("kind") == "still":
            occ.setdefault(s["still"], []).append([s["start"], s["end"]])
    for st in stills:
        if st["id"] in occ:
            st["occurrences"] = occ[st["id"]]
