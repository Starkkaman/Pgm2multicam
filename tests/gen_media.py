"""Synthetic multicam shoot generator for the engine tests.

A shared "world" timeline is rendered per camera source (lavfi, deterministic). Each ISO records a slice of its
camera's world at 1920x1080; the PGM (1280x720, slight eq) records a slice of the world cut between cameras by
a ground-truth cut list that also contains a 2 s segment of a 4th source that is never given to the engine.
Audio: one seeded master track; each file carries its slice with its own gain and noise.

Scenario C (Analysis v2) adds transitions and graphics: cross-dissolves (xfade), a dip to black (fade out/in),
a full-screen graphic shown three times (once dissolving in from a camera) and a second graphic shown once.
Cameras get the eq there, graphics do not, so the extracted still can be compared with the source image.

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
# v2 scenarios live apart so the v1 CLI/FCPXML tests keep iterating over A and B only
SCENARIOS_V2 = {
    "C": {"fps": Fraction(25), "timecode": True},
}
GEN_VERSION_C = 1
C_FRAMES = 900
# (source, nominal start frame); gfx sources are still images
C_SHOTS = [("cam1", 0), ("cam2", 100), ("gfx1", 200), ("cam3", 238), ("cam1", 340), ("gfx1", 450), ("cam2", 488),
           ("cam3", 600), ("gfx2", 700), ("cam1", 738), ("gfx1", 800), ("cam2", 838)]
# nominal boundary -> (kind, duration frames). dissolve: mix runs [b - d//2, b - d//2 + d); dip: fade out over
# [b - d/2, b), fade in over [b, b + d/2)
C_TRANSITIONS = {100: ("dissolve", 25), 340: ("dip", 20), 450: ("dissolve", 14), 600: ("dissolve", 25)}
GFX = {
    "gfx1": "color=c=0x203060:s=1280x720,format=gbrp,"
            "geq=r='40+150*X/W':g='60+120*Y/H':b='170-90*X/W',"
            "drawbox=x=100:y=500:w=1080:h=140:color=0xf0f0f0:t=fill,"
            "drawbox=x=140:y=530:w=600:h=80:color=0xc03020:t=fill,"
            "drawbox=x=900:y=80:w=280:h=280:color=0x30a050:t=fill",
    "gfx2": "color=c=black:s=1280x720,format=gbrp,"
            "geq=r='200-150*Y/H':g='110+90*sin(X/90)':b='70+60*cos(Y/70)',"
            "drawbox=x=80:y=80:w=400:h=560:color=0x101010:t=fill",
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


def c_plan():
    """Scenario C ground truth: shots with their rendered frame ranges, and transitions."""
    bounds = [b for _, b in C_SHOTS[1:]]
    shots = []
    for i, (src, b) in enumerate(C_SHOTS):
        end = C_SHOTS[i + 1][1] if i + 1 < len(C_SHOTS) else C_FRAMES
        r0, r1 = b, end
        tl = C_TRANSITIONS.get(b)
        if tl and tl[0] == "dissolve":
            r0 = b - tl[1] // 2
        tr = C_TRANSITIONS.get(end)
        if tr and tr[0] == "dissolve":
            r1 = end - tr[1] // 2 + tr[1]
        shots.append({"source": src, "start": b, "end": end, "render": [r0, r1]})
    trans = []
    for b in bounds:
        if b in C_TRANSITIONS:
            kind, d = C_TRANSITIONS[b]
            start = b - d // 2
            trans.append({"kind": kind, "start": start, "duration": d, "centre": start + d / 2.0, "boundary": b})
    return shots, trans


def generate_c(name, force=False):
    cfg = SCENARIOS_V2[name]
    fps = cfg["fps"]
    rate = "%d/%d" % (fps.numerator, fps.denominator)
    out = os.path.join(MEDIA, name)
    truth_path = os.path.join(out, "truth.json")
    if not force and os.path.isfile(truth_path):
        with open(truth_path) as f:
            if json.load(f).get("gen_version") == GEN_VERSION_C:
                return truth_path
    os.makedirs(out, exist_ok=True)
    ff = ffmpeg_exe()
    base = [ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    world_frames = sec_to_frames(WORLD_S, fps)
    cmds = []
    for src in CAMERAS:
        cmds.append(base + ["-f", "lavfi", "-i", SOURCES[src].format(r=rate), "-frames:v", str(world_frames),
                            "-pix_fmt", "yuv420p", "-c:v", "ffv1", os.path.join(out, "world_%s.mkv" % src)])
    for g, spec in GFX.items():
        cmds.append(base + ["-f", "lavfi", "-i", spec, "-frames:v", "1", "-pix_fmt", "rgb24",
                            os.path.join(out, "%s.png" % g)])
    run_parallel(cmds)

    master = master_audio(frame_to_sample(world_frames, fps) + SR)
    rng = np.random.default_rng(77)
    p0 = sec_to_frames(PGM_START_S, fps)
    plen = C_FRAMES
    tc_base = 3600 * int(round(fps))
    shots, trans = c_plan()
    truth = {"gen_version": GEN_VERSION_C, "fps": [fps.numerator, fps.denominator], "pgm_frames": plen,
             "isos": {}, "shots": shots, "transitions": trans,
             "gfx": {g: os.path.join(out, "%s.png" % g) for g in GFX}}
    cmds = []
    for cam, (s0, s1) in CAMERAS.items():
        a, b = sec_to_frames(s0, fps), sec_to_frames(s1, fps)
        sa, sb = frame_to_sample(a, fps), frame_to_sample(b, fps)
        wav = os.path.join(out, "%s.wav" % cam)
        write_wav(wav, GAINS[cam] * master[sa:sb] + 0.02 * rng.standard_normal(sb - sa))
        path = os.path.join(out, "%s.mov" % cam)
        cmds.append(base + ["-i", os.path.join(out, "world_%s.mkv" % cam), "-i", wav,
                            "-vf", "trim=start_frame=%d:end_frame=%d,setpts=PTS-STARTPTS,scale=1920:1080" % (a, b),
                            "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                            "-g", str(2 * int(round(fps))), "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le",
                            "-timecode", tc_string(tc_base + a, fps), path])
        truth["isos"][cam] = {"path": path, "offset": p0 - a, "frames": b - a, "start_tc_frames": tc_base + a}

    inputs, chains = [], []
    tb = "settb=expr=%d/%d,setpts=N" % (fps.denominator, fps.numerator)
    for i, sh in enumerate(shots):
        r0, r1 = sh["render"]
        if sh["source"].startswith("gfx"):
            inputs += ["-loop", "1", "-framerate", rate, "-i", truth["gfx"][sh["source"]]]
            chain = "[%d:v]trim=end_frame=%d,format=yuv420p,%s" % (i, r1 - r0, tb)
        else:
            inputs += ["-i", os.path.join(out, "world_%s.mkv" % sh["source"])]
            chain = ("[%d:v]trim=start_frame=%d:end_frame=%d,setpts=PTS-STARTPTS,scale=1280:720,"
                     "eq=brightness=0.03:contrast=1.08:saturation=1.2,format=yuv420p,%s"
                     % (i, p0 + r0, p0 + r1, tb))
        tl, tr = C_TRANSITIONS.get(sh["start"]), C_TRANSITIONS.get(sh["end"])
        if tl and tl[0] == "dip":
            chain += ",fade=t=in:start_frame=0:nb_frames=%d" % (tl[1] // 2)
        if tr and tr[0] == "dip":
            chain += ",fade=t=out:start_frame=%d:nb_frames=%d" % (r1 - r0 - tr[1] // 2, tr[1] // 2)
        chains.append(chain + "[p%d]" % i)
    cur = "p0"
    for i in range(1, len(shots)):
        sh = shots[i]
        tl = C_TRANSITIONS.get(sh["start"])
        nxt = "j%d" % i
        if tl and tl[0] == "dissolve":
            chains.append("[%s][p%d]xfade=transition=fade:duration=%.6f:offset=%.6f,%s[%s]"
                          % (cur, i, tl[1] / float(fps), sh["render"][0] / float(fps), tb, nxt))
        else:
            chains.append("[%s][p%d]concat=n=2:v=1:a=0,%s[%s]" % (cur, i, tb, nxt))
        cur = nxt
    chains.append("[%s]format=yuv420p[v]" % cur)
    sa = frame_to_sample(p0, fps)
    n_s = frame_to_sample(plen, fps)
    pwav = os.path.join(out, "pgm.wav")
    write_wav(pwav, 0.8 * master[sa:sa + n_s] + 0.005 * rng.standard_normal(n_s))
    pgm = os.path.join(out, "PGM.mov")
    cmds.append(base + inputs + ["-i", pwav, "-filter_complex", ";".join(chains), "-map", "[v]",
                                 "-map", "%d:a" % len(shots), "-frames:v", str(plen), "-r", rate,
                                 "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p",
                                 "-c:a", "pcm_s16le", "-timecode", tc_string(tc_base + p0, fps), pgm])
    run_parallel(cmds)
    truth["pgm"] = {"path": pgm, "start_tc_frames": tc_base + p0}
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
    for n in SCENARIOS_V2:
        print(generate_c(n, force))
