"""Command line: python -m pgm2mc PGM ISO [ISO ...] -o OUTDIR (see docs/CONTRACT.md, "CLI contract")."""
import argparse
import json
import os
import sys

from . import fcpxml, match
from .probe import EngineError


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write("ERROR %s\n" % message)
        sys.stderr.flush()
        sys.exit(2)


def build_parser():
    ap = _Parser(prog="pgm2mc", description="Rebuild a live multicam cut from a PGM recording and its ISOs.")
    ap.add_argument("pgm", nargs="?", help="program (live cut) recording")
    ap.add_argument("isos", nargs="*", help="ISO recordings, one per camera")
    ap.add_argument("-o", "--outdir", required=True, help="output folder (created if missing)")
    ap.add_argument("--name", help="base name for files and timelines (default: PGM file stem)")
    ap.add_argument("--sync", default="auto", choices=["auto", "timecode", "audio"])
    ap.add_argument("--offset", action="append", default=[], metavar="ISO_STEM=+N",
                    help="manual offset in PGM frames (repeatable)")
    ap.add_argument("--min-shot", type=int, default=match.DEFAULT_MIN_SHOT)
    ap.add_argument("--switch-penalty", type=float, default=match.DEFAULT_SWITCH_PENALTY)
    ap.add_argument("--pgm-floor", type=float, default=match.DEFAULT_PGM_FLOOR)
    ap.add_argument("--refine", action="store_true", help="search +-3 frames per ISO for a better offset")
    ap.add_argument("--hwaccel")
    ap.add_argument("--audio-window", type=float, default=1200.0, help="seconds of audio used for sync")
    ap.add_argument("--allow-rate-mismatch", action="store_true")
    ap.add_argument("--audio", default="pgm", choices=["pgm", "follow"],
                    help="pgm: audio from the PGM angle (default); follow: audio follows the video angle")
    ap.add_argument("--ffmpeg")
    ap.add_argument("--ffprobe")
    ap.add_argument("--from-json", metavar="ANALYSIS.json", help="skip analysis, regenerate from a saved analysis")
    ap.add_argument("--no-dissolves", action="store_true", help="do not detect dissolves / dips")
    ap.add_argument("--no-stills", action="store_true", help="do not extract repeating stills")
    ap.add_argument("--stills-min-repeat", type=int, default=2, metavar="N",
                    help="occurrences needed before a static image becomes a still (default 2)")
    ap.add_argument("--no-supersource", action="store_true", help="do not rebuild SuperSource composites")
    ap.add_argument("--thumb", default="64x36", help=argparse.SUPPRESS)
    return ap


def _err(msg):
    sys.stderr.write("ERROR %s\n" % msg)
    sys.stderr.flush()
    return 1


def _warn(msg):
    sys.stderr.write("WARN %s\n" % msg)


def _progress(stage, pct):
    sys.stderr.write("PROGRESS %s %d\n" % (stage, pct))
    sys.stderr.flush()


def _run_analysis(a, assets_dir):
    from .analyze import analyze
    from .sync import parse_manual_offsets
    try:
        w, h = (int(v) for v in a.thumb.lower().split("x"))
    except ValueError:
        raise EngineError("bad --thumb %r (expected WxH)" % a.thumb)
    return analyze(os.path.abspath(a.pgm), [os.path.abspath(p) for p in a.isos], ffmpeg=a.ffmpeg,
                   ffprobe=a.ffprobe, sync=a.sync, offsets=parse_manual_offsets(a.offset),
                   min_shot=a.min_shot, switch_penalty=a.switch_penalty, pgm_floor=a.pgm_floor,
                   thumb_size=(w, h), hwaccel=a.hwaccel, audio_window=a.audio_window,
                   allow_rate_mismatch=a.allow_rate_mismatch, refine=a.refine, progress=_progress,
                   dissolves=not a.no_dissolves, stills=not a.no_stills, stills_min_repeat=a.stills_min_repeat,
                   supersource=not a.no_supersource, assets_dir=assets_dir)


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if not a.from_json and (not a.pgm or not a.isos):
        ap.error("need a PGM file and at least one ISO file (or --from-json)")
    if a.name is not None and not a.name.strip():
        ap.error("--name must not be empty")
    if a.stills_min_repeat < 1:
        ap.error("--stills-min-repeat must be at least 1")
    try:
        outdir = os.path.abspath(a.outdir)
        if a.from_json:
            with open(a.from_json, encoding="utf-8") as f:
                analysis = json.load(f)
            if not isinstance(analysis, dict) or analysis.get("version") not in (1, 2):
                raise EngineError("%s is not a version 1 or 2 analysis" % a.from_json)
            stem = os.path.splitext(os.path.basename(analysis["program"]["path"]))[0]
        else:
            for p in [a.pgm] + a.isos:
                if not os.path.isfile(p):
                    raise EngineError("file not found: %s" % p)
            stem = os.path.splitext(os.path.basename(a.pgm))[0]
        name = a.name.strip() if a.name else stem
        stills_dir = os.path.join(outdir, fcpxml.safe_filename(name) + "_stills")
        os.makedirs(stills_dir, exist_ok=True)
        if not a.from_json:
            analysis = _run_analysis(a, stills_dir)
        warnings = list(analysis.get("warnings") or [])
        for w in warnings:
            _warn(w)

        _progress("write", 0)
        paths, plan = fcpxml.write_all(analysis, outdir, name, a.audio, assets_dir=stills_dir)
        _progress("write", 75)
        paths["analysis"] = os.path.join(outdir, fcpxml.safe_filename(plan.name) + "_analysis.json")
        with open(paths["analysis"], "w", encoding="utf-8") as f:
            json.dump(analysis, f, indent=2, ensure_ascii=False)
            f.write("\n")
        for w in plan.warnings:
            _warn(w)
            if w not in warnings:
                warnings.append(w)
        _progress("write", 100)
    except (EngineError, ValueError, KeyError, TypeError, OSError) as e:
        if isinstance(e, KeyError):
            return _err("analysis is missing key %s" % e)
        return _err(str(e) or e.__class__.__name__)
    except Exception as e:
        return _err("internal error: %s: %s" % (e.__class__.__name__, e))
    sys.stderr.flush()
    result = {"multicam": paths["multicam"], "conform": paths["conform"], "stacked": paths["stacked"],
              "analysis": paths["analysis"], "warnings": warnings, "segments": len(plan.segments),
              "stills_dir": stills_dir}
    result.update(fcpxml.summary(plan))
    sys.stdout.write("RESULT %s\n" % json.dumps(result))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
