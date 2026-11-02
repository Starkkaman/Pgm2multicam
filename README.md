# pgm2multicam

Turn a live-cut **program** recording (PGM) and the **ISO** recordings of each camera into a DaVinci Resolve
**multicam clip with the live cut already applied**. You can then re-cut the show with the real ISO media
instead of starting from scratch.

It writes three timelines as FCPXML and imports them into Resolve:

- **Multicam Cut**: a multicam clip (one angle per ISO, plus the PGM as the last angle, named "PGM") and a
  timeline whose angle switches follow the live cut.
- **Conform**: the same cut built from plain clips (ISO video, PGM audio). This works even if Resolve imports
  the multicam as a compound clip.
- **Stacked**: every file synced on its own track. Use it as a fallback for Resolve's own "Convert to Multicam".

## Requirements

- **DaVinci Resolve Studio** to get the dialog.
- **Free Resolve**:
  - 19.1 to 21.0: the script runs without a dialog. It uses your Media Pool selection with default settings
    and prints progress to the Console (Workspace > Console).
  - 21.1 and later: the free version no longer runs Python scripts. Use the CLI, then File > Import >
    Timeline. The self-contained macOS install uses a Lua script instead. Whether free 21.1 runs it has not
    been checked yet.
- The self-contained macOS install needs nothing else. The source install and the CLI need:
  - Python 3.8 or newer. On macOS the [python.org](https://www.python.org/downloads/macos/) installer is
    recommended, because Resolve's Python scripting also uses it. `brew install python` works for the engine.
  - ffmpeg and ffprobe. On macOS: `brew install ffmpeg`.

## Self-contained macOS install

Use this if you don't want to install Python or ffmpeg. The release zip
(`PGM-to-Multicam-macos-<arch>-<version>.zip`, where arch is `arm64` for Apple silicon or `x86_64` for Intel)
contains everything. Current builds are `arm64` only and need macOS 14 Sonoma or newer.

1. Unzip it and open the `PGM to Multicam` folder.
2. Double-click **Install.command**. If macOS refuses to open it, right-click it > **Open** (macOS 14 and
   earlier), or use **Open Anyway** in System Settings > Privacy & Security (macOS 15 and later).
3. Restart Resolve, then use **Workspace > Scripts > PGM to Multicam**.

The installer:
- copies the engine (a self-contained `pgm2mc` bundle with its own ffmpeg and ffprobe) to
  `~/Library/Application Support/pgm2multicam/engine/pgm2mc/`, and checks that it starts;
- writes `config.json` next to it, pointing at the engine;
- puts `PGM to Multicam.lua` in `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/`.
  Resolve runs Lua scripts itself, so no Python is needed;
- removes the download quarantine flag from the files it installed;
- renames the script from an earlier Python install to `PGM to Multicam.py.disabled`, so the menu shows the
  script only once.

To update, run the new Install.command. **Uninstall.command** removes only what Install.command added.

The Lua script works like the Python one: same dialog, same no-dialog mode, same imports. The engine runs in
the background, so Resolve stays responsive. Its output goes to `NAME_engine_stdout.log` and
`NAME_engine_stderr.log` in the output folder.

To build the zip from an engine bundle built on a Mac: `packaging/macos/make_release.sh dist/pgm2mc [VERSION]`.

## Install from source (Python engine, alternative)

This route also gives you the command line (see below).

macOS:

```sh
bash install/install_mac.sh            # re-run to update; --uninstall to remove
```

The installer:
- creates a virtual environment with numpy in `~/Library/Application Support/pgm2multicam/`;
- copies the engine there and records the ffmpeg/ffprobe paths in `config.json`;
- puts the script in `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/`.

Restart Resolve afterwards.

Windows (**untested**): `powershell -ExecutionPolicy Bypass -File install\install_windows.ps1`. It needs
ffmpeg on PATH, for example from `winget install Gyan.FFmpeg`.

## Use in Resolve

1. Import the PGM and the ISO files into the Media Pool. Select the program clip and the ISO clips.
   - On Resolve versions older than 19.0.2 the script cannot read the selection. It offers every clip in the
     current bin instead.
2. **Workspace > Scripts > PGM to Multicam**.
3. Check the dialog settings:
   - **Program clip**: preselected when the name contains PGM, Program, Prog or "PVW out".
   - **ISO clips**: the clips to use as angles.
   - **Sync**, **Audio in cut** and **Min shot**: matching options (see Tips).
   - **Rebuild**: **Dissolves & dips**, **Repeating stills** (with **min repeats**, default 2) and
     **SuperSource**. All on by default (see "Dissolves, stills and SuperSource").
   - **Import**: which timelines to import.
   - **Output folder**: defaults to a `pgm2multicam` folder next to the PGM file.
4. Click **Analyze & Build**. The timelines land in a Media Pool bin called "PGM to Multicam", and the
   Multicam Cut timeline opens.

Without the dialog (free Resolve 19.1 to 21.0; free 21.1+ restricts scripting to Studio, so the script may
not run there at all — use the CLI and File > Import > Timeline):
- The program clip must be the only selected clip whose name matches PGM, Program, Prog or "PVW out".
- All other selected video clips become ISOs.
- The Multicam Cut and Conform timelines are imported.

## Command line

```sh
APP="$HOME/Library/Application Support/pgm2multicam"
alias pgm2mc='PYTHONPATH="$APP/engine" "$APP/venv/bin/python" -m pgm2mc'

pgm2mc PGM.mov CAM1.mov CAM2.mov CAM3.mov -o out/
pgm2mc PGM.mov CAM*.mov -o out/ --sync audio --refine --min-shot 10
pgm2mc PGM.mov CAM1.mov CAM2.mov -o out/ --offset CAM2=+120     # manual sync for one ISO, in frames
pgm2mc --from-json out/PGM_analysis.json -o out/ --audio follow  # regenerate without re-analysing
```

The output is `NAME_multicam.fcpxml`, `NAME_conform.fcpxml`, `NAME_stacked.fcpxml` and `NAME_analysis.json`,
where NAME is the PGM file name by default (`--name` changes it). Import the FCPXML files into Resolve via
**File > Import > Timeline**.

Angle names come from the ISO file names. The ISOs must have the same frame rate as the PGM.

## How it works

1. Every file is probed with ffprobe (frame rate, start timecode, duration, audio).
2. Each ISO is synced to the PGM by timecode when both carry it, otherwise by audio cross-correlation.
   `--offset` sets an ISO's offset by hand.
3. The PGM and the ISOs are decoded to small grayscale thumbnails. Every PGM frame is compared with every ISO
   at the same instant.
4. A smoothing pass (Viterbi) picks the angle for each frame. It penalises switches and enforces a minimum
   shot length. Frames that match no ISO stay on the PGM angle.
5. The resulting segments are written as FCPXML.

## Dissolves, stills and SuperSource

Besides the hard cuts, the engine looks for three more things in the PGM:

- **Dissolves and dips**: a mix from one shot to the next, or a dip through a solid colour (usually black).
- **Repeating stills**: static program images such as graphics, still-store frames or a holding slate.
  Only images that appear at least **min repeats** times (default 2) are rebuilt. Each one is saved as a PNG
  at PGM resolution in `OUTDIR/NAME_stills/`.
- **SuperSource**: a layout of scaled and cropped ISOs (boxes) over background art.

How they appear in the Multicam Cut and Conform timelines:

- A transition between two camera or PGM shots becomes a Cross Dissolve. A dip becomes a dissolve into a
  solid-colour clip and a dissolve out of it.
- A still goes on **V2**, over the PGM clip.
- A SuperSource puts its background (a still) on **V2** and each box on **V3** and up, as the ISO clip with
  position, zoom and crop.
- The PGM clip always stays on **V1** under stills and SuperSources. Disable the upper tracks to fall back to
  the program picture.
- Transitions into or out of a still or a SuperSource are not rebuilt. The PGM clip on V1 shows them, the
  upper layers start after the transition and a "PGM transition" marker sits at the edit.
- The Resolve script sets each box's Pan, Tilt, Zoom and Crop again after the import, because Resolve's
  FCPXML import is not documented to carry crops. The log reports how many boxes it set.

Flags (CLI) and their dialog equivalents:

| CLI | Dialog | Effect |
|---|---|---|
| `--no-dissolves` | Dissolves & dips off | hard cuts only |
| `--no-stills` | Repeating stills off | stills stay part of the PGM clip |
| `--stills-min-repeat N` | min repeats | how often an image must appear (default 2) |
| `--no-supersource` | SuperSource off | SuperSource layouts stay part of the PGM clip |

Limits:

- Box borders and outlines end up in the background art. Keyed graphics over the boxes are lost.
- Moving boxes (animated layouts) use the layout of their middle frame.
- One box per ISO per layout.
- Box crops are detected up to about 30% per edge.
- The background is always a still, even for a picture-in-picture over a live camera.
- Dips use a solid colour.

## Tips

- **Timecode jam-synced** across all recorders is the most reliable sync. Audio sync needs scratch audio on
  every ISO.
- Graphics, replays and media-player shots match no ISO, so they use the **PGM angle**. That is expected.
- Tuning:
  - `--min-shot N` (frames, default 6): raise it if you see flicker cuts.
  - `--switch-penalty F`: higher means fewer, longer shots.
  - `--pgm-floor F`: higher means unclear frames fall back to the PGM angle sooner.
- `--refine` searches ±3 frames per ISO. Use it when audio sync lands a frame or two off.

## Troubleshooting

- **"engine is not installed"**: run the installer again (Install.command for the self-contained install) and
  restart Resolve.
- **ffmpeg not found**: run `brew install ffmpeg`, then run the installer again (it records the paths).
- **The multicam shows up as a compound clip**:
  1. Right-click it in the Media Pool and choose **Convert Compound Clips to Multicam Clips**. This cannot be
     undone.
  2. If that does not work, use the **Conform** timeline (same cut, plain clips).
  3. Or use **Stacked**, then Resolve's Convert to Multicam. You get the multicam but not the cut.
- **Wrong or missing audio in the multicam cut**: by default the cut takes video from the ISO and audio from
  the PGM. If Resolve mishandles that, rebuild with `--audio follow`. The CLI does it without re-analysing
  (`--from-json out/NAME_analysis.json`).
- **An import failed**: the script names the file. Import it by hand via File > Import > Timeline.
  - Resolve refuses a timeline name that already exists. The script picks a free name ("PGM 2 …"), but
    imports done by hand may clash.
- **Wrong angles in places**:
  - Check the sync method and confidence in `NAME_analysis.json`.
  - Try `--sync timecode` or `--sync audio`, add `--refine`, or set `--offset` by hand.

## First-run checklist (new Resolve version)

1. Import a short test shoot. Check the items Resolve creates:
   - The Media Pool holds a **Multicam** item, not a Compound.
   - The angles are named after the ISOs plus "PGM".
   - The cut's clips show the right active angle.
   - The audio comes from the PGM.
2. Check that the timeline names are honoured and see whether the source media is duplicated in the pool.
3. If you shoot 29.97 drop-frame: check that the timecode display and alignment are right.
4. If your ISOs have multi-mono audio tracks: check which audio ends up in each angle.
5. Open the Stacked timeline and check which tracks the files land on.
6. If you got compound clips: check that Convert Compound Clips to Multicam Clips keeps the cut's angles.
7. If the shoot has a SuperSource: check each box's position and crop after the import. If the transforms look
   wrong after an import done with the CLI only (File > Import > Timeline), run the import through the Resolve
   script: it sets the box geometry again.
