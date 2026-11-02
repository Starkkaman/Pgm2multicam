"""Synthetic SuperSource shoot for tests/test_supersource.py.

Three cameras (lavfi, deterministic, distinct moving content) are rendered over a shared "world" timeline. Each ISO
records a slice of its camera at 1920x1080 with its own start timecode. The 1280x720 PGM (25 fps) is cut from:
full-frame cameras, a 2-box SuperSource over a background art fill (shown twice with the same layout), a 3-box
SuperSource with overlapping boxes, and a full-screen graphic. Boxes are built like a switcher would: the camera
at ISO resolution is cropped, scaled and overlaid on the art at even pixel positions.

Usage: python tests/gen_ss.py [--force]   (needs ffmpeg via PGM2MC_FFMPEG or imageio-ffmpeg)
"""
import json
import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "media", "ss")
GEN_VERSION = 3
FPS = 25
PW, PH = 1280, 720
IW, IH = 1920, 1080
WORLD_FRAMES = 22 * FPS
P0 = 2 * FPS  # PGM frame p = world frame p + P0

SOURCES = {
    "cam1": "testsrc2=s=480x270:r=25",
    "cam2": "mandelbrot=s=480x270:r=25",
    "cam3": "life=s=160x90:r=25:seed=7:mold=10:ratio=0.3:death_color=#203040:life_color=#ffd080,"
            "scale=480:270:flags=neighbor",
}
# world frame range recorded by each ISO
CAMERAS = {"cam1": (0, 22 * FPS), "cam2": (25, 22 * FPS), "cam3": (75, 21 * FPS)}

# boxes: (camera, crop px [l, t, r, b] on the ISO frame, box size in PGM px (w, h), position (x, y)); bottom first
SS_A = [("cam1", (200, 0, 200, 0), (456, 324), (160, 198)),
        ("cam2", (200, 0, 200, 0), (456, 324), (664, 198))]
SS_B = [("cam3", (0, 0, 0, 0), (768, 432), (60, 60)),
        ("cam1", (96, 108, 0, 108), (456, 216), (700, 120)),
        ("cam2", (280, 0, 100, 0), (462, 324), (640, 340))]

# animated: one box sliding right by MOVE_PX per frame (x = start x + MOVE_PX * frame index in the segment)
SS_M = [("cam2", (0, 0, 0, 0), (512, 288), (200, 216))]
MOVE_PX = 6

# PGM timeline in frames: (start, end, kind, arg)
TIMELINE = [(0, 50, "cam", "cam1"), (50, 150, "ss", "A"), (150, 200, "cam", "cam3"), (200, 275, "ss", "B"),
            (275, 325, "graphic", None), (325, 425, "ss", "A"), (425, 450, "cam", "cam2"), (450, 500, "ss", "M")]
LAYOUTS = {"A": SS_A, "B": SS_B, "M": SS_M}


def ffmpeg_exe():
    exe = os.environ.get("PGM2MC_FFMPEG")
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def run_parallel(cmds):
    procs = [subprocess.Popen(c, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for c in cmds]
    for c, p in zip(cmds, procs):
        err = p.communicate()[1]
        if p.returncode != 0:
            raise RuntimeError("ffmpeg failed: %s\n%s" % (" ".join(c), err.decode()[-2000:]))


def tc_string(frames):
    s, ff = divmod(frames, FPS)
    return "%02d:%02d:%02d:%02d" % (s // 3600, (s // 60) % 60, s % 60, ff)


def background_art():
    """Gradient art with soft shapes and text-like bars (uint8 RGB, PH x PW)."""
    y, x = np.mgrid[0:PH, 0:PW].astype(np.float64)
    r = 30 + 90 * x / PW + 20 * y / PH
    g = 20 + 40 * y / PH
    b = 90 + 100 * (1 - x / PW) + 30 * y / PH
    img = np.stack([r, g, b], axis=-1)
    for cx, cy, rad, col in [(1100, 120, 160, (60, 40, 20)), (200, 620, 220, (-20, 30, 50))]:
        d = np.sqrt((x - cx) ** 2 + (y - cy) ** 2)
        img += np.clip(1 - d / rad, 0, 1)[..., None] * np.array(col, dtype=np.float64)
    for i in range(6):  # "title" bars along the top and bottom
        x0 = 80 + i * 190
        img[24:44, x0:x0 + 120 + (i * 37) % 50] = (230, 230, 240)
        img[680:692, x0 + 20:x0 + 90] = (250, 200, 80)
    return np.clip(np.round(img), 0, 255).astype(np.uint8)


def graphic_art():
    y, x = np.mgrid[0:PH, 0:PW].astype(np.float64)
    img = np.stack([200 - 80 * y / PH, 60 + 0 * x, 40 + 60 * x / PW], axis=-1)
    for i in range(5):
        img[140 + i * 100:180 + i * 100, 200:200 + 160 * (i + 2)] = (250, 250, 250)
    return np.clip(np.round(img), 0, 255).astype(np.uint8)


def write_png(ff, rgb, path):
    h, w = rgb.shape[:2]
    p = subprocess.run([ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
                        "-pix_fmt", "rgb24", "-s", "%dx%d" % (w, h), "-i", "-", "-frames:v", "1", "-update", "1",
                        path], input=rgb.tobytes(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode()[-2000:])


def truth_layout(boxes):
    out = []
    for layer, (cam, (cl, ct, cr, cb), (bw, bh), (x, y)) in enumerate(boxes, 1):
        out.append({"cam": cam, "layer": layer,
                    "rect": [x / float(PW), y / float(PH), (x + bw) / float(PW), (y + bh) / float(PH)],
                    "crop": [cl / float(IW), ct / float(IH), cr / float(IW), cb / float(IH)]})
    return out


def generate(force=False):
    truth_path = os.path.join(OUT, "truth.json")
    if not force and os.path.isfile(truth_path):
        with open(truth_path) as f:
            if json.load(f).get("gen_version") == GEN_VERSION:
                return truth_path
    os.makedirs(OUT, exist_ok=True)
    ff = ffmpeg_exe()
    base = [ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]

    cmds = []
    for src, spec in SOURCES.items():
        cmds.append(base + ["-f", "lavfi", "-i", spec, "-frames:v", str(WORLD_FRAMES), "-pix_fmt", "yuv420p",
                            "-c:v", "ffv1", os.path.join(OUT, "world_%s.mkv" % src)])
    run_parallel(cmds)

    bg = background_art()
    bg.tofile(os.path.join(OUT, "background.rgb"))
    bg_png = os.path.join(OUT, "background.png")
    write_png(ff, bg, bg_png)
    gfx_png = os.path.join(OUT, "graphic.png")
    write_png(ff, graphic_art(), gfx_png)

    truth = {"gen_version": GEN_VERSION, "fps": FPS, "pgm_frames": TIMELINE[-1][1], "pgm_size": [PW, PH],
             "isos": {}, "timeline": [], "background": os.path.join(OUT, "background.rgb")}
    cmds = []
    for cam, (a, b) in CAMERAS.items():
        path = os.path.join(OUT, "%s.mov" % cam)
        cmds.append(base + ["-i", os.path.join(OUT, "world_%s.mkv" % cam),
                            "-vf", "trim=start_frame=%d:end_frame=%d,setpts=PTS-STARTPTS,scale=%d:%d"
                            % (a, b, IW, IH),
                            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-g", "50",
                            "-pix_fmt", "yuv420p", "-timecode", tc_string(3600 * FPS + a), path])
        truth["isos"][cam] = {"path": path, "offset": P0 - a, "frames": b - a, "width": IW, "height": IH,
                              "start_tc_frames": 3600 * FPS + a}

    # Each segment is rendered on its own (lossless FFV1) and the PGM is encoded once from the
    # concat demuxer: a single 16-input filter graph deadlocks on ffmpeg 9.
    seg_paths = []
    for si, (s, e, kind, arg) in enumerate(TIMELINE):
        inputs, chains = [], []

        def add_input(args):
            inputs.extend(args)
            return inputs.count("-i") - 1

        n = e - s
        lab = "s%d" % si
        trim = "trim=start_frame=%d:end_frame=%d,setpts=PTS-STARTPTS" % (P0 + s, P0 + e)
        if kind == "cam":
            i = add_input(["-i", os.path.join(OUT, "world_%s.mkv" % arg)])
            chains.append("[%d:v]%s,scale=%d:%d,format=yuv420p[%s]" % (i, trim, PW, PH, lab))
        elif kind == "graphic":
            i = add_input(["-loop", "1", "-framerate", str(FPS), "-t", "%.3f" % (n / float(FPS)), "-i", gfx_png])
            chains.append("[%d:v]trim=end_frame=%d,setpts=PTS-STARTPTS,format=yuv420p[%s]" % (i, n, lab))
        else:
            i = add_input(["-loop", "1", "-framerate", str(FPS), "-t", "%.3f" % (n / float(FPS)), "-i", bg_png])
            chains.append("[%d:v]trim=end_frame=%d,setpts=PTS-STARTPTS,format=yuv420p[%sb0]" % (i, n, lab))
            cur = "%sb0" % lab
            for bi, (cam, (cl, ct, cr, cb), (bw, bh), (x, y)) in enumerate(LAYOUTS[arg], 1):
                j = add_input(["-i", os.path.join(OUT, "world_%s.mkv" % cam)])
                chains.append("[%d:v]%s,scale=%d:%d,crop=%d:%d:%d:%d,scale=%d:%d,eq=contrast=1.04:brightness=0.015"
                              "[%sx%d]" % (j, trim, IW, IH, IW - cl - cr, IH - ct - cb, cl, ct, bw, bh, lab, bi))
                nxt = "%sb%d" % (lab, bi)
                xs = "%d+%d*t" % (x, MOVE_PX * FPS) if arg == "M" else str(x)
                chains.append("[%s][%sx%d]overlay=x='%s':y=%d:eof_action=pass[%s]" % (cur, lab, bi, xs, y, nxt))
                cur = nxt
            chains.append("[%s]null[%s]" % (cur, lab))
        seg = os.path.join(OUT, "seg_%02d.mkv" % si)
        seg_paths.append(seg)
        cmds.append(base + inputs + ["-filter_complex", ";".join(chains), "-map", "[%s]" % lab,
                                     "-frames:v", str(n), "-r", str(FPS), "-c:v", "ffv1",
                                     "-pix_fmt", "yuv420p", seg])
        entry = {"start": s, "end": e, "kind": kind, "arg": arg}
        if kind == "ss":
            entry["layout"] = truth_layout(LAYOUTS[arg])
            entry["animated"] = arg == "M"
            if arg == "M":
                entry["move_px"] = MOVE_PX
        truth["timeline"].append(entry)

    run_parallel(cmds)
    seg_list = os.path.join(OUT, "segments.txt")
    with open(seg_list, "w") as f:
        for seg in seg_paths:
            f.write("file '%s'\n" % seg.replace("'", "'\\''"))
    pgm = os.path.join(OUT, "PGM.mov")
    run_parallel([base + ["-f", "concat", "-safe", "0", "-i", seg_list, "-map", "0:v", "-r", str(FPS),
                          "-frames:v", str(TIMELINE[-1][1]), "-c:v", "libx264",
                          "-preset", "ultrafast", "-crf", "18", "-g", "50", "-pix_fmt", "yuv420p",
                          "-timecode", tc_string(3600 * FPS + P0), pgm]])
    truth["pgm"] = {"path": pgm, "start_tc_frames": 3600 * FPS + P0}

    for f in os.listdir(OUT):
        if f.startswith(("world_", "seg_")) or f == "segments.txt":
            os.remove(os.path.join(OUT, f))
    with open(truth_path, "w") as f:
        json.dump(truth, f, indent=1)
    return truth_path


if __name__ == "__main__":
    print(generate("--force" in sys.argv))
