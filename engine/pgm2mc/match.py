"""Per-frame angle scores and Viterbi smoothing into segments."""
import numpy as np

from .frames import downsample, normalise

DEFAULT_PGM_FLOOR = 0.5
DEFAULT_SWITCH_PENALTY = 1.5
DEFAULT_MIN_SHOT = 6
CHUNK = 256


def compute_scores(pgm_reader, iso_readers, radius=0, chunk=CHUNK, progress=None, store=None):
    """Score every PGM frame against every ISO at shifts -radius..+radius.

    Each ISO reader must start at ISO-local frame (offset - radius). Returns float32 array
    (frames, n_isos, 2*radius+1); score = max(0, NCC) of normalised thumbnails, 0 where the ISO has no media.
    Only the score matrix is kept; thumbnails are streamed chunk by chunk. With a frames.ThumbStore, 32x18
    thumbnails are also appended to it: "pgm", "pgm_full" (reader size) and "iso<k>" (the raw ISO stream from
    ISO-local frame offset - radius, so PGM frame t at shift j is stream frame t + j).
    """
    span = 2 * radius
    carries = []
    for r in iso_readers:
        f, v = r.read(span)
        if store is not None and span:
            store.append("iso%d" % len(carries), downsample(f), v)
        carries.append((normalise(f), v))
    parts = []
    total = 0
    while True:
        pf, pv = pgm_reader.read(chunk)
        got = int(pv.sum())
        if got == 0:
            break
        p = normalise(pf[:got])
        if store is not None:
            store.append("pgm", downsample(pf[:got]))
            store.append("pgm_full", pf[:got])
        block = np.zeros((got, len(iso_readers), span + 1), dtype=np.float32)
        for k, r in enumerate(iso_readers):
            f, v = r.read(got)
            if store is not None:
                store.append("iso%d" % k, downsample(f), v)
            cf, cv = carries[k]
            buf = np.concatenate([cf, normalise(f)])
            val = np.concatenate([cv, v])
            for j in range(span + 1):
                s = np.einsum("ij,ij->i", p, buf[j:j + got])
                s[~val[j:j + got]] = 0.0
                block[:, k, j] = s
            carries[k] = (buf[got:], val[got:])
        np.clip(block, 0.0, 1.0, out=block)
        parts.append(block)
        total += got
        if progress:
            progress(total)
        if got < chunk:
            break
    if not parts:
        return np.zeros((0, len(iso_readers), span + 1), dtype=np.float32)
    return np.concatenate(parts)


def viterbi(scores, pgm_floor=DEFAULT_PGM_FLOOR, switch_penalty=DEFAULT_SWITCH_PENALTY,
            min_shot=DEFAULT_MIN_SHOT):
    """Best label path over states {0..n_isos-1, n_isos = pgm}.

    Maximises sum(score of chosen state) - switch_penalty * switches, with every shot at least `min_shot`
    frames (the first and last shot may be cut short by the PGM boundaries).
    """
    T, n = scores.shape
    S = n + 1
    if T == 0:
        return np.zeros(0, dtype=np.int32)
    m = max(1, int(min_shot))
    emit = np.empty((T, S), dtype=np.float64)
    emit[:, :n] = scores
    emit[:, n] = pgm_floor
    # V[s, k]: best total ending at this frame in state s, k+1 frames into the shot (k = m-1 means >= m)
    V = np.full((S, m), -np.inf)
    V[:, m - 1] = emit[0]
    # back[t, s]: previous state for entering (s, k=0) at frame t (-1 never used); stay[t, s]: (s, m-1) came
    # from (s, m-1) rather than (s, m-2)
    back = np.zeros((T, S), dtype=np.int16)
    stay = np.zeros((T, S), dtype=bool)
    idx = np.arange(S)
    for t in range(1, T):
        e = emit[t]
        last = V[:, m - 1]
        order = np.argsort(last)
        best, second = order[-1], order[-2] if S > 1 else order[-1]
        prev = np.where(idx == best, second, best)
        enter = last[prev] - switch_penalty
        Vn = np.empty_like(V)
        if m == 1:
            st = last >= enter
            Vn[:, 0] = np.where(st, last, enter) + e
            stay[t] = st
        else:
            Vn[:, 0] = enter + e
            if m > 2:
                Vn[:, 1:m - 1] = V[:, 0:m - 2] + e[:, None]
            st = last >= V[:, m - 2]
            Vn[:, m - 1] = np.where(st, last, V[:, m - 2]) + e
            stay[t] = st
        back[t] = prev
        V = Vn
    labels = np.empty(T, dtype=np.int32)
    s, k = np.unravel_index(int(np.argmax(V)), V.shape)
    for t in range(T - 1, -1, -1):
        labels[t] = s
        if t == 0:
            break
        if m == 1:
            if not stay[t, s]:
                s = back[t, s]
        elif k == 0:
            s, k = back[t, s], m - 1
        elif k == m - 1:
            if not stay[t, s]:
                k = m - 2
        else:
            k -= 1
    return labels


def segments_from_labels(labels, scores, ids):
    """Run-length encode labels into contract segments. `ids`: ISO ids; label len(ids) is 'pgm'."""
    out = []
    T = len(labels)
    n = len(ids)
    start = 0
    for t in range(1, T + 1):
        if t == T or labels[t] != labels[start]:
            lab = int(labels[start])
            if lab == n:
                best = scores[start:t].max(axis=1) if n else np.zeros(t - start)
                score = 1.0 - float(best.mean())
                angle = "pgm"
            else:
                score = float(scores[start:t, lab].mean())
                angle = ids[lab]
            out.append({"start": start, "end": t, "angle": angle, "score": round(score, 4)})
            start = t
    return out
