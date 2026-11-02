"""SuperSource / picture-in-picture detection on program ranges that no single ISO explains.

For each candidate range three PGM frames (20/50/80 %) and the time-aligned ISO frames are decoded small (RGB).
Each ISO is located in the PGM by multi-scale FFT normalised cross-correlation (luma) of a central patch of the
scaled ISO (so boxes cropped up to ~30 % per edge still match): coarse at half size, then fine, then refined on the
whole visible area. The visible rect is where the PGM agrees in colour with the placed ISO (pixels explained by
another box count as unknown, so occluded edges are still found); the crop follows from the rect and the placed
full ISO frame. A box must also be seen in the other sample frames, fit its own centre well and pass a high-pass
correlation check (smooth graphics correlate with anything at low frequency). Same layouts are grouped into one
entry; the background art is the per-pixel median of box-masked full-resolution PGM frames, holes filled from
other layouts' backgrounds or by push-pull interpolation, written as PNG with ffmpeg.
"""
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .probe import EngineError

WORK_W = 480            # working width for matching
MIN_BOX = 0.15          # box (full ISO frame) width as a fraction of PGM width
MAX_BOX = 0.95
COARSE_STEP = 1.06      # geometric scale step of the coarse search
FINE_STEP = 0.005
FINE_N = 7              # fine search: +-FINE_N steps
PATCH = 0.4             # central fraction of the ISO frame used as template
REFINE_STEP = 0.0025
REFINE_N = 6
NCC_MIN = 0.8
AGREE_MIN = 0.6         # share of agreeing pixels inside the final rect
HP_MIN = 0.7            # high-pass NCC inside the final rect
GRAD_TOL = 1.0          # residual tolerance per unit of local gradient (~1 px misregistration)
RES_MAX = 10.0          # max median residual (grey levels) of the fit on the central patch
GAP_TOL = 0.015         # disagreeing run (fraction of frame size) that ends a box edge walk
CONF_MIN = 0.6
COVER_MIN = 0.02        # boxes must cover at least this share of the frame
MIN_AREA = 0.004        # min rect area, fraction of frame
STATIC_TOL = 0.01       # placement tolerance for "same layout" / static
CROP_TOL = 0.02
CROP_SNAP = 0.005
SAMPLES = (0.2, 0.5, 0.8)
BG_MAX_FRAMES = 12
BG_MAX_BYTES = 150e6    # cap on decoded full-res frames held for the median
BG_MARGIN = 0.005       # extra mask around boxes (borders, scaler ringing), fraction of width


def _grab(ffmpeg, path, fps, frame, size, pix):
    """One decoded frame (file-local index) as uint8 (h, w) gray or (h, w, 3) rgb; None past the end."""
    w, h = size
    ch = 3 if pix == "rgb24" else 1
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
    if frame > 0:
        # same as frames.ThumbReader: half a frame early, then the first frame is exactly `frame`
        cmd += ["-ss", "%.6f" % ((frame - 0.5) / fps)]
    cmd += ["-i", path, "-map", "0:v:0", "-an", "-sn", "-dn",
            "-vf", "setpts=PTS-STARTPTS,scale=%d:%d:flags=area,format=%s" % (w, h, pix),
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", pix, "-"]
    p = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise EngineError("ffmpeg failed decoding %s frame %d: %s"
                          % (path, frame, p.stderr.decode("utf-8", "replace").strip()[-800:]))
    n = w * h * ch
    if len(p.stdout) < n:
        return None
    a = np.frombuffer(p.stdout[:n], dtype=np.uint8)
    return a.reshape(h, w, 3) if ch == 3 else a.reshape(h, w)


def _write_png(ffmpeg, rgb, path):
    h, w = rgb.shape[:2]
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", "%dx%d" % (w, h), "-i", "-", "-frames:v", "1", "-update", "1", path]
    p = subprocess.run(cmd, input=np.ascontiguousarray(rgb).tobytes(), stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise EngineError("ffmpeg failed writing %s: %s" % (path, p.stderr.decode("utf-8", "replace")[-800:]))


def _resample(n_out, n_in, k, start):
    """(n_out, n_in) area weights: output pixel j covers input [start + j/k, start + (j+1)/k)."""
    a = start + np.arange(n_out) / k
    i = np.arange(n_in)
    lo = np.maximum(a[:, None], i[None, :])
    hi = np.minimum(a[:, None] + 1.0 / k, i[None, :] + 1.0)
    return np.clip(hi - lo, 0.0, None) * k


def _integral(img):
    ii = np.zeros((img.shape[0] + 1, img.shape[1] + 1))
    ii[1:, 1:] = img.cumsum(0).cumsum(1)
    return ii


def _box_sums(ii, th, tw):
    return ii[th:, tw:] - ii[:-th, tw:] - ii[th:, :-tw] + ii[:-th, :-tw]


def _box3(a):
    p = np.pad(a, 1, mode="edge")
    return (p[:-2, :-2] + p[:-2, 1:-1] + p[:-2, 2:] + p[1:-1, :-2] + p[1:-1, 1:-1] + p[1:-1, 2:] +
            p[2:, :-2] + p[2:, 1:-1] + p[2:, 2:]) / 9.0


def _parabola(a, b, c):
    d = a - 2.0 * b + c
    if d >= 0:
        return 0.0
    return float(np.clip(0.5 * (a - c) / d, -0.5, 0.5))


class _Frame:
    """A PGM working frame with its FFT and integral images, for NCC against many templates."""

    def __init__(self, img):
        self.img = img.astype(np.float64)
        self.h, self.w = img.shape
        self.fft = np.fft.rfft2(self.img)
        self.ii = _integral(self.img)
        self.ii2 = _integral(self.img * self.img)

    def ncc(self, tpl):
        th, tw = tpl.shape
        if th > self.h or tw > self.w or th < 4 or tw < 4:
            return None
        t = tpl - tpl.mean()
        tn = np.sqrt((t * t).sum())
        if tn < np.sqrt(t.size) * 2.0:  # flat template
            return None
        tf = np.fft.rfft2(t, s=(self.h, self.w))
        corr = np.fft.irfft2(self.fft * np.conj(tf), s=(self.h, self.w))[:self.h - th + 1, :self.w - tw + 1]
        n = float(th * tw)
        s1 = _box_sums(self.ii, th, tw)
        var = _box_sums(self.ii2, th, tw) - s1 * s1 / n
        out = np.zeros_like(corr)
        good = var > n
        out[good] = corr[good] / (np.sqrt(var[good]) * tn)
        return out

    def crop(self, x0, y0, x1, y1):
        return _Frame(self.img[y0:y1, x0:x1])


def _half(a):
    h, w = a.shape[0] // 2 * 2, a.shape[1] // 2 * 2
    return a[:h, :w].reshape(h // 2, 2, w // 2, 2).mean(axis=(1, 3))


def _gray(rgb):
    rgb = rgb.astype(np.float64)
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def _hp(a):
    return a - _box3(_box3(a))


def _template(iso, k, region=None):
    """Template of the ISO (gray) at scale k: the central PATCH, or region (u0, v0, u1, v1) in ISO pixels."""
    hi, wi = iso.shape
    if region is None:
        tw, th = int(round(PATCH * k * wi)), int(round(PATCH * k * hi))
        u0, v0 = wi / 2.0 - tw / (2.0 * k), hi / 2.0 - th / (2.0 * k)
    else:
        u0, v0, u1, v1 = region
        tw, th = int(round((u1 - u0) * k)), int(round((v1 - v0) * k))
    if tw < 4 or th < 4:
        return None, 0.0, 0.0
    r0, r1 = max(0, int(np.floor(v0))), min(hi, int(np.ceil(v0 + th / k)) + 1)
    c0, c1 = max(0, int(np.floor(u0))), min(wi, int(np.ceil(u0 + tw / k)) + 1)
    my = _resample(th, r1 - r0, k, v0 - r0)
    mx = _resample(tw, c1 - c0, k, u0 - c0)
    return my.dot(iso[r0:r1, c0:c1]).dot(mx.T), u0, v0


def _peak(m, window=None):
    """(value, x, y) of the max of an NCC map, subpixel; window = (x0, y0, x1, y1) on top-left positions."""
    ox = oy = 0
    if window is not None:
        x0, y0 = max(0, int(np.floor(window[0]))), max(0, int(np.floor(window[1])))
        x1, y1 = min(m.shape[1], int(np.ceil(window[2]))), min(m.shape[0], int(np.ceil(window[3])))
        if x1 <= x0 or y1 <= y0:
            return -1.0, 0.0, 0.0
        m = m[y0:y1, x0:x1]
        ox, oy = x0, y0
    y, x = np.unravel_index(int(np.argmax(m)), m.shape)
    v = float(m[y, x])
    fx = _parabola(m[y, x - 1], v, m[y, x + 1]) if 0 < x < m.shape[1] - 1 else 0.0
    fy = _parabola(m[y - 1, x], v, m[y + 1, x]) if 0 < y < m.shape[0] - 1 else 0.0
    return v, ox + x + fx, oy + y + fy


def _scan(frame, iso, ks, region=None, centre=None, radius=None):
    """NCC peak per scale. Returns (values, best index, [(value, cx, cy)]); cx/cy = ISO centre in PGM pixels."""
    hi, wi = iso.shape
    tpls = [(k,) + _template(iso, k, region) for k in ks]
    sub, sx, sy = frame, 0, 0
    if centre is not None:
        # only the neighbourhood of the expected position: correlate on a crop
        pos = [(centre[0] - k * wi / 2.0 + u0 * k, centre[1] - k * hi / 2.0 + v0 * k, t.shape)
               for k, t, u0, v0 in tpls if t is not None]
        if pos:
            sx = max(0, int(np.floor(min(q[0] for q in pos) - radius)) - 1)
            sy = max(0, int(np.floor(min(q[1] for q in pos) - radius)) - 1)
            ex = min(frame.w, int(np.ceil(max(q[0] + q[2][1] for q in pos) + radius)) + 2)
            ey = min(frame.h, int(np.ceil(max(q[1] + q[2][0] for q in pos) + radius)) + 2)
            sub = frame.crop(sx, sy, ex, ey)
    res = []
    for k, tpl, u0, v0 in tpls:
        m = sub.ncc(tpl) if tpl is not None else None
        if m is None:
            res.append((-1.0, 0.0, 0.0))
            continue
        win = None
        if centre is not None:
            px = centre[0] - k * wi / 2.0 + u0 * k - sx
            py = centre[1] - k * hi / 2.0 + v0 * k - sy
            win = (px - radius, py - radius, px + radius + 1, py + radius + 1)
        v, x, y = _peak(m, win)
        x, y = x + sx, y + sy
        res.append((v, x - u0 * k + k * wi / 2.0, y - v0 * k + k * hi / 2.0))
    vals = [r[0] for r in res]
    return vals, int(np.argmax(vals)), res


def _best(ks, vals, i, res, step):
    f = _parabola(vals[i - 1], vals[i], vals[i + 1]) if 0 < i < len(ks) - 1 else 0.0
    return {"ncc": vals[i], "k": float(ks[i] * (1.0 + step * f)), "cx": float(res[i][1]), "cy": float(res[i][2])}


def _locate(frame, iso, hint=None, half=None):
    """Find the ISO (gray) in the PGM frame; hint = placement to search around, else a coarse search over all
    scales on the half-size pair half = (frame, iso). Returns a placement or None."""
    if hint is None:
        f2, i2 = half if half is not None else (_Frame(_half(frame.img)), _half(iso))
        lo, top = MIN_BOX * f2.w / i2.shape[1], MAX_BOX * f2.w / i2.shape[1]
        n = int(np.ceil(np.log(top / lo) / np.log(COARSE_STEP))) + 1
        ks = lo * COARSE_STEP ** np.arange(n)
        vals, i, res = _scan(f2, i2, ks)
        if vals[i] < NCC_MIN - 0.15:
            return None
        k0, centre, radius = ks[i], (2.0 * res[i][1], 2.0 * res[i][2]), 0.04 * frame.w
    else:
        k0, centre, radius = hint["k"], (hint["cx"], hint["cy"]), 0.02 * frame.w
    ks = k0 * (1.0 + FINE_STEP * np.arange(-FINE_N, FINE_N + 1))
    vals, i, res = _scan(frame, iso, ks, None, centre, radius)
    if vals[i] < NCC_MIN:
        return None
    return _best(ks, vals, i, res, FINE_STEP)


def _refine(frame, iso, pl, rect):
    """Re-fit scale/position with the whole visible rect (working px) as template."""
    hi, wi = iso.shape
    k = pl["k"]
    ox, oy = pl["cx"] - k * wi / 2.0, pl["cy"] - k * hi / 2.0
    m = 1.5
    region = ((rect[0] + m - ox) / k, (rect[1] + m - oy) / k, (rect[2] - m - ox) / k, (rect[3] - m - oy) / k)
    region = (max(0.0, region[0]), max(0.0, region[1]), min(float(wi), region[2]), min(float(hi), region[3]))
    if region[2] - region[0] < 8 or region[3] - region[1] < 8:
        return None
    ks = k * (1.0 + REFINE_STEP * np.arange(-REFINE_N, REFINE_N + 1))
    vals, i, res = _scan(frame, iso, ks, region, (pl["cx"], pl["cy"]), 3.0)
    if vals[i] < NCC_MIN:
        return None
    return _best(ks, vals, i, res, REFINE_STEP)


def _origin(pl, iso_shape):
    hi, wi = iso_shape[:2]
    return pl["cx"] - pl["k"] * wi / 2.0, pl["cy"] - pl["k"] * hi / 2.0


def _placed(iso, pl, h, w):
    """The ISO (rgb) resampled into PGM working coordinates at placement pl, plus its coverage."""
    hi, wi = iso.shape[:2]
    k = pl["k"]
    ox, oy = _origin(pl, iso.shape)
    t = np.zeros((h, w, 3))
    cov = np.zeros((h, w))
    y0, y1 = max(0, int(np.floor(oy))), min(h, int(np.ceil(oy + k * hi)))
    x0, x1 = max(0, int(np.floor(ox))), min(w, int(np.ceil(ox + k * wi)))
    if y1 <= y0 or x1 <= x0:
        return t, cov
    my = _resample(y1 - y0, hi, k, (y0 - oy) / k)
    mx = _resample(x1 - x0, wi, k, (x0 - ox) / k)
    src = iso.astype(np.float64)
    for c in range(3):
        t[y0:y1, x0:x1, c] = my.dot(src[..., c]).dot(mx.T)
    cov[y0:y1, x0:x1] = np.outer(my.sum(1), mx.sum(1))
    return t, cov


def _patch_rect(pl, iso_shape, h, w):
    hi, wi = iso_shape[:2]
    k = pl["k"]
    hw, hh = PATCH * k * wi / 2.0, PATCH * k * hi / 2.0
    return (max(0, int(np.ceil(pl["cx"] - hw))), max(0, int(np.ceil(pl["cy"] - hh))),
            min(w, int(pl["cx"] + hw)), min(h, int(pl["cy"] + hh)))


def _grad(a):
    gx = np.zeros_like(a)
    gy = np.zeros_like(a)
    gx[:, 1:-1] = np.abs(a[:, 2:] - a[:, :-2]) * 0.5
    gy[1:-1, :] = np.abs(a[2:, :] - a[:-2, :]) * 0.5
    return np.maximum(gx, gy)


def _max3(a):
    p = np.pad(a, 1, mode="edge")
    out = a.copy()
    for dy in range(3):
        for dx in range(3):
            np.maximum(out, p[dy:dy + a.shape[0], dx:dx + a.shape[1]], out=out)
    return out


def _agreement(pgm, iso, pl):
    """Mask of PGM working pixels explained by the placed ISO (both rgb), per-channel gain/offset fitted on the
    central patch. The tolerance grows with the local gradient (dilated by 1 px) so small misregistration at
    edges, and chroma siting differences between the PGM and ISO paths, still pass."""
    h, w = pgm.shape[:2]
    t, cov = _placed(iso, pl, h, w)
    x0, y0, x1, y1 = _patch_rect(pl, iso.shape, h, w)
    if (x1 - x0) * (y1 - y0) < 16:
        return None, None
    p = pgm.astype(np.float64)
    res = np.zeros((h, w))
    tol = np.zeros((h, w))
    pred = np.zeros_like(t)
    for c in range(3):
        tp, pp = t[y0:y1, x0:x1, c].ravel(), p[y0:y1, x0:x1, c].ravel()
        a, b = 1.0, float(pp.mean() - tp.mean())
        if tp.std() > 2.0:
            fa, fb = np.polyfit(tp, pp, 1)
            if 0.5 < fa < 2.0:
                a, b = float(fa), float(fb)
        pred[..., c] = a * t[..., c] + b
        res = np.maximum(res, np.abs(p[..., c] - pred[..., c]))
        tol = np.maximum(tol, _grad(pred[..., c]))
    r = _box3(res)
    med = float(np.median(r[y0:y1, x0:x1]))
    if med > RES_MAX:
        return None, None  # the ISO does not explain its own centre: a false match
    base = max(8.0, 3.0 * med)
    thr = base + GRAD_TOL * _max3(tol)
    agree = r < thr
    # the smoothing erodes the box by a pixel: take back neighbours that agree on their own
    agree |= (_max3(agree.astype(np.float64)) > 0) & (res < thr)
    return (cov > 0.98) & agree, pred


def _run(prof, start, step, n):
    """Walk from start while the profile is >= 0.5 or unknown (nan); stop after a short run of failing samples
    (single bad lines inside a box and mixed pixels at the border of an overlapping box are skipped)."""
    limit = max(3, int(round(GAP_TOL * n)))
    last, i, fails = start, start, 0
    while 0 <= i < n:
        v = prof[i]
        if np.isnan(v) or v >= 0.5:
            if not np.isnan(v):
                last = i
            fails = 0
        else:
            fails += 1
            if fails >= limit:
                break
        i += step
    return last


def _profile(agree, valid, axis, lo, hi):
    sl = agree[lo:hi, :] if axis == 0 else agree[:, lo:hi]
    vl = valid[lo:hi, :] if axis == 0 else valid[:, lo:hi]
    num = sl.sum(axis).astype(np.float64)
    den = vl.sum(axis).astype(np.float64)
    out = np.full(num.shape, np.nan)
    ok = den >= max(3.0, 0.05 * (hi - lo))
    out[ok] = num[ok] / den[ok]
    return out


def _visible_rect(agree, others, seed):
    """Pixel rect [x0, y0, x1, y1) of the box, grown from the seed (patch) rect along agreement profiles.
    Pixels explained by another box are unknown, not disagreeing, so occluded edges are still found."""
    valid = agree | ~others
    h, w = agree.shape
    x0, y0, x1, y1 = seed
    if x1 <= x0 or y1 <= y0:
        return None
    for _ in range(4):
        cp = _profile(agree, valid, 0, y0, y1)
        c = (x0 + x1) // 2
        nx0, nx1 = _run(cp, c, -1, w), _run(cp, c, 1, w) + 1
        rp = _profile(agree, valid, 1, nx0, nx1)
        c = (y0 + y1) // 2
        ny0, ny1 = _run(rp, c, -1, h), _run(rp, c, 1, h) + 1
        if (nx0, ny0, nx1, ny1) == (x0, y0, x1, y1):
            break
        x0, y0, x1, y1 = nx0, ny0, nx1, ny1
    return x0, y0, x1, y1


def _hp_score(pgm_gray, pred_gray, rect, valid):
    """NCC of high-passed PGM vs placed ISO inside the rect: rejects matches of smooth gradients."""
    x0, y0, x1, y1 = rect
    x0, y0, x1, y1 = x0 + 2, y0 + 2, x1 - 2, y1 - 2
    if x1 - x0 < 4 or y1 - y0 < 4:
        return 0.0
    v = valid[y0:y1, x0:x1]
    a = _hp(pgm_gray)[y0:y1, x0:x1][v]
    b = _hp(pred_gray)[y0:y1, x0:x1][v]
    if a.size < 32:
        return 0.0
    a = a - a.mean()
    b = b - b.mean()
    d = np.sqrt((a * a).sum() * (b * b).sum())
    if d < 1e-9 or np.sqrt((b * b).mean()) < 0.5:
        return 0.0
    return float((a * b).sum() / d)


def _box_geometry(pl, iso_shape, rect, h, w):
    """rect (working px) + placement -> rect fractions and crop fractions, with near-zero crops snapped."""
    hi, wi = iso_shape[:2]
    k = pl["k"]
    ox, oy = _origin(pl, iso_shape)
    ex = [ox, oy, ox + k * wi, oy + k * hi]
    r = [float(v) for v in rect]
    crop = [(r[0] - ex[0]) / (k * wi), (r[1] - ex[1]) / (k * hi),
            (ex[2] - r[2]) / (k * wi), (ex[3] - r[3]) / (k * hi)]
    for i in range(4):
        if crop[i] < CROP_SNAP:
            crop[i] = 0.0
            r[i] = ex[i]
    r = [min(max(r[0] / w, 0.0), 1.0), min(max(r[1] / h, 0.0), 1.0),
         min(max(r[2] / w, 0.0), 1.0), min(max(r[3] / h, 0.0), 1.0)]
    return [round(v, 5) for v in r], [round(min(v, 0.95), 5) for v in crop]


def _same_placement(a, b, iso_shape, h, w):
    wi = iso_shape[1]
    return (abs(a["cx"] - b["cx"]) <= STATIC_TOL * w and abs(a["cy"] - b["cy"]) <= STATIC_TOL * h and
            abs(a["k"] - b["k"]) * wi <= STATIC_TOL * w)


def _mean_placement(pls):
    return {"k": float(np.mean([q["k"] for q in pls])), "cx": float(np.mean([q["cx"] for q in pls])),
            "cy": float(np.mean([q["cy"] for q in pls])), "ncc": float(np.mean([q["ncc"] for q in pls]))}


def _layer_order(boxes):
    """Assign layer 1..n: in each overlap the box whose pixels agree more is on top; else larger is lower."""
    n = len(boxes)
    above = [set() for _ in range(n)]  # above[i] = boxes on top of i
    for i in range(n):
        for j in range(i + 1, n):
            a, b = boxes[i]["px"], boxes[j]["px"]
            x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
            if x1 - x0 < 2 or y1 - y0 < 2:
                continue
            si = int(boxes[i]["mask"][y0:y1, x0:x1].sum())
            sj = int(boxes[j]["mask"][y0:y1, x0:x1].sum())
            if si > sj:
                above[j].add(i)
            elif sj > si:
                above[i].add(j)
    area = [(b["px"][2] - b["px"][0]) * (b["px"][3] - b["px"][1]) for b in boxes]
    order, left = [], set(range(n))
    while left:
        ready = [i for i in left if not any(i in above[j] for j in left if j != i)] or list(left)
        i = max(ready, key=lambda q: area[q])
        order.append(i)
        left.remove(i)
    for layer, i in enumerate(order, 1):
        boxes[i]["layer"] = layer


def _masks_and_rects(found, pgms, isoimgs, mid, H, W):
    """Agreement masks (static boxes: AND over the sample frames) and visible rects for all boxes."""
    for b in found:
        fis = [fi for fi, q in enumerate(b["pls"]) if q is not None] if b["static"] else [mid]
        mask, pred = None, None
        for fi in fis:
            m, pr = _agreement(pgms[fi], isoimgs[b["iso"]][fi], b["pl"])
            if m is None:
                continue
            mask = m if mask is None else (mask & m)
            if fi == mid:
                pred = pr
        b["mask"], b["pred"] = mask, pred
    found = [b for b in found if b["mask"] is not None and b["pred"] is not None]
    for b in found:
        others = np.zeros((H, W), dtype=bool)
        for o in found:
            if o is not b:
                others |= o["mask"]
        b["others"] = others
        b["px"] = _visible_rect(b["mask"], others, _patch_rect(b["pl"], b["shape"], H, W))
    return [b for b in found if b["px"] is not None]


def _iso_job(pool, ffmpeg, fps, iso, p):
    q = p + int(iso["offset_frames"])
    if 0 <= q < int(iso["duration_frames"]):
        return pool.submit(_grab, ffmpeg, iso["path"], fps, q, iso["_work"], "rgb24")
    return None


def _prefetch(pool, ffmpeg, fps, prog, isos, s, e, W, H):
    """Start decoding the PGM sample frames and every ISO's middle frame of a candidate range."""
    L = e - s
    frames = sorted(set(min(e - 1, max(s, s + int(round(L * f)))) for f in SAMPLES))
    mid = len(frames) // 2
    pgm_jobs = [pool.submit(_grab, ffmpeg, prog["path"], fps, p, (W, H), "rgb24") for p in frames]
    mid_jobs = [_iso_job(pool, ffmpeg, fps, iso, frames[mid]) for iso in isos]
    return frames, pgm_jobs, mid_jobs


def _analyse(pool, ffmpeg, fps, isos, s, e, W, H, pre):
    """Detect the composite of one candidate range; returns a layout dict or None."""
    frames, pgm_jobs, mid_jobs = pre
    mid = len(frames) // 2
    pgms = [j.result() for j in pgm_jobs]
    isoimgs = [[None] * len(frames) for _ in isos]
    for ii, j in enumerate(mid_jobs):
        isoimgs[ii][mid] = j.result() if j is not None else None
    if pgms[mid] is None:
        return None
    pf = [_Frame(_gray(p)) if p is not None else None for p in pgms]
    half = _Frame(_half(pf[mid].img))
    isogray = [[None] * len(frames) for _ in isos]

    hits = []
    for ii in range(len(isos)):
        if isoimgs[ii][mid] is None:
            continue
        g = isogray[ii][mid] = _gray(isoimgs[ii][mid])
        pl = _locate(pf[mid], g, None, (half, _half(g)))
        if pl is None:
            continue
        if pl["k"] * g.shape[1] >= 0.97 * W and pl["k"] * g.shape[0] >= 0.97 * H:
            continue  # full frame: a camera shot, not a box
        hits.append((ii, pl))
    if not hits:
        return None
    # second round: the other sample frames, only for the ISOs seen in the middle frame
    jobs = {(ii, fi): _iso_job(pool, ffmpeg, fps, isos[ii], frames[fi])
            for ii, _ in hits for fi in range(len(frames)) if fi != mid}
    for (ii, fi), j in jobs.items():
        isoimgs[ii][fi] = j.result() if j is not None else None
        isogray[ii][fi] = _gray(isoimgs[ii][fi]) if isoimgs[ii][fi] is not None else None

    found = []
    for ii, pl in hits:
        g = isogray[ii][mid]
        pls = [None] * len(frames)
        pls[mid] = pl
        tried = 1
        for fi in range(len(frames)):
            if fi == mid or pf[fi] is None or isogray[ii][fi] is None:
                continue
            tried += 1
            q = _locate(pf[fi], isogray[ii][fi], pl)
            if q is None or not _same_placement(q, pl, g.shape, H, W):
                q = _locate(pf[fi], isogray[ii][fi])
            pls[fi] = q
        seen = sum(1 for q in pls if q is not None)
        if seen < min(2, tried):
            continue  # not there in the other samples: a coincidence, or the composite is not on screen
        static = seen == tried and all(_same_placement(q, pl, g.shape, H, W) for q in pls if q is not None)
        found.append({"iso": ii, "pls": pls, "static": static, "shape": g.shape,
                      "pl": _mean_placement([q for q in pls if q is not None]) if static else pl})
    if not found:
        return None

    found = _masks_and_rects(found, pgms, isoimgs, mid, H, W)
    # refine scale/position on the whole visible area, then recompute masks and rects
    for b in found:
        fis = [fi for fi, q in enumerate(b["pls"]) if q is not None] if b["static"] else [mid]
        ref = [r for r in (_refine(pf[fi], isogray[b["iso"]][fi], b["pl"], b["px"]) for fi in fis) if r]
        if ref:
            b["pl"] = _mean_placement(ref)
    found = _masks_and_rects(found, pgms, isoimgs, mid, H, W)

    pgray = _gray(pgms[mid])
    boxes = []
    for b in found:
        x0, y0, x1, y1 = b["px"]
        if (x1 - x0) * (y1 - y0) < MIN_AREA * W * H:
            continue
        valid = b["mask"] | ~b["others"]
        share = float(b["mask"][y0:y1, x0:x1].sum()) / max(1, int(valid[y0:y1, x0:x1].sum()))
        hp = _hp_score(pgray, _gray(b["pred"]), b["px"], valid)
        if share < AGREE_MIN or hp < HP_MIN:
            continue
        rect, crop = _box_geometry(b["pl"], b["shape"], b["px"], H, W)
        boxes.append({"angle": isos[b["iso"]]["id"], "rect": rect, "crop": crop, "px": b["px"], "mask": b["mask"],
                      "score": b["pl"]["ncc"] * share * min(1.0, hp / 0.8), "static": b["static"],
                      "frame_px": _frame_rects(b, pgms, isoimgs, frames, mid, H, W)})
    if not boxes:
        return None
    covered = np.zeros((H, W), dtype=bool)
    for b in boxes:
        x0, y0, x1, y1 = b["px"]
        covered[y0:y1, x0:x1] = True
    _layer_order(boxes)
    animated = not all(b["static"] for b in boxes)
    conf = float(np.mean([b["score"] for b in boxes]))
    if conf < CONF_MIN or covered.mean() < COVER_MIN:
        return None
    if animated:
        conf *= 0.6  # layout of the middle sample only
    return {"range": [s, e], "frames": frames, "animated": animated, "confidence": conf,
            "boxes": [{"angle": b["angle"], "layer": b["layer"], "rect": b["rect"], "crop": b["crop"],
                       "frame_px": b["frame_px"]} for b in sorted(boxes, key=lambda q: q["layer"])]}


def _frame_rects(b, pgms, isoimgs, frames, mid, H, W):
    """Box rect fractions per sample frame (for background masking); static boxes reuse the common rect."""
    out = {}
    for fi, p in enumerate(frames):
        if b["static"] or fi == mid:
            if b["pls"][fi] is not None or fi == mid:
                px = b["px"]
            else:
                continue
        else:
            pl = b["pls"][fi]
            if pl is None or pgms[fi] is None:
                continue
            mask, _ = _agreement(pgms[fi], isoimgs[b["iso"]][fi], pl)
            if mask is None:
                continue
            px = _visible_rect(mask, b["others"], _patch_rect(pl, b["shape"], H, W))
            if px is None:
                continue
        out[p] = [px[0] / float(W), px[1] / float(H), px[2] / float(W), px[3] / float(H)]
    return out


def _same_layout(a, b):
    if a["animated"] or b["animated"]:
        return False
    if sorted(x["angle"] for x in a["boxes"]) != sorted(x["angle"] for x in b["boxes"]):
        return False
    bb = {x["angle"]: x for x in b["boxes"]}
    for x in a["boxes"]:
        y = bb[x["angle"]]
        if max(abs(u - v) for u, v in zip(x["rect"], y["rect"])) > STATIC_TOL:
            return False
        if max(abs(u - v) for u, v in zip(x["crop"], y["crop"])) > CROP_TOL:
            return False
    return True


def _group(results):
    groups = []
    for r in results:
        for g in groups:
            if _same_layout(g[0], r):
                g.append(r)
                break
        else:
            groups.append([r])
    return groups


def _fill_holes(img, known):
    """Push-pull fill of pixels where known is False (img float32 (h, w, 3))."""
    if known.all():
        return img
    if not known.any():
        img[:] = 128.0
        return img
    levels = []
    v = img * known[..., None]
    wt = known.astype(np.float32)
    while min(wt.shape) > 1:
        levels.append((v, wt))
        h, w = wt.shape
        if h % 2 or w % 2:
            v = np.pad(v, ((0, h % 2), (0, w % 2), (0, 0)), mode="edge")
            wt = np.pad(wt, ((0, h % 2), (0, w % 2)), mode="edge")
        v = v[0::2, 0::2] + v[1::2, 0::2] + v[0::2, 1::2] + v[1::2, 1::2]
        wt = wt[0::2, 0::2] + wt[1::2, 0::2] + wt[0::2, 1::2] + wt[1::2, 1::2]
    cur = v / np.maximum(wt, 1e-6)[..., None]
    if not (wt > 0).all():
        cur[wt <= 0] = (v.sum((0, 1)) / max(wt.sum(), 1e-6))
    for v, wt in reversed(levels):
        h, w = wt.shape
        up = _up2(_up2(cur, 0), 1)[:h, :w]
        alpha = np.minimum(wt, 1.0)[..., None]
        c = v / np.maximum(wt, 1e-6)[..., None]
        cur = alpha * c + (1.0 - alpha) * up
        cur = cur.astype(np.float32)
    out = img.copy()
    out[~known] = _smooth_fill(cur)[~known]
    return out


def _up2(a, axis):
    """2x bilinear upsampling along one axis (pixel-centre aligned, clamped edges)."""
    a = np.moveaxis(a, axis, 0)
    prev = np.concatenate([a[:1], a[:-1]])
    nxt = np.concatenate([a[1:], a[-1:]])
    out = np.empty((2 * a.shape[0],) + a.shape[1:], dtype=a.dtype)
    out[0::2] = 0.75 * a + 0.25 * prev
    out[1::2] = 0.75 * a + 0.25 * nxt
    return np.moveaxis(out, 0, axis)


def _smooth_fill(a):
    """Light blur to soften block edges of the push-pull fill."""
    for _ in range(2):
        p = np.pad(a, ((2, 2), (2, 2), (0, 0)), mode="edge")
        a = sum(p[dy:dy + a.shape[0], dx:dx + a.shape[1]] for dy in range(5) for dx in range(5)) / 25.0
    return a


def _background(pool, ffmpeg, prog, fps, group, pw, ph):
    """Median of box-masked full-res PGM frames over the group's occurrences -> (rgb float32, known mask)."""
    samples = []
    for r in group:
        for p in r["frames"]:
            rects = [b["frame_px"][p] for b in r["boxes"] if p in b["frame_px"]]
            samples.append((p, rects))
    most = max(3, min(BG_MAX_FRAMES, int(BG_MAX_BYTES // (pw * ph * 3))))
    if len(samples) > most:
        idx = np.linspace(0, len(samples) - 1, most).round().astype(int)
        samples = [samples[i] for i in sorted(set(idx))]
    jobs = [pool.submit(_grab, ffmpeg, prog["path"], fps, p, (pw, ph), "rgb24") for p, _ in samples]
    imgs, masks = [], []
    m = int(np.ceil(BG_MARGIN * pw)) + 2
    for (p, rects), j in zip(samples, jobs):
        img = j.result()
        if img is None:
            continue
        mask = np.ones((ph, pw), dtype=bool)
        for x0, y0, x1, y1 in rects:
            a, b = max(0, int(np.floor(x0 * pw)) - m), max(0, int(np.floor(y0 * ph)) - m)
            c, d = min(pw, int(np.ceil(x1 * pw)) + m), min(ph, int(np.ceil(y1 * ph)) + m)
            mask[b:d, a:c] = False
        imgs.append(img)
        masks.append(mask)
    if not imgs:
        return None, None
    stack = np.stack(imgs)                       # (n, h, w, 3) uint8
    mk = np.stack(masks)                         # (n, h, w)
    n = stack.shape[0]
    cnt = mk.sum(0)
    out = np.zeros((ph, pw, 3), dtype=np.float32)
    step = 64
    for y in range(0, ph, step):
        s = stack[:, y:y + step].astype(np.uint16)
        s[~mk[:, y:y + step]] = 1000             # masked samples sort last
        s.sort(axis=0)
        c = cnt[y:y + step]
        lo = np.clip((c - 1) // 2, 0, n - 1)[None, ..., None]
        hi = np.clip(c // 2, 0, n - 1)[None, ..., None]
        lo = np.broadcast_to(lo, (1,) + s.shape[1:])
        hi = np.broadcast_to(hi, (1,) + s.shape[1:])
        med = 0.5 * (np.take_along_axis(s, lo, 0)[0].astype(np.float32) +
                     np.take_along_axis(s, hi, 0)[0].astype(np.float32))
        out[y:y + step] = med
    known = cnt > 0
    out[~known] = 0.0
    return out, known


def detect_supersources(ffmpeg, analysis, candidates, assets_dir, min_len=25, progress=None):
    """Find SuperSource / PiP composites in candidate PGM ranges.

    analysis: v2 dict built so far (fps, program, isos with offset_frames). candidates: [[start, end], ...] PGM
    frame ranges (end exclusive). Writes background PNGs into assets_dir. progress: callable(pct 0..100) or None.
    Returns (supersources, stills) as described in docs/CONTRACT.md ("v2 additions").
    """
    fps = float(analysis["fps"]["num"]) / float(analysis["fps"]["den"])
    prog = analysis["program"]
    pw, ph = int(prog["width"]), int(prog["height"])
    W = min(WORK_W, pw)
    H = max(8, int(round(W * ph / float(pw))))
    isos = []
    for iso in analysis.get("isos", []):
        iw, ih = int(iso.get("width") or 0), int(iso.get("height") or 0)
        if iw <= 0 or ih <= 0:
            continue
        d = dict(iso)
        d["_work"] = (W, max(8, int(round(W * ih / float(iw)))))
        isos.append(d)
    ranges = sorted([int(s), int(e)] for s, e in candidates if int(e) - int(s) >= min_len)
    if not ranges or not isos:
        return [], []

    def report(pct):
        if progress:
            progress(min(100.0, max(0.0, pct)))

    results = []
    workers = max(2, min(8, (os.cpu_count() or 2)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        nxt = _prefetch(pool, ffmpeg, fps, prog, isos, ranges[0][0], ranges[0][1], W, H)
        for n, (s, e) in enumerate(ranges):
            pre = nxt
            if n + 1 < len(ranges):
                nxt = _prefetch(pool, ffmpeg, fps, prog, isos, ranges[n + 1][0], ranges[n + 1][1], W, H)
            r = _analyse(pool, ffmpeg, fps, isos, s, e, W, H, pre)
            if r is not None:
                results.append(r)
            report(80.0 * (n + 1) / len(ranges))
        groups = _group(results)
        if not groups:
            report(100.0)
            return [], []

        bgs = [_background(pool, ffmpeg, prog, fps, g, pw, ph) for g in groups]
        report(90.0)
    # holes of one background filled from another that agrees with it where both are known
    for i, (img, known) in enumerate(bgs):
        if img is None:
            continue
        for j, (oimg, oknown) in enumerate(bgs):
            if i == j or oimg is None or known.all():
                continue
            common = known & oknown
            if common.sum() < 0.2 * known.size:
                continue
            if float(np.abs(img[common] - oimg[common]).mean()) > 6.0:
                continue
            take = ~known & oknown
            img[take] = oimg[take]
            known = known | take
        bgs[i] = (img, known)

    os.makedirs(assets_dir, exist_ok=True)
    supersources, stills = [], []
    nbg = 0
    for gi, (g, (img, known)) in enumerate(zip(groups, bgs), 1):
        sid = "ss%d" % gi
        nbg += 1
        while os.path.exists(os.path.join(assets_dir, "ssbg%d.png" % nbg)):  # never overwrite other files
            nbg += 1
        bid = "ssbg%d" % nbg
        occ = sorted([r["range"] for r in g])
        background = None
        if img is not None:
            rgb = np.clip(np.round(_fill_holes(img, known)), 0, 255).astype(np.uint8)
            path = os.path.abspath(os.path.join(assets_dir, bid + ".png"))
            _write_png(ffmpeg, rgb, path)
            stills.append({"id": bid, "path": path, "width": pw, "height": ph,
                           "occurrences": [list(o) for o in occ], "role": "background"})
            background = bid
        boxes = []
        for b in g[0]["boxes"]:
            same = [next(x for x in r["boxes"] if x["angle"] == b["angle"]) for r in g]
            boxes.append({"angle": b["angle"], "layer": b["layer"],
                          "rect": [round(float(np.mean([x["rect"][i] for x in same])), 5) for i in range(4)],
                          "crop": [round(float(np.mean([x["crop"][i] for x in same])), 5) for i in range(4)]})
        supersources.append({"id": sid, "background": background, "boxes": boxes,
                             "occurrences": [list(o) for o in occ],
                             "animated": any(r["animated"] for r in g),
                             "confidence": round(float(np.mean([r["confidence"] for r in g])), 3)})
    report(100.0)
    return supersources, stills
