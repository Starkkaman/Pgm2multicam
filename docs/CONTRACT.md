# pgm2multicam — architecture and interface contract

Goal: from a live-cut **program** recording (PGM) and the **ISO** recordings of each camera, build a DaVinci Resolve
multicam clip (one angle per ISO, plus the PGM itself as a last angle) and a timeline whose multicam angle switches
match the live cut, so the editor can re-cut with real ISO media.

## Layout

```
engine/pgm2mc/        Python 3.8+ package. Needs numpy and ffmpeg/ffprobe on PATH (or --ffmpeg/--ffprobe).
  probe.py            ffprobe wrapper -> MediaInfo (fps, duration, start timecode, size, audio)
  sync.py             ISO -> PGM offsets: timecode, audio cross-correlation, manual
  frames.py           ffmpeg rawvideo pipe -> small grayscale thumbnails (numpy)
  match.py            per-frame angle scores + Viterbi smoothing -> segments
  analyze.py          orchestrates the above -> Analysis (JSON below)
  fcpxml.py           Analysis -> FCPXML (multicam clip + cut timeline + stacked sync timeline)
  cli.py / __main__   `python -m pgm2mc PGM ISO1 ISO2 ... -o out.fcpxml [--json analysis.json]`
resolve/PGM to Multicam.py   Resolve script (Workspace > Scripts). Stdlib only. Picks clips from the media pool,
                             runs the engine in a subprocess with the venv python, imports the FCPXML.
install/              install_mac.sh (+ Windows later): venv with numpy, ffmpeg check, copy the Resolve script.
tests/                synthetic media generator + end-to-end tests (media under tests/media, gitignored).
```

## Analysis JSON (engine output, fcpxml.py input) — version 1

All frame numbers are integers at the PGM frame rate. ISOs must share the PGM nominal rate (else error, unless
`--allow-rate-mismatch`, which only warns).

```json
{
  "version": 1,
  "fps": {"num": 30000, "den": 1001},
  "drop_frame": false,
  "program": {"id": "pgm", "path": "/abs/PGM.mov", "name": "PGM", "start_tc_frames": 0,
              "duration_frames": 108000, "width": 1920, "height": 1080,
              "has_audio": true, "audio_rate": 48000, "audio_channels": 2},
  "isos": [
    {"id": "a1", "path": "/abs/CAM1.mov", "name": "CAM1", "start_tc_frames": 3600,
     "duration_frames": 110000, "width": 3840, "height": 2160,
     "has_audio": true, "audio_rate": 48000, "audio_channels": 2,
     "offset_frames": 1200,
     "sync_method": "timecode", "sync_confidence": 1.0}
  ],
  "segments": [
    {"start": 0, "end": 240, "angle": "a1", "score": 0.93},
    {"start": 240, "end": 600, "angle": "pgm", "score": 0.20}
  ],
  "warnings": []
}
```

- `offset_frames`: PGM frame `p` shows the same instant as ISO-local frame `p + offset_frames` (ISO-local = frames
  from the start of the ISO file, 0-based). Can be negative (ISO started after PGM).
- `start_tc_frames`: the file's start timecode as a frame count (0 when there is none).
- `segments`: contiguous, sorted, cover `[0, program.duration_frames)` exactly. `angle` is an ISO id or `"pgm"`
  (no ISO matches: graphics, media player, an ISO not supplied, or the ISO has no media at that instant).
- `score`: mean match score of the chosen angle over the segment (0..1, higher = better).

## Matching method (engine)

1. Probe all files. 2. Sync each ISO to PGM: `timecode` when PGM and ISO both carry start TC and the ranges overlap,
else `audio` (FFT cross-correlation of mono 8 kHz envelopes/samples, coarse then fine), else `manual` offsets
(`--offset CAM1=+120`). `--sync auto|timecode|audio`. 3. Decode PGM and each ISO to small grayscale thumbnails
(default 64x36, area-scaled, letterbox-safe) aligned by offset. 4. Score per frame = normalised cross-correlation
between PGM and each ISO thumbnail, mapped to 0..1. Frames where the ISO has no media score 0. 5. Viterbi over
states {ISOs, pgm} with a switch penalty and a floor score for `pgm`; enforce `--min-shot` frames (default 6).
6. Optional `--refine` searches ±3 frames per ISO for a better offset using the frames where that ISO is live.

## CLI contract (cli.py) — fixed, the Resolve script depends on it

```
python -m pgm2mc PGM ISO [ISO ...] -o OUTDIR [--name NAME]
    [--sync auto|timecode|audio] [--offset ISO_STEM=+N ...] [--min-shot 6] [--switch-penalty F] [--pgm-floor F]
    [--refine] [--hwaccel X] [--audio-window SEC] [--allow-rate-mismatch]
    [--audio pgm|follow] [--ffmpeg PATH] [--ffprobe PATH] [--from-json ANALYSIS.json]
```
- Angle names = ISO file stems; the PGM angle is always added last, named "PGM".
- Writes into OUTDIR (NAME defaults to the PGM stem):
  `NAME_multicam.fcpxml` (multicam clip + "NAME Multicam Cut" timeline of mc-clips),
  `NAME_conform.fcpxml` ("NAME Conform": plain asset-clips cut from the ISOs/PGM on the primary storyline, PGM audio
  as a connected clip — works even if Resolve imports multicams as compound clips),
  `NAME_stacked.fcpxml` ("NAME Stacked": every file synced on its own lane, for Convert-to-Multicam fallback),
  `NAME_analysis.json`.
- `--audio pgm` (default): mc-clips take video from the ISO angle and audio from the PGM angle (split mc-source);
  `--audio follow`: single `srcEnable="all"` mc-source.
- `--from-json` skips analysis and only regenerates the FCPXML files from a saved analysis.
- stderr: `PROGRESS <stage> <0-100>` lines (stages probe, sync, frames, match, write), `WARN <text>`, and on failure
  `ERROR <text>` + exit code 1 (2 for usage errors).
- stdout, last line on success: `RESULT {"multicam": ..., "conform": ..., "stacked": ..., "analysis": ...,
  "warnings": [...], "segments": N}` (absolute paths, JSON).

## v2 additions (2026-11-02): dissolves, stills, SuperSource

Analysis `"version": 2` (fcpxml.py must still accept v1 = no fields below). New/changed fields:

```json
"segments": [{"start": 0, "end": 240, "angle": "a1", "score": 0.93, "kind": "camera"},
             {"start": 240, "end": 300, "angle": "pgm", "score": 0.9, "kind": "still", "still": "s1"},
             {"start": 300, "end": 900, "angle": "pgm", "score": 0.8, "kind": "supersource", "supersource": "ss1"},
             {"start": 900, "end": 960, "angle": "pgm", "score": 0.2, "kind": "pgm"}],
"transitions": [{"edit": 240, "duration": 24, "kind": "dissolve", "confidence": 0.9},
                {"edit": 900, "duration": 30, "kind": "dip", "color": [0, 0, 0], "confidence": 0.8}],
"stills": [{"id": "s1", "path": "/abs/OUTDIR/NAME_stills/s1.png", "width": 1920, "height": 1080,
            "occurrences": [[240, 300], [5000, 5100]], "role": "still"}],
"supersources": [{"id": "ss1", "background": "s2",
                  "boxes": [{"angle": "a1", "layer": 1, "rect": [0.05, 0.25, 0.48, 0.75], "crop": [0.1, 0.0, 0.1, 0.0]}],
                  "occurrences": [[300, 900]], "animated": false, "confidence": 0.85}]
```
- `kind`: camera (angle = ISO id) | still | supersource | pgm (angle = "pgm" for the last three).
- `transitions`: `edit` equals a segment boundary; the transition is centred on it (`duration` frames, split evenly).
  `dissolve` = A→B mix; `dip` = through a solid colour (usually black). Works between any two segment kinds.
- `stills`: static program images (graphics / still store / SS background art) written as PNG at PGM resolution
  (median of a few full-res frames). `role`: "still" or "background" (SS art fill). Only stills that occur at least
  `--stills-min-repeat` times (default 2) become `kind: still` segments; SS backgrounds always kept.
- `supersources`: a composite of scaled/cropped ISOs over a background. `rect` = visible box in PGM frame fractions
  [x0, y0, x1, y1] (origin top-left); `crop` = fraction trimmed from the ISO frame [left, top, right, bottom];
  `layer` 1 = bottom box. Identical layouts across occurrences share one entry.
- Timelines (Multicam Cut and Conform): the spine keeps the PGM-angle clip under still / supersource segments (safe
  fallback); a still goes on lane 1 above it; a supersource puts its background still on lane 1 and each box as an
  ISO clip with transform + crop on lanes 2..n. Transitions become FCPXML cross-dissolve / dip transitions.
- CLI flags: `--no-dissolves`, `--no-stills`, `--stills-min-repeat N`, `--no-supersource`; assets go to
  `OUTDIR/NAME_stills/`. RESULT gains `"stills_dir"`, `"transitions": N`, `"supersources": N`, `"stills": N`.

Engine module boundaries:
- `thumbs` store: during the first decode pass, also keep 32x18 gray thumbnails of PGM and every aligned ISO in a
  disk-backed numpy memmap (temp dir), for dissolve/still analysis without a second full decode.
- `stills.py`: static-run detection on non-camera segments, clustering, full-res extraction.
- `dissolve.py`: at each boundary, fit PGM ≈ α·A + (1−α)·B (A/B = ISO, still template, or solid colour) over a
  window (max 60 frames); a monotonic α ramp → transition; removes spurious short `pgm` segments inside dissolves.
- `supersource.py` — exactly this entry point (analyze.py calls it; skips if it raises ImportError):
  `detect_supersources(ffmpeg, analysis, candidates, assets_dir, min_len=25, progress=None) -> (supersources, stills)`
  where `analysis` is the v2 dict built so far (program + isos with offsets), `candidates` = list of
  `[start, end]` pgm-segment ranges; returns new supersource entries (with `occurrences`) and the background still
  entries it wrote (role "background"). analyze.py then relabels the matching segments.

### Writer decisions (orchestrator, after RESEARCH2)
- Transitions between two camera/pgm segments → real FCPXML Cross Dissolve on the spine (uid per RESEARCH2).
  A dip → dissolve, solid-colour slug (generated PNG), dissolve, as RESEARCH2 recommends.
- A transition that touches a `still` or `supersource` segment is NOT rebuilt: the PGM clip shows it. The spine cut
  moves to the transition's start (incoming) or end (outgoing) so the PGM-angle clip covers the whole transition, no
  `<transition>` element is written, and the lane layers (still / background / boxes) start after the incoming
  transition ends and stop before the outgoing one starts. Add a marker "PGM transition" at the edit.
- FCPXML gets adjust-transform (+ adjust-crop with mode="trim") per RESEARCH2 §7; the Resolve script re-applies the
  box geometry after import with TimelineItem.SetProperty (Pan/Tilt/ZoomX/ZoomY/Crop*), since Resolve's FCPXML import
  is not documented to carry crop. The CLI-only path relies on the FCPXML values.
