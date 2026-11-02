"""Synthetic multicam shoot generator for the engine tests.

A shared "world" timeline is rendered per camera source (lavfi, deterministic). Each ISO records a slice of its
camera's world at 1920x1080; the PGM (1280x720, slight eq) records a slice of the world cut between cameras by
a ground-truth cut list that also contains a 2 s segment of a 4th source that is never given to the engine.
Audio: one seeded master track; each file carries its slice with its own gain and noise.

Usage: python tests/gen_media.py [--force]   (needs ffmpeg via PGM2MC_FFMPEG or imageio-ffmpeg)
"""
import json
import os
import subprocess
import sys
import wave
from fractions import Fraction

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
MEDIA = os.path.join(HERE, "media")
GEN_VERSION = 4
SR = 48000
WORLD_S = 46.0
PGM_START_S = 2.0
PGM_LEN_S = 40.0

SOURCES = {
    "cam1": "testsrc2=s=480x270:r={r}",
    "cam2": "mandelbrot=s=480x270:r={r}",
    "cam3": "life=s=160x90:r={r}:seed=7:mold=10:ratio=0.3:death_color=#203040:life_color=#ffd080,"
            "scale=480:270:flags=neighbor",
    "ext": "cellauto=s=480x270:r={r}:rule=110:seed=3",
}
# world interval recorded by each ISO, seconds
CAMERAS = {"cam1": (0.0, 45.0), "cam2": (3.3, 46.0), "cam3": (1.0, 40.5)}
GAINS = {"cam1": 0.9, "cam2": 0.5, "cam3": 1.3}
# PGM cut list in PGM seconds; "8f" marks the 8-frame shot
CUTS = [(0.0, "cam1"), (4.8, "cam2"), (10.0, "cam3"), ("8f", "cam1"), (16.0, "ext"), (18.0, "cam3"),
        (24.0, "cam2"), (29.0, "cam1"), (33.0, "cam3"), (37.0, "cam2")]

SCENARIOS = {
    "A": {"fps": Fraction(25), "timecode": True},
    "B": {"fps": Fraction(30000, 1001), "timecode": False},
}


def ffmpeg_exe():
    exe = os.environ.get("PGM2MC_FFMPEG")
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg failed: %s\n%s" % (" ".join(cmd), p.stderr.decode()[-2000:]))


def run_parallel(cmds):
    procs = [subprocess.Popen(c, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for c in cmds]
    for c, p in zip(cmds, procs):
        err = p.communicate()[1]
        if p.returncode != 0:
            raise RuntimeError("ffmpeg failed: %s\n%s" % (" ".join(c), err.decode()[-2000:]))


def sec_to_frames(s, fps):
    return int(round(s * fps))


def frame_to_sample(f, fps):
    return int(round(f * SR / fps))


def tc_string(frames, fps):
    nom = int(round(fps))
    ff = frames % nom
    s = frames // nom
    return "%02d:%02d:%02d:%02d" % (s // 3600, (s // 60) % 60, s % 60, ff)


def master_audio(n, seed=1234):
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(n)
    spec = np.fft.rfft(noise)
    freqs = np.fft.rfftfreq(n, 1.0 / SR)
    spec[(freqs < 150) | (freqs > 3500)] = 0
    voiced = np.fft.irfft(spec, n)
    voiced /= np.abs(voiced).max()
    # syllable-like envelope: random gates at ~5 Hz, smoothed, with pauses
    step = SR // 5
    gates = rng.random(n // step + 2)
    gates = np.where(gates < 0.25, 0.0, gates)
    env = np.repeat(gates, step)[:n]
    k = int(0.03 * SR)
    env = np.convolve(env, np.ones(k) / k, mode="same")
    return (0.6 * voiced * env).astype(np.float64)


def write_wav(path, samples, channels=2):
    data = np.clip(samples, -1.0, 1.0)
    data = (data * 32767).astype("<i2")
    if channels == 2:
        data = np.repeat(data[:, None], 2, axis=1)
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())


def cut_frames(fps):
    """Ground-truth segments in PGM frames: list of (start, end, source)."""
    starts = []
    for t, src in CUTS:
        if t == "8f":
            starts.append((starts[-1][0] + 8, src))
        else:
            starts.append((sec_to_frames(t, fps), src))
    total = sec_to_frames(PGM_LEN_S, fps)
    segs = []
    for i, (s, src) in enumerate(starts):
        e = starts[i + 1][0] if i + 1 < len(starts) else total
        segs.append((s, e, src))
    return segs


def generate(name, force=False):
    cfg = SCENARIOS[name]
    fps = cfg["fps"]
    rate = "%d/%d" % (fps.numerator, fps.denominator)
    out = os.path.join(MEDIA, name)
    truth_path = os.path.join(out, "truth.json")
    if not force and os.path.isfile(truth_path):
        with open(truth_path) as f:
            if json.load(f).get("gen_version") == GEN_VERSION:
                return truth_path
    os.makedirs(out, exist_ok=True)
    ff = ffmpeg_exe()
    base = [ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    world_frames = sec_to_frames(WORLD_S, fps)

    # world clips per source, lossless
    cmds = []
    for src, spec in SOURCES.items():
        cmds.append(base + ["-f", "lavfi", "-i", spec.format(r=rate), "-frames:v", str(world_frames),
                            "-pix_fmt", "yuv420p", "-c:v", "ffv1", os.path.join(out, "world_%s.mkv" % src)])
    run_parallel(cmds)

    master = master_audio(frame_to_sample(world_frames, fps) + SR)
    rng = np.random.default_rng(99)
    p0 = sec_to_frames(PGM_START_S, fps)
    plen = sec_to_frames(PGM_LEN_S, fps)
    tc_base = 3600 * int(round(fps))  # world frame 0 = 01:00:00:00
    truth = {"gen_version": GEN_VERSION, "fps": [fps.numerator, fps.denominator], "pgm_frames": plen,
             "isos": {}, "segments": []}

    cmds = []
    for cam, (s0, s1) in CAMERAS.items():
        a, b = sec_to_frames(s0, fps), sec_to_frames(s1, fps)
        sa, sb = frame_to_sample(a, fps), frame_to_sample(b, fps)
        audio = GAINS[cam] * master[sa:sb] + 0.02 * rng.standard_normal(sb - sa)
        wav = os.path.join(out, "%s.wav" % cam)
        write_wav(wav, audio)
        path = os.path.join(out, "%s.mov" % cam)
        cmd = base + ["-i", os.path.join(out, "world_%s.mkv" % cam), "-i", wav,
                      "-vf", "trim=start_frame=%d:end_frame=%d,setpts=PTS-STARTPTS,scale=1920:1080" % (a, b),
                      "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                      "-g", str(2 * int(round(fps))), "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le"]
        if cfg["timecode"]:
            cmd += ["-timecode", tc_string(tc_base + a, fps)]
        cmds.append(cmd + [path])
        truth["isos"][cam] = {"path": path, "offset": p0 - a, "frames": b - a,
                              "start_tc_frames": (tc_base + a) if cfg["timecode"] else 0}

    segs = cut_frames(fps)
    truth["segments"] = [[s, e, src] for s, e, src in segs]
    inputs, chains = [], []
    for i, (s, e, src) in enumerate(segs):
        inputs += ["-i", os.path.join(out, "world_%s.mkv" % src)]
        chains.append("[%d:v]trim=start_frame=%d:end_frame=%d,setpts=PTS-STARTPTS,scale=1280:720[v%d]"
                      % (i, p0 + s, p0 + e, i))
    graph = ";".join(chains) + ";" + "".join("[v%d]" % i for i in range(len(segs))) + \
        "concat=n=%d:v=1:a=0,eq=brightness=0.03:contrast=1.08:saturation=1.2,format=yuv420p[v]" % len(segs)
    sa = frame_to_sample(p0, fps)
    pgm_audio = 0.8 * master[sa:sa + frame_to_sample(plen, fps)] + 0.005 * rng.standard_normal(
        frame_to_sample(plen, fps))
    pwav = os.path.join(out, "pgm.wav")
    write_wav(pwav, pgm_audio)
    pgm = os.path.join(out, "PGM.mov")
    cmd = base + inputs + ["-i", pwav, "-filter_complex", graph, "-map", "[v]", "-map", "%d:a" % len(segs),
                           "-r", rate, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt",
                           "yuv420p", "-c:a", "pcm_s16le"]
    if cfg["timecode"]:
        cmd += ["-timecode", tc_string(tc_base + p0, fps)]
    cmds.append(cmd + [pgm])
    run_parallel(cmds)
    truth["pgm"] = {"path": pgm, "start_tc_frames": (tc_base + p0) if cfg["timecode"] else 0}

    for f in os.listdir(out):
        if f.endswith(".wav") or f.startswith("world_"):
            os.remove(os.path.join(out, f))
    with open(truth_path, "w") as f:
        json.dump(truth, f, indent=1)
    return truth_path


if __name__ == "__main__":
    force = "--force" in sys.argv
    for n in SCENARIOS:
        print(generate(n, force))
