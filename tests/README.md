# Engine tests

```
~/.local/bin/uv venv .venv && ~/.local/bin/uv pip install --python .venv numpy pytest imageio-ffmpeg
.venv/bin/python -m pytest tests -q
```

ffmpeg comes from `PGM2MC_FFMPEG`, or else from imageio-ffmpeg. That build has no ffprobe, so the end-to-end
tests run the `ffmpeg -i` stderr fallback in `probe.py`. The ffprobe JSON path is covered by hand-written
fixtures in `test_engine.py`.

The first run generates media into `tests/media/{A,B,C}` (gitignored, about 220 MB each; C is generated
separately via `generate_c`, versioned by `GEN_VERSION_C`). This takes about
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

- Scenario C (Analysis v2, 25 fps, timecode, 900 frames), described by `C_SHOTS` / `C_TRANSITIONS` in
  `gen_media.py`: cam1 -> cam2 1 s xfade dissolve, a generated graphic `gfx1` (hard cuts), cam3 -> cam1 20-frame
  dip to black (fade out 10 + fade in 10), cam1 -> gfx1 14-frame dissolve, cam2 -> cam3 1 s dissolve, a second
  graphic `gfx2` shown once, then gfx1 a third time. The eq is applied to the cameras only, so the extracted
  still can be compared with `gfx1.png`. `truth.json` lists shots (nominal edit-to-edit ranges plus the
  rendered ranges) and transitions (`start`, `duration`, `centre` = start + duration / 2, xfade convention:
  mix weight 1 at `start`, 0 at `start + duration`).
- `SCENARIOS` still holds only A and B (the CLI / FCPXML tests iterate over it); C is in `SCENARIOS_V2`.

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
- Scenario C (`assets_dir` given -> version 2; SuperSource replaced by a recording stub so the test does not
  depend on that detector): every transition found with the right kind, edit within ±2 frames of the true
  centre, duration within ±3; no `pgm` segment inside a transition; transitions never overlap and fit inside
  both neighbours; `gfx1` extracted once as a 1280x720 PNG with mean abs error < 6/255 against the source
  image, its 3 occurrences labelled `still`; `gfx2` stays `pgm`; camera labels >= 98% outside transitions;
  stage order stills -> supersource -> dissolves; SS candidates = pgm segments >= 25 frames. Without
  `assets_dir` the result is version 1 (no stills) but camera-to-camera dissolves are still found.
- A and B in v2 mode: `kind` on every segment, no transitions, no stills, cuts unchanged.
- `test_dissolve.py` / `test_stills.py`: synthetic-thumbnail unit tests (dissolve, dip to black / grey, dissolve
  hiding a pgm segment, camera -> still dissolve, back-to-back dissolves, hard cut and unrelated short pgm left
  alone; static runs vs noise / slow fades / flat frames, clustering, relabel, thumb store, SuperSource hand-off
  with still-id renaming).
- Unit tests cover: timecode maths (including drop-frame), the probe parsers, the sign of the audio lag, low
  confidence on unrelated audio, Viterbi min-shot / pgm-floor behaviour, and the fps-mismatch error.

Current result: 100% frame accuracy on A and B. Scenario C: transitions at 101/25 (truth 100.5/25),
340/20 dip (340/20), 450/14 (450/14), 600/24 (600.5/25); camera labels 100% outside transitions; still PNG
mean abs error 1.4/255.

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

## Analysis v2 notes (stills, dissolves)

- Version: `analyze(..., assets_dir=DIR)` returns version 2 (stills PNGs, SuperSource, extra PROGRESS stages
  `stills`, `supersource`, `dissolves`). With `assets_dir=None` (what `cli.py` passes today) the result keeps
  `"version": 1` and the v1 PROGRESS stages so the v1 FCPXML writer still accepts it; dissolves still run (cuts
  move to the ramp centre, `transitions` is filled) but no stills / SuperSource. `kind` is always present.
- Thumb store: the scoring pass appends 32x18 grey thumbnails of the PGM and of each raw ISO stream (from
  `offset - refine radius`, so refined offsets index into it) plus the PGM at the scoring size (64x36) to raw
  files in a temp dir, read back as memmaps and deleted at the end. Disk: about 2.9 KB + 0.6 KB per ISO per frame
  (1 h at 29.97 with 4 ISOs ~ 560 MB).
- Stills (`stills.py`): a static run = consecutive 32x18 frames with mean abs diff < 1.0 grey level and < 2.5
  to the run's first frame, >= 12 frames, inside `pgm` segments only; runs with spatial std < 4 (solid colour,
  dip middles) are ignored. Clustering: 32x18 template NCC >= 0.97, 64x36 NCC >= 0.95, mean abs diff < 8.
  Clusters with >= `stills_min_repeat` runs become `s1, s2...`: PNG = median of 5 full-res frames from the
  middle of the longest run (ffmpeg seek + rawvideo rgb24, PNG written by ffmpeg). Pgm leftovers < 3 frames next
  to a still are absorbed. A locked-off camera that is not supplied as an ISO and repeats could be taken for a
  still.
- Dissolves (`dissolve.py`): candidates are every boundary between two known sources (camera or still) and
  every `pgm` segment <= 60 frames between two different known sources; window = ±30 frames. Per frame,
  least squares PGM ~ a*A + b*B + d on 32x18; mix weights wA = a/gA, wB = b/gB (gA = median per-frame gain of
  the camera into the PGM, 1 for still templates). Mixed frames (not wA > 0.92 & wB < 0.08 or vice versa)
  must form one run bordered by pure A and pure B frames, every frame R^2 >= 0.85 (or flat, std < 2.5).
  wA + wB < 0.5 or a flat frame -> dip: the line fits of wA and wB give where the ramp leaves A and reaches B;
  colour = d at the bottom (black below grey 24). Otherwise dissolve: progress u = (wA + 1 - wB)/2 must fall
  monotonically (90% of steps) and the 2-source residual must be < 0.5x the best single-source residual; the
  line fit of u gives start/end. edit = rounded centre, duration = end - start, clipped so transitions never
  overlap or exceed a neighbour. A pgm segment is only removed if the transition covers it (±1 frame).
- Limits: no template for plain `pgm` segments (an unknown source or a single non-repeating graphic), so
  dissolves into those stay hard cuts with a short pgm piece. Non-linear wipes / DVE transitions are not
  modelled. Two near-identical camera angles make the 2-source fit ill-conditioned (the boundary is skipped).
