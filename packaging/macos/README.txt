PGM to Multicam for DaVinci Resolve (macOS)
============================================

Turns a live-cut program recording (PGM) and the camera ISO recordings into a Resolve multicam clip with the
live cut already applied. Nothing else needs to be installed: the engine and ffmpeg are included.

Install
-------
1. Unzip the download and open the "PGM to Multicam" folder.
2. Double-click Install.command. A Terminal window shows the progress.
   If macOS refuses to open it ("unidentified developer" or "cannot be verified"):
   - macOS 14 and earlier: right-click Install.command > Open, then click Open.
   - macOS 15 and later: click Done, open System Settings > Privacy & Security, scroll down, click
     "Open Anyway" next to Install.command, and confirm.
3. Restart DaVinci Resolve.

Use
---
1. Import the PGM and the ISO files into the Media Pool and select them.
   The program clip's name should contain PGM, Program, Prog or "PVW out".
2. Workspace > Scripts > PGM to Multicam.
3. Resolve Studio shows a dialog: check the settings and click Analyze & Build.
   Free Resolve 19.1 and later runs without a dialog, using default settings; progress appears in
   Workspace > Console.
The timelines land in a Media Pool bin called "PGM to Multicam". The analysis files are written to a
"pgm2multicam" folder next to the PGM file.

Update: run Install.command from the new download.
Remove: double-click Uninstall.command (your output folders are kept).

Installed files
---------------
~/Library/Application Support/pgm2multicam/  (engine and config.json)
~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/PGM to Multicam.lua
