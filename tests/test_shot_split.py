"""Hard-cut splitting of pgm segments before stills / SuperSource detection (synthetic thumbnails, no media)."""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "engine"))

from pgm2mc import analyze as AN  # noqa: E402
from pgm2mc import stills as S  # noqa: E402


def image(seed):
    rng = np.random.default_rng(seed)
    return (rng.random((18, 32)) * 180 + 40).astype(np.float32)


def shot(img, n, seed, sigma=0.8, drift=0.0):
    """n frames of `img` with compression-like noise; drift > 0 adds a sliding bright box (motion)."""
    rng = np.random.default_rng(seed)
    out = img[None] + rng.normal(0, sigma, (n,) + img.shape)
    if drift:
        for t in range(n):
            x = int(abs((t * drift) % 48 - 24))  # bouncing, no jumps
            out[t, 4:12, x:x + 8] += 50
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


def dissolve(a, b, n):
    return np.stack([np.clip(np.rint(a * (1 - (t + 1) / float(n + 1)) + b * (t + 1) / float(n + 1)), 0, 255)
                     for t in range(n)]).astype(np.uint8)


def test_hard_cuts_found_motion_ignored():
    x = np.concatenate([shot(image(1), 40, 1, drift=0.7), shot(image(2), 30, 2), shot(image(3), 50, 3, drift=1.3)])
    assert AN.hard_cuts(x, 0, len(x)) == [40, 70]
    assert AN.hard_cuts(x, 10, 60) == [40]           # range-relative input, absolute output
    assert AN.hard_cuts(shot(image(4), 80, 4, drift=1.0), 0, 80) == []


def test_dissolve_and_flash_are_not_cuts():
    a, b = image(5), image(6)
    x = np.concatenate([shot(a, 30, 1), dissolve(a, b, 12), shot(b, 30, 2)])
    assert AN.hard_cuts(x, 0, len(x)) == []
    short = np.concatenate([shot(a, 30, 1), dissolve(a, b, 2), shot(b, 30, 2)])
    assert AN.hard_cuts(short, 0, len(short)) == []
    flash = np.concatenate([shot(a, 30, 1), np.full((1, 18, 32), 250, np.uint8), shot(a, 30, 2)])
    assert AN.hard_cuts(flash, 0, len(flash)) == []


def test_no_tiny_pieces_at_edges():
    x = np.concatenate([shot(image(7), 1, 1), shot(image(8), 40, 2), shot(image(9), 2, 3)])
    assert AN.hard_cuts(x, 0, len(x)) == []
    assert AN.hard_cuts(x, 0, 4) == []


def test_split_and_merge_back():
    x = np.concatenate([shot(image(1), 20, 1), shot(image(2), 30, 2), shot(image(3), 25, 3)])
    segs = [{"start": 0, "end": 20, "angle": "a1", "score": 1.0, "kind": "camera"},
            {"start": 20, "end": 75, "angle": "pgm", "score": 0.4, "kind": "pgm"}]
    out = AN.split_at_cuts(segs, x)
    assert [(s["start"], s["end"], s["kind"]) for s in out] == [(0, 20, "camera"), (20, 50, "pgm"),
                                                                (50, 75, "pgm")]
    assert AN._merge([dict(s) for s in out]) == segs
    out[2].update(kind="supersource", supersource="ss1")
    assert len(AN._merge(out)) == 3


def test_still_occurrences_count_shots_not_runs():
    ranges = [(0, 100), (150, 250)]
    # one quasi-static shot broken into touching runs = one occurrence
    assert S.count_occurrences([(10, 30), (30, 60), (61, 90)], ranges) == 1
    assert S.count_occurrences([(10, 30), (40, 60)], ranges) == 2
    assert S.count_occurrences([(80, 100), (150, 170)], ranges) == 2
    assert S.count_occurrences([(160, 200), (10, 40)], ranges) == 2
