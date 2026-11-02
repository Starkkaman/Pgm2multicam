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
