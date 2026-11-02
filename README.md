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
    Timeline.
- Python 3.8 or newer. On macOS the [python.org](https://www.python.org/downloads/macos/) installer is
  recommended, because Resolve's scripting also uses it. `brew install python` works for the engine.
- ffmpeg and ffprobe. On macOS: `brew install ffmpeg`.

## Install

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
   - **Import**: which timelines to import.
   - **Output folder**: defaults to a `pgm2multicam` folder next to the PGM file.
4. Click **Analyze & Build**. The timelines land in a Media Pool bin called "PGM to Multicam", and the
   Multicam Cut timeline opens.

Without the dialog (free Resolve 19.1+):
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

- **"engine is not installed"**: run the installer again and restart Resolve.
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
