"""Temporary dev entry point: python -m pgm2mc._analyze_cli PGM ISO... [--json out.json]"""
import argparse
import json
import sys

from .analyze import analyze
from .probe import EngineError
from .sync import parse_manual_offsets


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pgm2mc._analyze_cli")
    ap.add_argument("pgm")
    ap.add_argument("isos", nargs="+")
    ap.add_argument("--json", help="write the Analysis JSON here (default: stdout)")
    ap.add_argument("--sync", default="auto", choices=["auto", "timecode", "audio"])
    ap.add_argument("--offset", action="append", default=[], metavar="NAME=+FRAMES")
    ap.add_argument("--min-shot", type=int, default=6)
    ap.add_argument("--switch-penalty", type=float, default=1.5)
    ap.add_argument("--pgm-floor", type=float, default=0.5)
    ap.add_argument("--thumb", default="64x36", help="thumbnail size WxH")
    ap.add_argument("--hwaccel")
    ap.add_argument("--audio-window", type=float, default=1200.0, help="seconds of audio used for sync")
    ap.add_argument("--allow-rate-mismatch", action="store_true")
    ap.add_argument("--refine", action="store_true")
    ap.add_argument("--ffmpeg")
    ap.add_argument("--ffprobe")
    a = ap.parse_args(argv)
    try:
        w, h = (int(v) for v in a.thumb.lower().split("x"))
        result = analyze(a.pgm, a.isos, ffmpeg=a.ffmpeg, ffprobe=a.ffprobe, sync=a.sync,
                         offsets=parse_manual_offsets(a.offset), min_shot=a.min_shot,
                         switch_penalty=a.switch_penalty, pgm_floor=a.pgm_floor, thumb_size=(w, h),
                         hwaccel=a.hwaccel, audio_window=a.audio_window,
                         allow_rate_mismatch=a.allow_rate_mismatch, refine=a.refine)
    except (EngineError, ValueError) as e:
        sys.stderr.write("ERROR %s\n" % e)
        return 1
    text = json.dumps(result, indent=2)
    if a.json:
        with open(a.json, "w") as f:
            f.write(text + "\n")
    else:
        sys.stdout.write(text + "\n")
    for wmsg in result["warnings"]:
        sys.stderr.write("WARNING %s\n" % wmsg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
