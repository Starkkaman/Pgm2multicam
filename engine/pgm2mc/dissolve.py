"""Dissolves and dips at segment boundaries, fitted on 32x18 thumbnails.

Per frame, PGM_t ~ a*A_t + b*B_t + d by least squares (A, B = aligned ISO thumbnails or a still template; a solid
colour is the constant d). wA = a/gA and wB = b/gB (g = the source's usual gain into the PGM) are the mix
weights: a dissolve has wA + wB ~ 1 with wA ramping 1 -> 0, a dip ramps wA down to ~0 and then wB up.
"""
import math

import numpy as np

MAX_SHORT = 60          # pgm segments up to this long between two sources may be a dissolve's middle
MARGIN = 30             # frames searched on each side of a boundary / short pgm segment
PURE = 0.92             # weight above which a frame counts as one pure source (and the other below 1 - PURE)
EXPLAINED = 0.85        # R^2 every mixed frame must reach (flat frames always pass)
FLAT_STD = 2.5          # PGM spatial std below this = solid colour
BEAT = 0.5              # dissolve: 2-source residual must be below BEAT * best single-source residual
DIP_SUM = 0.5           # wA + wB below this somewhere in the run -> dip
MONO_TOL = 0.08


class Sources:
    """Thumbnail access for segment sources: cameras (aligned ISO streams) and stills (templates)."""

    def __init__(self, pgm_small, isos, ids, templates):
        # isos: list of (frames memmap, valid, shift) with PGM frame t = stream frame t + shift
        self.pgm = pgm_small
        self.isos = isos
        self.index = {i: k for k, i in enumerate(ids)}
        self.templates = templates
        self.gains = {}

    def key(self, seg):
        if seg.get("kind", "camera" if seg["angle"] != "pgm" else "pgm") == "camera":
            return ("cam", self.index.get(seg["angle"]))
        if seg.get("kind") == "still" and seg.get("still") in self.templates:
            return ("still", seg["still"])
        return None

    def frames(self, key, a, b):
        """(n, h*w) float32 thumbnails of a source over PGM frames [a, b), or None if unavailable."""
        kind, k = key
        if kind == "still":
            t = self.templates[k].reshape(1, -1).astype(np.float32)
            return np.repeat(t, b - a, axis=0)
        if k is None or self.isos[k][0] is None:
            return None
        fr, va, sh = self.isos[k]
        if a + sh < 0 or b + sh > len(fr) or not va[a + sh:b + sh].all():
            return None
        return np.asarray(fr[a + sh:b + sh]).reshape(b - a, -1).astype(np.float32)

    def gain(self, key, segments):
        if key[0] == "still":
            return 1.0
        if key not in self.gains:
            vals = []
            for s in segments:
                if self.key(s) != key or s["end"] - s["start"] < 8:
                    continue
                a, b = s["start"] + 3, s["end"] - 3
                idx = np.unique(np.linspace(a, b - 1, min(b - a, 40)).astype(int))
                for t in idx:
                    A = self.frames(key, t, t + 1)
                    if A is None:
                        continue
                    P = np.asarray(self.pgm[t]).reshape(-1).astype(np.float64)
                    A = A[0].astype(np.float64)
                    A -= A.mean()
                    P -= P.mean()
                    aa = (A * A).sum()
                    if aa > 0:
                        vals.append((A * P).sum() / aa)
                if len(vals) > 400:
                    break
            g = float(np.median(vals)) if vals else 1.0
            self.gains[key] = g if g > 0.2 else 1.0
        return self.gains[key]


def fit_window(P, A, B):
    """Per-frame least squares. Returns dict of arrays: a, b, d, r2 (2-source), rA, rB (single), sst, flat."""
    N = P.shape[1]
    mP, mA, mB = P.mean(1, keepdims=True), A.mean(1, keepdims=True), B.mean(1, keepdims=True)
    Pc, Ac, Bc = P - mP, A - mA, B - mB
    aa, bb, ab = (Ac * Ac).sum(1), (Bc * Bc).sum(1), (Ac * Bc).sum(1)
    ap, bp, pp = (Ac * Pc).sum(1), (Bc * Pc).sum(1), (Pc * Pc).sum(1)
    det = aa * bb - ab * ab
    ok = det > 1e-6 * np.maximum(aa * bb, 1e-12)
    det = np.where(ok, det, 1.0)
    a = np.where(ok, (ap * bb - bp * ab) / det, 0.0)
    b = np.where(ok, (bp * aa - ap * ab) / det, 0.0)
    r2 = np.maximum(pp - a * ap - b * bp, 0.0) / N
    rA = np.maximum(pp - np.where(aa > 0, ap * ap / np.maximum(aa, 1e-12), 0.0), 0.0) / N
    rB = np.maximum(pp - np.where(bb > 0, bp * bp / np.maximum(bb, 1e-12), 0.0), 0.0) / N
    d = (mP - a[:, None] * mA - b[:, None] * mB)[:, 0]
    return {"a": a, "b": b, "d": d, "r2": r2, "rA": rA, "rB": rB, "sst": pp / N, "ok": ok,
            "flat": pp / N < FLAT_STD ** 2}


def _line_end(t, w, target):
    """Fit w = m*t + q over the points and return the t where w == target (None if not usable)."""
    if len(t) < 2:
        return None
    m, q = np.polyfit(np.asarray(t, dtype=np.float64), np.asarray(w, dtype=np.float64), 1)
    if abs(m) < 1e-6:
        return None
    return (target - q) / m, m


def _monotonic(w, sign):
    if len(w) < 2:
        return 1.0
    steps = np.diff(np.asarray(w)) * sign
    return float(np.mean(steps > -MONO_TOL))


def analyse_window(P, A, B, gA, gB, w0, anchor):
    """Look for a transition A -> B in PGM frames [w0, w0 + len(P)).

    anchor: (lo, hi) frame range the mixed run must touch. Returns a dict (kind, start, end, confidence, color)
    with start/end = fractional frames where the ramp leaves A / reaches B, or None.
    """
    f = fit_window(P, A, B)
    n = len(P)
    wA = np.where(f["flat"], 0.0, f["a"] / gA)
    wB = np.where(f["flat"], 0.0, f["b"] / gB)
    pureA = (wA > PURE) & (wB < 1 - PURE)
    pureB = (wB > PURE) & (wA < 1 - PURE)
    mixed = ~(pureA | pureB)
    lo, hi = anchor[0] - w0, anchor[1] - w0
    # contiguous mixed runs; pick the one touching the anchor
    run = None
    i = 0
    while i < n:
        if not mixed[i]:
            i += 1
            continue
        j = i
        while j < n and mixed[j]:
            j += 1
        if i <= hi and j >= lo:
            if run is None or (j - i) > (run[1] - run[0]):
                run = (i, j)
        i = j
    if run is None:
        return None
    i, j = run
    if i == 0 or j == n or not pureA[i - 1] or not pureB[j] or j - i < 2:
        return None
    if not f["ok"][i:j].all():
        return None
    sst = np.maximum(f["sst"][i:j], 1e-9)
    r2n = 1.0 - f["r2"][i:j] / sst
    if not np.all((r2n >= EXPLAINED) | f["flat"][i:j]):
        return None
    t = np.arange(i, j) + w0
    sA, sB = wA[i:j], wB[i:j]
    total = sA + sB
    if total.min() < DIP_SUM or f["flat"][i:j].any():
        k = int(np.argmin(total))
        selA = (np.arange(i, j) <= i + k) & (sA > 0.05) & (sA < 0.95)
        selB = (np.arange(i, j) >= i + k) & (sB > 0.05) & (sB < 0.95)
        ra = _line_end(t[selA], sA[selA], 1.0)
        rb = _line_end(t[selB], sB[selB], 1.0)
        start = ra[0] if ra and ra[1] < 0 else i + w0 - 1.0
        end = rb[0] if rb and rb[1] > 0 else j + w0 + 0.0
        mono = min(_monotonic(sA[:k + 1], -1), _monotonic(sB[k:], 1))
        if mono < 0.9:
            return None
        lvl = float(np.median(f["d"][i:j][total <= total.min() + 0.1]))
        g = int(np.clip(round(lvl), 0, 255))
        color = [0, 0, 0] if g < 24 else [g, g, g]
        conf = float(np.mean(np.where(f["flat"][i:j], 1.0, np.clip(r2n, 0, 1)))) * mono
        return {"kind": "dip", "start": start, "end": end, "confidence": conf, "color": color}
    u = np.clip((sA + 1.0 - sB) / 2.0, 0.0, 1.0)
    mono = _monotonic(u, -1)
    if mono < 0.9:
        return None
    core = (u > 0.2) & (u < 0.8)
    if not core.any():
        core = np.ones(len(u), dtype=bool)
    single = np.minimum(f["rA"][i:j], f["rB"][i:j])[core]
    two = f["r2"][i:j][core]
    ratio = float(np.median(two) / max(float(np.median(single)), 1e-9))
    if ratio > BEAT:
        return None
    sel = (u > 0.05) & (u < 0.95)
    r = _line_end(t[sel], u[sel], 1.0)
    if r and r[1] < 0:
        start = r[0]
        end = start - 1.0 / r[1]
    else:
        start, end = i + w0 - 1.0, j + w0 + 0.0
    conf = float(np.clip(1.0 - ratio, 0.0, 1.0)) * mono
    return {"kind": "dissolve", "start": start, "end": end, "confidence": conf}


def detect_dissolves(segments, sources, progress=None):
    """Return (segments, transitions). Cuts move to ramp centres; pgm segments a transition explains vanish."""
    segs = [dict(s) for s in segments]
    T = segs[-1]["end"] if segs else 0
    transitions = []
    prev_end = 0
    i = 0
    total = max(1, len(segs) - 1)
    while i + 1 < len(segs):
        if progress:
            progress(100.0 * min(i, total) / total)
        found = None
        L = segs[i]
        kL = sources.key(L)
        cands = []
        if i + 2 < len(segs) and segs[i + 1].get("kind") == "pgm" and \
                segs[i + 1]["end"] - segs[i + 1]["start"] <= MAX_SHORT:
            cands.append((i + 2, segs[i + 1]["start"], segs[i + 1]["end"]))
        cands.append((i + 1, segs[i + 1]["start"], segs[i + 1]["start"]))
        for ri, lo, hi in cands:
            R = segs[ri]
            kR = sources.key(R)
            if kL is None or kR is None or kL == kR:
                continue
            w0 = max(L["start"], prev_end, lo - MARGIN)
            w1 = min(R["end"], hi + MARGIN)
            if w1 - w0 < 4:
                continue
            A = sources.frames(kL, w0, w1)
            B = sources.frames(kR, w0, w1)
            if A is None or B is None:
                continue
            P = np.asarray(sources.pgm[w0:w1]).reshape(w1 - w0, -1).astype(np.float32)
            res = analyse_window(P, A, B, sources.gain(kL, segs), sources.gain(kR, segs), w0, (lo, hi))
            if res is None:
                continue
            if hi > lo and (lo < math.floor(res["start"]) - 1 or hi > math.ceil(res["end"]) + 1):
                continue        # the pgm segment is more than this transition
            found = (ri, res)
            break
        if found is None:
            i += 1
            continue
        ri, res = found
        R = segs[ri]
        edit = int(math.floor((res["start"] + res["end"]) / 2.0 + 0.5))
        edit = min(max(edit, L["start"] + 1, prev_end + 1), R["end"] - 1)
        dur = int(round(res["end"] - res["start"]))
        room_l, room_r = edit - max(L["start"], prev_end), R["end"] - edit
        if dur // 2 > room_l or dur - dur // 2 > room_r:
            dur = min(2 * room_l, 2 * room_r)
        L["end"] = edit
        R["start"] = edit
        del segs[i + 1:ri]
        if dur >= 2:
            tr = {"edit": edit, "duration": dur, "kind": res["kind"], "confidence": round(res["confidence"], 3)}
            if res["kind"] == "dip":
                tr["color"] = res["color"]
            transitions.append(tr)
            prev_end = edit + dur - dur // 2
        i += 1
    if progress:
        progress(100)
    assert not segs or segs[-1]["end"] == T
    return segs, transitions
