"""Unit tests for dissolve.py on synthetic 32x18 thumbnails (no media needed)."""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "engine"))

from pgm2mc import dissolve as D  # noqa: E402

T = 160
GAIN, BIAS = 1.08, 6.0


def moving_texture(seed, n=T):
    """A smooth random texture panning one pixel every other frame, like a moving camera."""
    rng = np.random.default_rng(seed)
    big = rng.random((18 + 8, 32 + n)) * 200 + 20
    k = np.ones(3) / 3.0
    big = np.apply_along_axis(lambda r: np.convolve(r, k, mode="same"), 1, big)
    big = np.apply_along_axis(lambda c: np.convolve(c, k, mode="same"), 0, big)
    return np.stack([big[4:22, t // 2:t // 2 + 32] for t in range(n)]).astype(np.float32)


def make_sources(cams, templates=None):
    isos = [(np.clip(np.rint(c), 0, 255).astype(np.uint8), np.ones(len(c), dtype=bool), 0) for c in cams]
    return isos, templates or {}


def pgm_from(weights, cams, colour=0.0, seed=9):
    """weights: (T, n_cams) mix weights; the rest of the light is `colour`."""
    rng = np.random.default_rng(seed)
    x = np.zeros((T, 18, 32), dtype=np.float32)
    for k, c in enumerate(cams):
        x += weights[:, k, None, None] * (GAIN * c + BIAS)
    x += (1.0 - weights.sum(axis=1))[:, None, None] * colour
    x += rng.normal(0, 0.6, x.shape)
    return np.clip(np.rint(x), 0, 255).astype(np.uint8)


def segs(*spec):
    out = []
    for s, e, angle in spec:
        kind = "pgm" if angle == "pgm" else ("still" if angle.startswith("s") else "camera")
        seg = {"start": s, "end": e, "angle": "pgm" if kind != "camera" else angle, "score": 0.9, "kind": kind}
        if kind == "still":
            seg["still"] = angle
        out.append(seg)
    return out


def run(weights, cams, segments, colour=0.0, templates=None):
    P = pgm_from(weights, cams, colour)
    isos, tpl = make_sources(cams, templates)
    src = D.Sources(P, isos, ["a1", "a2", "a3"][:len(cams)], tpl)
    out, trans = D.detect_dissolves(segments, src)
    check_invariants(out, trans, segments[-1]["end"])
    return out, trans


def check_invariants(out, trans, n):
    assert out[0]["start"] == 0 and out[-1]["end"] == n
    for a, b in zip(out, out[1:]):
        assert a["end"] == b["start"] and a["end"] > a["start"]
    bounds = {s["start"]: i for i, s in enumerate(out)}
    last = 0
    for tr in sorted(trans, key=lambda t: t["edit"]):
        i = bounds[tr["edit"]]
        lo = tr["edit"] - tr["duration"] // 2
        hi = lo + tr["duration"]
        assert lo >= out[i - 1]["start"] and hi <= out[i]["end"]
        assert lo >= last
        last = hi


def ramp(n, start, dur):
    """1 -> 0 linear mix weight: 1 at `start`, 0 at `start + dur` (xfade convention)."""
    t = np.arange(n, dtype=np.float64)
    return np.clip(1.0 - (t - start) / dur, 0.0, 1.0)


def test_cross_dissolve_moves_cut_to_centre():
    cams = [moving_texture(1), moving_texture(2)]
    a = ramp(T, 50, 20)
    w = np.stack([a, 1 - a], axis=1)
    out, trans = run(w, cams, segs((0, 63, "a1"), (63, T, "a2")))
    assert len(trans) == 1
    tr = trans[0]
    assert tr["kind"] == "dissolve" and abs(tr["edit"] - 60) <= 1 and abs(tr["duration"] - 20) <= 1
    assert tr["confidence"] > 0.8
    assert [(s["start"], s["angle"]) for s in out] == [(0, "a1"), (tr["edit"], "a2")]


def test_dissolve_removes_spurious_pgm_segment():
    cams = [moving_texture(1), moving_texture(2)]
    a = ramp(T, 40, 40)
    w = np.stack([a, 1 - a], axis=1)
    out, trans = run(w, cams, segs((0, 52, "a1"), (52, 68, "pgm"), (68, T, "a2")))
    assert len(trans) == 1 and trans[0]["kind"] == "dissolve"
    assert abs(trans[0]["edit"] - 60) <= 1 and abs(trans[0]["duration"] - 40) <= 2
    assert [s["angle"] for s in out] == ["a1", "a2"]


def test_dip_to_black():
    cams = [moving_texture(3), moving_texture(4)]
    wa = ramp(T, 52, 8)
    wb = 1 - ramp(T, 60, 8)
    w = np.stack([wa, wb], axis=1)
    out, trans = run(w, cams, segs((0, 57, "a1"), (57, 63, "pgm"), (63, T, "a2")))
    assert len(trans) == 1
    tr = trans[0]
    assert tr["kind"] == "dip" and tr["color"] == [0, 0, 0]
    assert abs(tr["edit"] - 60) <= 1 and abs(tr["duration"] - 16) <= 1
    assert [s["angle"] for s in out] == ["a1", "a2"]


def test_dip_to_grey_colour():
    cams = [moving_texture(3), moving_texture(4)]
    wa = ramp(T, 50, 10)
    wb = 1 - ramp(T, 64, 10)
    w = np.stack([wa, wb], axis=1)
    out, trans = run(w, cams, segs((0, 55, "a1"), (55, 69, "pgm"), (69, T, "a2")), colour=128.0)
    assert len(trans) == 1 and trans[0]["kind"] == "dip"
    assert abs(trans[0]["color"][0] - 128) <= 6
    assert abs(trans[0]["edit"] - 62) <= 1 and abs(trans[0]["duration"] - 24) <= 1


def test_hard_cut_is_left_alone():
    cams = [moving_texture(5), moving_texture(6)]
    w = np.zeros((T, 2))
    w[:80, 0] = 1
    w[80:, 1] = 1
    s = segs((0, 80, "a1"), (80, T, "a2"))
    out, trans = run(w, cams, s)
    assert trans == [] and out == s


def test_unrelated_short_pgm_is_kept():
    cams = [moving_texture(5), moving_texture(6), moving_texture(7)]
    w = np.zeros((T, 3))
    w[:60, 0] = 1
    w[60:90, 2] = 1          # a source the engine was not given
    w[90:, 1] = 1
    P = pgm_from(w, cams)
    isos, _ = make_sources(cams[:2])
    s = segs((0, 60, "a1"), (60, 90, "pgm"), (90, T, "a2"))
    out, trans = D.detect_dissolves(s, D.Sources(P, isos, ["a1", "a2"], {}))
    assert trans == [] and out == s


def test_dissolve_from_camera_into_still():
    cams = [moving_texture(8)]
    rng = np.random.default_rng(3)
    still = (rng.random((18, 32)) * 180 + 40).astype(np.float32)
    a = ramp(T, 70, 14)
    P = np.clip(a[:, None, None] * (GAIN * cams[0] + BIAS) + (1 - a)[:, None, None] * still
                + rng.normal(0, 0.5, (T, 18, 32)), 0, 255).astype(np.uint8)
    isos, _ = make_sources(cams)
    tpl = {"s1": np.median(P[100:], axis=0).astype(np.float32)}
    s = segs((0, 79, "a1"), (79, 84, "pgm"), (84, T, "s1"))
    out, trans = D.detect_dissolves(s, D.Sources(P, isos, ["a1"], tpl))
    check_invariants(out, trans, T)
    assert len(trans) == 1 and trans[0]["kind"] == "dissolve"
    assert abs(trans[0]["edit"] - 77) <= 1 and abs(trans[0]["duration"] - 14) <= 1
    assert [s["kind"] for s in out] == ["camera", "still"]


def test_two_dissolves_back_to_back_do_not_overlap():
    cams = [moving_texture(1), moving_texture(2), moving_texture(3)]
    a = ramp(T, 40, 20)
    b = ramp(T, 66, 20)
    w = np.stack([a, (1 - a) * b, 1 - b], axis=1)
    out, trans = run(w, cams, segs((0, 50, "a1"), (50, 76, "a2"), (76, T, "a3")))
    assert [t["kind"] for t in trans] == ["dissolve", "dissolve"]
    assert abs(trans[0]["edit"] - 50) <= 1 and abs(trans[1]["edit"] - 76) <= 1
