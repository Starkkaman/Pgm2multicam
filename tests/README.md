# Engine tests

```
~/.local/bin/uv venv .venv && ~/.local/bin/uv pip install --python .venv numpy pytest imageio-ffmpeg
.venv/bin/python -m pytest tests -q
```

ffmpeg comes from `PGM2MC_FFMPEG`, or else from imageio-ffmpeg. That build has no ffprobe, so the end-to-end
tests run the `ffmpeg -i` stderr fallback in `probe.py`. The ffprobe JSON path is covered by hand-written
fixtures in `test_engine.py`.

The first run generates media into `tests/media/{A,B}` (gitignored, about 220 MB each). This takes about
30 s on 24 cores. Later runs reuse the media until `GEN_VERSION` in `gen_media.py` changes; to regenerate it,
run `python tests/gen_media.py --force`. With media cached the suite runs in about 10 s.

## Synthetic shoot (`gen_media.py`)

- Every camera source is rendered once over a 46 s "world" timeline (lavfi, deterministic): cam1 = testsrc2,
  cam2 = mandelbrot, cam3 = life, ext = cellauto (the 4th source is never given to the engine).
- ISOs are 1920x1080 h264 + PCM. cam1 records world 0–45 s (it starts before the PGM). cam2 records
  3.3–46 s (it starts after the PGM, so its offset is negative). cam3 records 1.0–40.5 s (it ends before the
  PGM).
- PGM is 1280x720 and covers world 2–42 s. It has eq applied (brightness, contrast, saturation) and follows
  the ground-truth cut list `CUTS`, which includes an 8-frame cam3 shot and a 2 s `ext` segment.
- Audio is one seeded speech-like master track (band-limited noise with syllable gating). Each file carries
  its own slice of it, with its own gain plus independent noise.
- Scenario A: 25 fps, and every file has a start timecode (`-timecode`), so sync uses timecode. Scenario B:
  30000/1001 with no timecode, so sync uses audio.
- `truth.json` holds the expected offsets, frame counts, timecodes and segments.

## What is checked

- Offsets are exact, and each ISO uses the expected sync method.
- There are as many cuts as in the truth, each within ±1 frame.
- At least 98% of frames carry the right label.
- The `ext` segment comes out as `pgm`.
- Every ISO segment scores above 0.99. This catches off-by-one alignment, because adjacent frames score
  about 0.95.
- Manual offsets combined with `refine=True` repair a deliberate +2 frame error.
- Seeking in `ThumbReader` is frame-exact, and negative starts pad with "no media" frames.
- `_analyze_cli` prints well-formed `PROGRESS` lines and writes the JSON.
- Unit tests cover: timecode maths (including drop-frame), the probe parsers, the sign of the audio lag, low
  confidence on unrelated audio, Viterbi min-shot / pgm-floor behaviour, and the fps-mismatch error.

Current result: 100% frame accuracy on A and B.

## Engine notes and known limits

- Score: `max(0, NCC)` of mean/std-normalised 64x36 grey thumbnails. Segment `score` for `pgm` is
  `1 - mean(best ISO score)`, so a higher value still means a more confident result.
- Defaults: `pgm_floor=0.5`, `switch_penalty=1.5`, `min_shot=6`. Only the synthetic media has been used to
  tune them. Real shows with graphics over the ISO (lower thirds, DVE, keys) lower the NCC. If the PGM
  carries a lot of graphics, `pgm_floor` may need to go down to about 0.35–0.4.
- Thumbnails are forced to WxH without padding. A PGM that letterboxes or pillarboxes an ISO (for example a
  4:3 camera in a 16:9 programme) is not compensated and will score poorly.
- Audio sync uses the first `audio_window` seconds of the PGM (default 1200) and 2x that of each ISO. The
  coarse step is a 500 Hz envelope correlation; the fine step is a raw 8 kHz correlation over ±50 ms. If the
  ISO and PGM do not overlap inside those windows, sync fails (low confidence warning). Use timecode or
  `--offset` in that case.
- Audio is assumed to start together with the first video frame. Containers with audio priming or edit
  lists that ffmpeg does not honour can be off by a frame; `--refine` (±3 frames, judged on the frames where
  the ISO is live) corrects that.
- Seeking uses input `-ss (q-0.5)/fps` plus `setpts=PTS-STARTPTS`. Without setpts the cfr output duplicated
  the first frame. ISO-local frame 0 is the first decoded video frame. VFR sources are converted to CFR at
  the probed rate by ffmpeg.
- With the ffmpeg-stderr fallback, `duration_frames` comes from the container Duration, rounded to 10 ms.
  The PGM duration is always replaced by the number of frames actually decoded.
