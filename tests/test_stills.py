"""Unit tests for stills.py, the thumbnail store and the SuperSource hand-off (synthetic data, no media)."""
import os
import sys
import types

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "engine"))

from pgm2mc import analyze as AN  # noqa: E402
from pgm2mc import stills as S  # noqa: E402
from pgm2mc.frames import ThumbStore, downsample  # noqa: E402


def image(seed):
    rng = np.random.default_rng(seed)
    return (rng.random((18, 32)) * 180 + 40).astype(np.float32)


def noisy(img, n, seed, sigma=0.8):
    rng = np.random.default_rng(seed)
    return np.clip(np.rint(img[None] + rng.normal(0, sigma, (n,) + img.shape)), 0, 255).astype(np.uint8)


def moving(n, seed):
    rng = np.random.default_rng(seed)
    return (rng.random((n, 18, 32)) * 255).astype(np.uint8)


def test_static_runs_bounds_and_noise():
    x = np.concatenate([moving(20, 1), noisy(image(1), 30, 2), moving(5, 3), noisy(image(1), 8, 4)])
    assert S.static_runs(x) == [(20, 50)]         # the 8-frame run is too short


def test_slow_fade_is_not_static():
    img = image(2)
    x = np.stack([np.clip(img * (1 - 0.012 * t), 0, 255) for t in range(60)]).astype(np.uint8)
    assert all(e - s < 40 for s, e in S.static_runs(x))


def test_flat_runs_are_not_stills():
    black = np.full((30, 18, 32), 3, dtype=np.uint8)
    x = np.concatenate([moving(10, 1), black, noisy(image(3), 20, 5)])
    runs = S.find_runs(x, [(0, len(x))])
    assert [(s, e) for s, e, _ in runs] == [(40, 60)]


def test_cluster_counts_repeats():
    g1, g2 = image(10), image(11)
    x = np.concatenate([noisy(g1, 20, 1), moving(10, 2), noisy(g2, 15, 3), moving(10, 4), noisy(g1, 25, 5)])
    runs = S.find_runs(x, [(0, 20), (20, 30), (30, 45), (45, 55), (55, 80)])
    assert [(s, e) for s, e, _ in runs] == [(0, 20), (30, 45), (55, 80)]
    clusters = S.cluster_runs(runs)
    assert sorted(clusters) == [[0, 2], [1]]
    # a brighter copy of the same layout is a different still
    runs.append((80, 100, runs[0][2] + 30))
    assert [0, 2] in S.cluster_runs(runs)


def test_relabel_splits_only_pgm():
    segs = [{"start": 0, "end": 10, "angle": "a1", "score": 1.0, "kind": "camera"},
            {"start": 10, "end": 50, "angle": "pgm", "score": 0.8, "kind": "pgm"}]
    out = S.relabel(segs, 5, 30, {"kind": "still", "still": "s1"})
    assert [(s["start"], s["end"], s["kind"]) for s in out] == [(0, 10, "camera"), (10, 30, "still"),
                                                                (30, 50, "pgm")]
    assert out[1]["still"] == "s1" and "still" not in out[2]
    st = [{"id": "s1", "occurrences": []}]
    S.sync_occurrences(st, out)
    assert st[0]["occurrences"] == [[10, 30]]


def test_downsample_and_store():
    x = np.arange(64 * 36, dtype=np.float64).reshape(36, 64) % 251
    frames = np.stack([x, x[::-1]]).astype(np.uint8)
    small = downsample(frames)
    assert small.shape == (2, 18, 32)
    want = frames[0].reshape(18, 2, 32, 2).astype(float).mean(axis=(1, 3))
    assert np.abs(small[0].astype(float) - want).max() <= 0.5
    with ThumbStore() as st:
        st.append("pgm", small[:1])
        st.append("pgm", small[1:], np.array([False]))
        st.finish()
        fr, va = st.get("pgm")
        assert fr.shape == (2, 18, 32) and np.array_equal(fr[1], small[1]) and list(va) == [True, False]
        assert st.get("nope") == (None, None)
        d = st.dir
    assert not os.path.exists(d)


def test_supersource_handoff(monkeypatch):
    calls = []

    def detect_supersources(ffmpeg, analysis, candidates, assets_dir, min_len=25, progress=None):
        calls.append((candidates, min_len, analysis["version"]))
        return ([{"id": "ss1", "background": "s1", "boxes": [], "occurrences": [[120, 200]], "animated": False,
                  "confidence": 0.9}],
                [{"id": "s1", "path": "/x/bg.png", "width": 8, "height": 8, "occurrences": [[120, 200]],
                  "role": "background"}])

    monkeypatch.setitem(sys.modules, "pgm2mc.supersource",
                        types.SimpleNamespace(detect_supersources=detect_supersources))
    segs = [{"start": 0, "end": 100, "angle": "a1", "score": 1.0, "kind": "camera"},
            {"start": 100, "end": 110, "angle": "pgm", "score": 1.0, "kind": "still", "still": "s1"},
            {"start": 110, "end": 115, "angle": "pgm", "score": 0.5, "kind": "pgm"},
            {"start": 115, "end": 220, "angle": "pgm", "score": 0.5, "kind": "pgm"}]
    analysis = {"version": 2, "stills": [{"id": "s1", "role": "still"}], "supersources": []}
    warnings = []
    out = AN._supersource("ffmpeg", analysis, segs, "/tmp/x", warnings, None)
    assert calls == [([[115, 220]], 25, 2)]
    assert [(s["start"], s["end"], s["kind"]) for s in out] == [
        (0, 100, "camera"), (100, 110, "still"), (110, 120, "pgm"), (120, 200, "supersource"), (200, 220, "pgm")]
    assert out[3]["supersource"] == "ss1"
    assert [s["id"] for s in analysis["stills"]] == ["s1", "s2"]
    assert analysis["supersources"][0]["background"] == "s2" and not warnings
