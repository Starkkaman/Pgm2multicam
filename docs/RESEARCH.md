# pgm2multicam — research: FCPXML multicam import into DaVinci Resolve + Resolve scripting

Researched 2026-11-02. Nothing here was test-imported: this container has no Resolve and no ffmpeg.
Each answer carries a confidence label:

- **verified**: confirmed in a primary source (Blackmagic release notes, manual or README, Apple DTD or docs, a Blackmagic staff forum post), or checked here by running something (DTD validation).
- **documented**: stated in secondary documentation or in measurements other people published.
- **inferred**: my reasoning from indirect evidence. Test it on a real Resolve before relying on it.

---

## 0. TL;DR recipe

1. **Emit FCPXML 1.10.** Resolve has imported 1.10 since 18.0, so this covers 18.5, 19, 20 and 21. It is also the
   newest version Resolve itself exports. 1.10 requires `<media-rep>` inside `<asset>`; the old `src`
   attribute on `<asset>` is invalid from 1.9 onwards.
2. **Resources:**
   - One `<format>` per distinct (frameDuration, width, height).
   - One `<asset>` per file, with `start` = the file's start timecode as time, and
     `<media-rep kind="original-media" src="file:///...">`.
   - One `<media>` that holds a `<multicam tcStart=… format=…>` with one `<mc-angle angleID>` per ISO plus a
     last angle for the PGM. Each angle holds `[<gap>] <asset-clip>`; the gap pushes a later-starting file to
     its synced position.
3. **Cut project:** a `<sequence>` whose `<spine>` holds contiguous `<mc-clip ref=media offset start duration>`.
   Each one carries `<mc-source angleID=X srcEnable="video"/>` plus `<mc-source angleID=pgm srcEnable="audio"/>`.
   Make the split configurable, with a fallback to a single `srcEnable="all"`.
4. **Stacked project:** a spine `<gap>` that spans the multicam range, with the files as connected
   `<asset-clip lane="1..n">`.
5. **All times are integer frames × frameDuration**, written as rationals (`262/25s`, `1001/30000s`
   multiples). Never use floats.
6. **Resolve script:** pass the absolute `.fcpxml` path to `MediaPool.ImportTimelineFromFile(path, {...})`.
   It returns a Timeline or `None`.
7. **Biggest open risk:** whether Resolve turns `<multicam>`/`<mc-clip>` into a **real multicam clip** or into a
   **compound clip**. The evidence conflicts (§2.4). This has to be spiked on a real Resolve first.
8. Other limits to plan for:
   - No public API before 21.1 creates multicam clips.
   - UIManager (script dialogs) is **Studio-only since 19.1**.
   - Python scripting as a whole is **Studio-only since 21.1**.

`docs/sample_multicam.fcpxml` is a complete, DTD-valid example (see §12).

---

## 1. FCPXML versions Resolve imports (Q1)

| Resolve | FCPXML import added (release-note wording) | Source |
|---|---|---|
| 12.0 | "Support for FCPX XML 1.5 DTD roundtrip", "Support for multicam and audio layout for FCPX XML roundtrip" | https://www.blackmagicdesign.com/support/readme/5e3b5057ee464cfdaf2f50011245aa3a |
| 16.2.6 | "Support for Final Cut Pro X XML Version 1.9" | https://www.blackmagicdesign.com/support/readme/b1101ea04e3e48c896268cbee0a0d1f4 |
| 18.0 | "Support for importing and exporting Final Cut Pro v1.10 XMLs" | https://www.blackmagicdesign.com/support/readme/42fbd7d69ded40109a21d738cf99f648 |
| 18.1 | "Ability to select specific timelines to import from FCPXML files" | https://www.blackmagicdesign.com/support/readme/ce671c375b4d48e2bf9dc5fb422aa9c9 |
| 18.6 | "Support for importing and exporting Final Cut Pro v1.11 XMLs"; "Import multiple XMLs, AAFs, FCP XMLs or EDLs as a single action" | https://www.blackmagicdesign.com/support/readme/d58fbf1e253f4b1285c8fde17387440e |
| 19.1.1 | "Ability to import and export Final Cut Pro v1.13 XMLs" (1.12 is not named in any release note) | https://www.blackmagicdesign.com/support/readme/d9ff14c92db1442083b716f09bc98158 |
| 21.0 | "Support for importing and exporting Final Cut Pro 12 FCPXML" | https://www.blackmagicdesign.com/support/readme/6aefbffe870f4ba686e61ecb83b19ec0 |

The scripting README for 21.0.4 lists these export constants:
- `resolve.EXPORT_FCPXML_1_8`, `_1_9`, `_1_10`;
- `EXPORT_FCPXML_1_3` … `_1_7` as subtypes.

(https://gist.github.com/X-Raym/2f2bf453fc481b9cca624d7ca0e19de8, README "Last Updated: 24 Jul 2026".)

**Pick: FCPXML 1.10.** It is the highest version that **Resolve 18.5** imports, every later release imports it, and it
is Resolve's own export ceiling, so its import path gets exercised by round trips. Confidence: **verified**
(release notes). How an older Resolve reacts to a newer version (for example 18.5 opening 1.11) is **unknown**:
no source found.

The same release notes say the `File > Import > Timeline` dialog and `ImportTimelineFromFile` share one importer.
The README lists FCPXML as a supported input of `ImportTimelineFromFile` (**verified**). That both paths behave
identically is **inferred**.

## 2. Multicam structure (Q2)

### 2.1 Elements (FCPXML 1.10 DTD and Apple docs)

DTD source: Apple's DTDs mirrored at https://github.com/CommandPost/CommandPost/tree/develop/src/extensions/cp/apple/fcpxml/dtd.
Apple docs: https://developer.apple.com/documentation/professional-video-applications/multicam-media and
https://developer.apple.com/documentation/professional-video-applications/audio-components-and-multicam.
Confidence: **verified**.

```
<!ELEMENT media (multicam | sequence)?>            id ID #REQUIRED, name
<!ELEMENT multicam (mc-angle*, metadata?)>         format IDREF #REQUIRED, duration, tcStart, tcFormat (DF|NDF), renderFormat
<!ELEMENT mc-angle ((%clip_item; | transition)*)>  name, angleID CDATA #REQUIRED
<!ELEMENT mc-clip (note?, conform-rate?, timeMap?, adjust-volume?, adjust-panner?, mc-source*, anchored items*, markers*, filter-audio*, metadata?)>
                                                   ref IDREF #REQUIRED (the media id), lane, offset, name, start, duration #REQUIRED,
                                                   enabled, srcEnable (all|audio|video), audioStart, audioDuration, modDate
<!ELEMENT mc-source (audio-role-source*, intrinsic video params, video filters*)>
                                                   angleID #REQUIRED, srcEnable (all|audio|video|none) default "all"
```

- `<media>` lives in `<resources>`. `<mc-clip>` lives in a `<spine>`, or is anchored with `lane`.
- `mc-angle` is a container "ordered serially in time", like a spine. Its children are laid out back to back.
- Since 1.2, the active angles are chosen with `mc-source` children. 1.1 used `videoAngleID`/`audioAngleID`
  instead (Apple).

### 2.2 Timing inside a multicam

These rules are **verified** for the DTD and Apple's semantics. Applying them to the absolute tcStart-based offsets
is **inferred**: it matches FCP and Resolve exports, which put `offset` = `tcStart` on the first spine item.

- `multicam@tcStart` is the multicam's own timecode origin. Children of each `mc-angle` sit at
  `offset` = multicam time, so the first child's `offset` equals `tcStart`.
- `asset-clip@start` is the **source** time in the asset's own timeline. The asset's timeline begins at
  `asset@start`, the file's start TC. To use the whole file, set `asset-clip@start = asset@start` and
  `duration = asset@duration`.
- **ISO that starts later than the multicam origin:** put a `<gap offset=tcStart start=tcStart duration=lead/>`
  first, then the asset-clip at `offset = tcStart + lead`. This is what the sample does, and the validator
  checked angle contiguity.
- **ISO that starts earlier than the others:** it defines the origin. Choose `multicam@tcStart` = the earliest
  angle start, so no angle ever needs a negative position. Trimming the head of the earliest file (by raising
  `asset-clip@start`) is also valid, but loses media.
- `mc-clip@start` is a time in the **multicam's** timeline: `tcStart` plus a position. `mc-clip@offset` is a time
  in the **parent sequence**: `sequence@tcStart` plus a position.

### 2.3 Separate video and audio angles (srcEnable split)

FCPXML expresses this as two `mc-source` elements on one `mc-clip`, one with `srcEnable="video"` and one with
`srcEnable="audio"`. Apple's example uses exactly this (**verified**).

**Whether Resolve honours it on import is unknown.** I found no source either way. The forum could not be
searched because it sits behind bot protection (see §6). Indirect evidence:
- Resolve's own multicam editing has "Audio & Video / Video only / Audio only" switch modes, so its data model
  can hold different audio and video angles per timeline clip (**inferred**).
- Resolve 12's note "multicam and audio layout for FCPX XML roundtrip" points the same way (**inferred**).

Recommendation:
- Emit the split by default, but keep an engine option such as `--audio-angle pgm|follow`. With `follow`, write a
  single `<mc-source angleID=X srcEnable="all"/>`.
- Test both on a real Resolve (§13).
- If the split is ignored, the common Resolve workaround is to put the PGM audio on a separate track in the Cut
  timeline: a connected `<asset-clip ref=PGM lane="-1" srcEnable="audio">` that spans the whole cut, with the
  mc-clips carrying `srcEnable="video"`. That structure is DTD-valid.

### 2.4 Does Resolve create a real multicam clip from `<multicam>`? (critical)

The evidence conflicts:

| Evidence | Says | Source | Confidence |
|---|---|---|---|
| Resolve 20 Reference Manual, ch. 55, "About Nested Sequences and Compound Clips" (PDF p. 1137) | "Compound clips with mixed frame rates are supported, as well as multi-cam and A/V synchronized clips from Final Cut Pro X, which are represented in DaVinci Resolve as **compound clips**." | https://documents.blackmagicdesign.com/UserManuals/DaVinci_Resolve_20_Reference_Manual.pdf | verified text; it may be stale (same wording as the Resolve 11 era) |
| Resolve 11.1 release notes | "Improved support for FCP X exported compound clips including multi-cam and sync clips" | https://www.blackmagicdesign.com/support/readme/a3270e16f79344238dcb0b234e390340 | verified |
| Resolve 12.0 release notes (the version that added native multicam editing) | "Support for multicam and audio layout for FCPX XML roundtrip" | readme/5e3b5057ee464cfdaf2f50011245aa3a | verified |
| Resolve 12.5.3 release notes | "Addressed an issue with FCPXML transforms for Multicam not getting imported correctly" | https://www.blackmagicdesign.com/support/readme/16aba80b65694ef48ff970ecff25e53c | verified |

My reading (**inferred**):
- Since Resolve 12, FCPXML multicams most likely come in as Resolve multicam clips, and the manual paragraph is a
  leftover from Resolve 11.
- Even if they arrive as compound clips, Resolve 17+ can turn them into real multicam clips in one step: Media Pool
  right-click > **"Convert Compound Clips (Timelines) to Multicam Clips"**. This is **verified**: Resolve 17
  release notes "Support for compound clip or timeline conversion into multicam clips", and Manual 20 p. 893. The
  conversion is one-way.
- Whether timeline instances of that compound clip follow the conversion, and keep their active angle, is
  **unknown**.

Fallbacks, in order:
1. Import as designed.
2. If the result is compound clips, run "Convert Compound Clips to Multicam Clips" on the imported multicam item,
   either manually or documented in the README.
3. Use the Stacked timeline, then Convert timeline to multicam. This gives a multicam without the cut.

## 3. Asset declaration (Q3)

### 3.1 `src` vs `<media-rep>`

**Verified** from the DTDs and Apple's `asset` page:

- **1.8 and earlier:** `<asset src="file://…" …>` with optional `bookmark` and `metadata` children. `src` is
  REQUIRED.
- **1.9 and later (including 1.10):** `<!ELEMENT asset (media-rep+, metadata?)>`, using
  `<media-rep kind="original-media" src="file://…"/>`.
  - A `src` attribute on `<asset>` is **invalid** in 1.9+.
  - `kind` is `original-media` (default) or `proxy-media`.

### 3.2 File URLs

- **Use an absolute `file://` URL with UTF-8 percent-encoding (RFC 8089).** Generate it from the absolute path:
  - POSIX: `pathlib.PurePosixPath(p).as_uri()` gives `file:///Users/editor/My%20Shoot/CAM%C3%A9.mov`.
  - Windows drive path: `pathlib.PureWindowsPath(p).as_uri()` gives `file:///C:/Shoot/My%20Clip.mov`.
  - UNC path: `file://server/share/...`.
  - Confidence: **inferred**. These are the standard forms, and Apple's examples use `file:///Volumes/...`.
- **Resolve's own FCPXML 1.10 export writes `file://localhost/Volumes/...%20...`.** This is **inferred**: it comes
  from a hand-made fixture that copies the shape of Resolve's export (AtelierSuite/Montage PR #57,
  https://github.com/AtelierSuite/Montage/pull/57). Both `file:///` and `file://localhost/` are valid file URLs.
  Prefer `file:///`.
- `as_uri()` percent-encodes `&`, `#`, `%` and `?`.
- XML escaping (`&amp;`) is done separately by the XML writer. Use `xml.etree`/`ElementTree` and never build XML by
  string formatting.
- **macOS Unicode:** use the path exactly as the OS or Resolve reports it (`GetClipProperty("File Path")`). Do not
  NFC/NFD-normalize it, because HFS+ stores NFD and APFS preserves whatever form it was given. **Inferred.**
- Every path must be absolute. Resolve resolves relative URLs against nothing useful. **Inferred.**

### 3.3 `start`, `format`, timecode, audio

`asset@start` is the media's start timecode as time: `start_tc_frames × frameDuration`. A file without timecode
gets `start="0s"`. Confidence: **verified** for semantics (Apple timing attributes, DTD). That Resolve conforms by
matching this against the file's real timecode is **inferred**, so take the value from ffprobe exactly.

**Drop frame (DF):**
- `tcFormat="DF"` goes on `sequence`, `multicam` and `asset-clip`.
- The time value is still the **frame count × 1001/30000s**. Here the frame count is the DF-label-to-frame-count
  conversion of the TC, not hours×3600×30.
- Only 29.97 and 59.94 can be DF.
- Confidence: **verified** for the attributes (DTD), **inferred** for Resolve's handling.

**`<format>`:**
- Write `frameDuration` (`1/25s`, `1001/30000s`, `1001/24000s`, `1/24s`, `1/30s`, `1/50s`, `1001/60000s`, `1/60s`)
  plus `width` and `height`.
- `name` (for example `FFVideoFormat1080p25`) is optional. From 1.7, explicit attributes override the name.
- `colorSpace="1-1-1 (Rec. 709)"` is optional.
- ISOs with a different resolution, such as UHD in a 1080 PGM, get their own `<format>` with the same
  frameDuration. Reference it from `asset@format` and `asset-clip@format`. The multicam and the sequences use the
  PGM's format.
- Confidence: **verified** (Apple `format` page, DTD).

**Audio on `<asset>`:**
- Write `hasAudio="1" audioSources="N" audioChannels="C" audioRate="48000"`, and `hasVideo="1"` plus optional
  `videoSources="1"`.
- Apple recommends setting these "for all assets" in case the media is offline.
- `audioSources` = number of audio tracks (streams); `audioChannels` = channels per source.
- On `<sequence>`, `audioRate` is the enum `48k` (also `44.1k`, `96k`…), and `audioLayout` is
  `mono|stereo|surround`.
- Confidence: **verified** (Apple `asset` page, DTD).

## 4. Rational time rules and pitfalls (Q4)

**Format** (**verified**: DTD comment, Apple Timing Attributes page):
- A time is a rational number of seconds with an `s` suffix: `"1001/30000s"`, `"262/25s"`.
- The numerator is 64-bit and the denominator 32-bit.
- Whole seconds may be written `"5s"`.

**Alignment:**
- Values should be whole multiples of the timeline's frameDuration. When they are not, FCP "inserts a gap … A
  warning message appears" (Apple, **verified**).
- Resolve 14.1 fixed "a crash when importing FCPXML files with a subframe video edit"
  (https://www.blackmagicdesign.com/support/readme/702f18bf874d480d8dc79e626b7ba9bf, **verified**).
- So never emit sub-frame values.

**Generating times:** keep everything as integer frames and convert at the very end with `Fraction(frames) * fd`.
Write `f"{num}/{den}s"`, or `f"{num}s"` when the denominator is 1. For 29.97, `fd = Fraction(1001, 30000)`. Never
use `frames/29.97` or floats. **Verified** by reasoning plus the DTD rules.

**Pitfalls (inferred unless noted):**
1. **Spine and angle offsets must be contiguous.** Each child's `offset` = previous `offset` + previous
   `duration`, starting at the container's `tcStart`. A mismatch makes the importer insert gaps or shift clips. The
   sample was checked with a script for this.
2. **Start TC mismatch.** If `asset@start` differs from the file's real TC, Resolve can conform the wrong range or
   leave the clip offline. Use the ffprobe TC exactly; for a file without TC, write `0s`.
3. **`asset-clip` range outside the media.** `start < asset.start` or `start + duration > asset.start + asset.duration`
   gives offline or black frames, or a rejected clip. Clamp to the media, and use gaps to fill.
4. **Off-by-one.** Segments are half-open `[start, end)`, so `duration = end - start` frames. The contract's
   segments cover `[0, duration)` exactly, so the sum of mc-clip durations must equal the sequence duration. The
   validator checks this.
5. **Frame rate / DF consistency.** All `<format>` frameDurations in one multicam must be equal; the contract
   already enforces the same nominal rate. Mixing DF and NDF between sequence and assets gives wrong TC display.
6. **Duplicate or empty names.** Use unique project names on every run, for example
   `"<PGM name> Cut 2026-11-02 14.03"`, because import collisions fail silently (§7).
7. **Never write `start` on `<asset>` as a float, and never use timecode strings.** Both are invalid.

## 5. Two projects in one file; lanes (Q5)

**FCPXML allows it** (**verified**, DTD): `<event>` can hold any number of `<project>` elements, and one `<media>`
in `<resources>` can be referenced from several projects.

**Resolve UI** (**verified**):
- 18.1 added "Ability to select specific timelines to import from FCPXML files".
- Manual 20 p. 1179 describes the import dialog: "Import timeline: If there are multiple sequences within the
  selected XML source file, this pop-up menu lets you choose which sequence to import".
- Since 18.6 several timeline files can be imported in one action. Resolve pauses after each to show its settings
  (manual p. 1178).

**Scripting API:** `ImportTimelineFromFile` returns **one** Timeline. Which project(s) it imports from a
multi-project FCPXML is **unknown**: not documented, not found.

**Recommendation:**
- Write **two files**, `<base>_cut.fcpxml` (multicam media + Cut) and `<base>_stacked.fcpxml` (assets + Stacked),
  and import them with two calls. This is deterministic on every version.
- Keep the one-file sample as a reference. The engine can also emit a combined file for UI users, who choose the
  timeline in the dialog.

**Lanes:**
- Connected clips are story elements anchored to a parent, with `lane="n"`: n>0 above, n<0 below. Their `offset` is
  in the parent's local time, which starts at the parent's `start` (**verified**: DTD and Apple Story Elements page).
- Resolve maps each lane to its own video track (V1 = spine, lane 1 = V2, …) and each clip's audio to separate audio
  tracks (**inferred**). The Montage fixture notes that Resolve's own export writes extra tracks as lanes
  ("V2 and A1 connected on lanes").
- In the Stacked sample the spine holds one `<gap>` for the full multicam range, so V1 will probably be empty, with
  CAM1 on V2, CAM2 on V3 and PGM on V4.
- If an empty V1 bothers users, put the earliest-starting file in the spine instead of the gap, with the others
  connected.

## 6. Known Resolve FCPXML / multicam bugs and limits (Q6)

| Item | Source | Confidence |
|---|---|---|
| FCPX multicam might come in as compound clips (manual wording; see §2.4) | Manual 20 p. 1137 | verified text / effect unknown |
| Viewer shows up to 25 angles (Resolve 18); 21.1: shortcuts "switch or cut up to 25 multicam angles". No hard maximum angle count found | readme/42fbd7d6…, readme/59dd4eef… | verified |
| Angle names in Resolve come from the multicam's **track names** ("You can rename tracks to change the angle name", manual p. 895). Whether `mc-angle@name` becomes the track or angle name on import is unknown | Manual 20 ch. 42 | verified / unknown |
| Multicam audio: 19.1 added a "Source audio channels option when editing multicam angles"; 21.0 "Multicams can include all audio tracks for each angle". So before 21, an angle likely carries only one (adaptive) audio track, and multi-track ISO audio may collapse | readme/18efd850…, readme/6aefbffe… | verified notes / inferred effect |
| Sub-frame edits crashed the FCPXML import (fixed in 14.1); still avoid them | 14.1 release notes | verified |
| FCPXML multicam transforms were imported wrongly (fixed in 12.5.3) | 12.5.3 release notes | verified |
| Timeline import can **merge pool media by a coarse identity**, cross-linking similar files (measured for .drt imports on 19.1.3) | https://github.com/samuelgursky/davinci-resolve-mcp/blob/main/docs/reference/api-limitations.md | documented (other format) |
| Blackmagic forum threads on FCPXML multicam import | forum.blackmagicdesign.com answers HTTP 202 with an empty body to scripted clients; search engines returned nothing usable from here | **not researched**: gap |

## 7. `MediaPool.ImportTimelineFromFile` (Q7)

From the README (21.0.4 text; the same option list since at least 17.x/18.x, README revisions of 2021 to 2023
checked). Confidence: **verified**.

```
ImportTimelineFromFile(filePath, {importOptions}) --> Timeline
  # file types: AAF/EDL/XML/FCPXML/DRT/ADL/OTIO
  "timelineName":        string, name of the timeline to be created (not for DRT)
  "importSourceClips":   Bool, import source clips, True by default (not for DRT)
  "sourceClipsPath":     string, filesystem path to search for source clips if the media is inaccessible in
                         their original path and if "importSourceClips" is True
  "sourceClipsFolders":  list of Media Pool Folder objects to search for source clips if the media is not present
                         in the current folder and if "importSourceClips" is False (not for DRT)
  "interlaceProcessing": Bool, valid only for AAF import
```

**Return value:** a `Timeline` object, or `None` on failure. There is no error message. **Verified** (README);
the silent `None` is **documented**.

**Measured behaviour** (davinci-resolve-mcp `api-limitations.md`, Studio 19.1.3, 21.0.4), **documented**:
- It returns `None` when a timeline with the requested name already exists.
- For FCP7 XML the internal sequence name beats `timelineName`. AAF and OTIO honour `timelineName`. FCPXML was not
  measured, so treat it as the FCP7 case and make the `<project name>` itself unique.
- `importSourceClips=False` + `sourceClipsFolders=[root]` gave repeatable imports **without duplicating media**
  (FCP7/AAF). DRT always re-imports media. OTIO with `importSourceClips=False` comes in all-offline.
- The same doc says bare `hasattr()` is always True on Resolve objects. Probe capabilities with
  `"Name" in dir(obj)` or `callable(getattr(obj, "Name", None))`.

**Media already in the pool:** the user picks the clips in the Media Pool, so they are already there.
- Recommended first attempt (**inferred**):
  `{"timelineName": unique, "importSourceClips": False, "sourceClipsFolders": [folders holding the picked clips]}`.
- If it returns `None`, retry with `{"importSourceClips": True}`. Resolve then links by path and may add duplicate
  pool entries.
- Before importing, create and select a dedicated bin (`MediaPool.AddSubFolder(root, "PGM2MC …")` plus
  `SetCurrentFolder`) so the multicam item and any duplicates land there.
- Check the result: open the timeline and read each item's `GetMediaPoolItem().GetClipProperty("File Path")`.

## 8. Clip properties and selection (Q8)

**`GetClipProperty(propertyName=None)`** (**verified**, README):
- With no argument it returns a dict of all properties. Use this to log the real keys once per run.
- An invalid key returns a "trivial result", not an error.
- The README does not list the keys.

Key names to use (**inferred**: standard Clip Attributes and Media Pool column names; the davinci-resolve-mcp
multicam helper reads `"Start TC"`, `"FPS"`, `"Frames"` and `"Duration"`):

| Need | Key | Typical value |
|---|---|---|
| file path | `"File Path"` | `/Users/editor/Shoot/CAM1.mov` |
| name | `"Clip Name"` / `"File Name"` | `CAM1.mov` |
| start TC | `"Start TC"` (also `"End TC"`) | `01:00:00:00` |
| fps | `"FPS"` | `25` / `29.97` (number or string, depending on version) |
| duration | `"Duration"` (TC string), `"Frames"` (count) | `00:01:00:00`, `1500` |
| resolution | `"Resolution"` | `1920x1080` |
| drop frame | `"Drop frame"` | `0`/`1` (unverified) |
| type | `"Type"` | `Video + Audio`, `Video`, `Audio`, `Timeline`, `Multicam`, `Compound` |
| audio | `"Audio Ch"`, `"Sample Rate"` | |

Design advice: from Resolve, use only `"File Path"`, plus `"Type"` to filter out timelines and multicams. Let the
engine's ffprobe supply fps, TC and duration, which are authoritative and version-independent.

**Selection:**
- `MediaPool.GetSelectedClips() --> [MediaPoolItems]` and `MediaPool.SetSelectedClip(item)` are **verified** in the
  README from the revision dated **20 Sep 2024** (Resolve 19.0.x/19.1) onward.
- They are **absent** from the READMEs dated 16 Jul 2024 (19.0) and 26 May 2023 (18.5). Checked via the gist
  revision history: https://gist.github.com/X-Raym/2f2bf453fc481b9cca624d7ca0e19de8/revisions.
- `Timeline.GetSelectedClips` (timeline items) only arrived in 21.0.4 (BMD scripting changelog).
- For 18.5/19.0 the fallback is `MediaPool.GetCurrentFolder().GetClipList()`, with the user picking PGM and ISOs
  in the dialog, or a "use all clips in the current bin" mode.
- Detect the API with `"GetSelectedClips" in dir(mediaPool)`.
- The return may be a list or a dict depending on version (**inferred**), so normalise with
  `list(x.values()) if isinstance(x, dict) else list(x or [])`.

## 9. Native multicam creation via the API? (Q9)

- **Resolve 18.5 to 21.0.x: no.**
  - There is no multicam-creating function in the README up to 21.0.4 (**verified**: full-text check of the
    21.0.4 README).
  - The davinci-resolve-mcp multicam guide states it explicitly: "Resolve's public scripting API does not expose
    creating native multicam clips, switching multicam angles, or flattening multicam edits."
- **Resolve 21.1 (Sep 2026) adds** `MediaPool.CreateMulticamClip([clips], {options})`,
  `TimelineItem.FlattenMulticam`, `PerformMulticamSmartSwitch` and `Timeline.AutoAlignClips`.
  - Options include `name`, `startTimecode`, `frameRate`, `angleSyncMode` (`MULTICAM_ANGLE_SYNC_TIMECODE|AUDIO|IN|OUT|MARKER`),
    `angleNameMode`, `multicamAudioMode`, `splitAtGaps`, `useFullClipExtents`, `createBinForSourceClips` and
    `detectSameCameraClipsMode`.
  - Sources: Blackmagic's scripting changelog as copied in
    https://github.com/samuelgursky/davinci-resolve-mcp/blob/main/docs/reference/resolve_scripting_changelog_21.1.md
    and the contributor-validated wrapper
    https://github.com/samuelgursky/davinci-resolve-mcp/blob/main/docs/reference/resolve211-multicam.md.
  - Confidence: **documented**.
- **There is still no API to set the active angle of each multicam timeline item.** So even on 21.1 the **cut
  timeline needs the FCPXML route**. On 21.1 the multicam item alone could be made natively, but 21.1 also makes
  Python scripting Studio-only.
- **Conclusion: the FCPXML route is required** for the target range (18.5 to 20) and remains the only way to get
  the angle-switched cut.

## 10. UIManager: free vs Studio, minimal dialog (Q10)

**Availability** (**verified**):

| Resolve | Studio | Free |
|---|---|---|
| ≤ 19.0.x | UIManager works | UIManager works ("slipped through") |
| 19.1 to 21.0.x | works | **UIManager disabled**. Scripts still run from Workspace > Scripts, but creating a UI shows a "needs Studio" prompt |
| ≥ 21.1 | works | **Python scripting moved to Studio** ("Advanced scripting now requires DaVinci Resolve Studio … We have moved the ability to script in Python to the Studio version") |

Sources:
- Blackmagic staff (Peter Chamberlain, 21 Nov 2024): "UIManager based scripts and workflows need DaVinci Resolve
  Studio or Fusion Studio … in Resolve 19.1, we addressed this issue". See
  https://forum.blackmagicdesign.com/viewtopic.php?f=21&t=149311&p=1100366#p1100366 (archived
  https://web.archive.org/web/20260921172248/https://forum.blackmagicdesign.com/viewtopic.php?f=21&t=149311&p=1100366).
- Thread "UIManager has been disabled in Resolve Free in v19.1":
  https://forum.blackmagicdesign.com/viewtopic.php?f=12&t=213158 (staff reply of 5 Jan 2026 confirms).
- 21.1 release notes: https://www.blackmagicdesign.com/support/readme/59dd4eef1f4941c29fb8dc48b33f5c87.
- Also: external or command-line scripting has long been Studio-only. Free Resolve only runs scripts from the
  Workspace > Scripts menu and the Console (**documented**: brianrossen/resolve-multicam-sync README).

**Implication:** the Resolve script needs a **no-UIManager path** for free 19.1 to 21.0. These are design choices
for the orchestrator:
- Use the media pool selection with defaults, plus a settings file, and print progress to the Console.
- Or show a GUI from the engine's own venv process, for example tkinter in the subprocess. It then runs in its own
  process with its own main thread, which avoids macOS main-thread issues inside Resolve.

Detection (**inferred**): `ui = getattr(fu, "UIManager", None)`. Then wrap the `AddWindow` call in try/except,
because on free 19.1+ the call opens Resolve's Studio prompt and returns nothing useful.

**Minimal UIManager dialog** (Python, run from Workspace > Scripts):
- `resolve`, `fu`/`fusion`, `bmd` and `app` are injected globals (**documented** by the README's console examples).
- Widget and event names follow Fusion's UIManager (Qt-like). They are **inferred** from community docs (WeSuckLess
  "UI Manager" examples, Reactor scripts) and not from a BMD reference. Test on Studio.

```python
# Workspace > Scripts > Utility > PGM to Multicam.py   (stdlib only)
ui = fu.UIManager
disp = bmd.UIDispatcher(ui)
win = disp.AddWindow(
    {"ID": "PGM2MC", "WindowTitle": "PGM to Multicam", "Geometry": [200, 200, 640, 520]},
    ui.VGroup({"Spacing": 6}, [
        ui.HGroup({"Weight": 0}, [ui.Label({"Text": "Program (PGM)", "Weight": 0.3}),
                                  ui.ComboBox({"ID": "Pgm", "Weight": 0.7})]),
        ui.HGroup({"Weight": 0}, [ui.Label({"Text": "Min shot (frames)", "Weight": 0.3}),
                                  ui.SpinBox({"ID": "MinShot", "Minimum": 1, "Maximum": 250, "Value": 6, "Weight": 0.7})]),
        ui.CheckBox({"ID": "Refine", "Text": "Refine offsets (+/-3 frames)", "Checked": True, "Weight": 0}),
        ui.HGroup({"Weight": 0}, [ui.Label({"Text": "Output folder", "Weight": 0.3}),
                                  ui.LineEdit({"ID": "OutDir", "PlaceholderText": "/path/to/folder", "Weight": 0.7})]),
        ui.TextEdit({"ID": "Log", "ReadOnly": True, "Weight": 1}),
        ui.HGroup({"Weight": 0}, [ui.Button({"ID": "Run", "Text": "Run"}),
                                  ui.Button({"ID": "Close", "Text": "Close"})]),
    ]))
itm = win.GetItems()
for name in ["PGM.mov", "CAM1.mov", "CAM2.mov"]:
    itm["Pgm"].AddItem(name)

def log(msg):
    itm["Log"].Append(msg)            # alternative: itm["Log"].PlainText += msg + "\n"

def on_run(ev):
    log("PGM=%s min_shot=%d refine=%s out=%s" % (itm["Pgm"].CurrentText, itm["MinShot"].Value,
                                                itm["Refine"].Checked, itm["OutDir"].Text))
    # start the engine with subprocess.Popen here; to keep the UI responsive, poll it from a ui.Timer
    # ({"ID": "Poll", "Interval": 250} + win.On.Poll.Timeout) or run it blocking and show the log afterwards.

def on_close(ev):
    disp.ExitLoop()

win.On.Run.Clicked = on_run
win.On.Close.Clicked = on_close
win.On.PGM2MC.Close = on_close         # window close button
win.Show()
disp.RunLoop()
win.Hide()
```

## 11. Install folders, Python, subprocess (Q11)

**Script folders.** Resolve scans these at startup and lists the scripts under Workspace > Scripts. `Utility` makes
a script visible on all pages. **Verified** (README):

| OS | All users | Specific user |
|---|---|---|
| macOS | `/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/` | `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/` |
| Windows | `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\` | README says `%APPDATA%\Roaming\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts`; `%APPDATA%` already ends in `\Roaming`, so the real path is `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\` (**inferred**: README typo) |
| Linux | `/opt/resolve/Fusion/Scripts/Utility/` (or `/home/resolve/...`) | `~/.local/share/DaVinciResolve/Fusion/Scripts/Utility/` |

Resolve must be restarted, or the Scripts menu reopened, to see new scripts (**inferred**: "On startup … scans").

**Python:**
- README: scripting "requires one of the following to be installed (for all users): Lua 5.1, Python >= 3.6 64-bit,
  Python 2.7 64-bit". Before 21.1 Resolve uses a **system-installed** Python.
- Resolve 18: "Support for all modern and future python 3 versions for scripting". 18.6.5: "Import module support
  for Python 3.12".
- 21.1: "Built-in Python support for script menu and console scripts"; Python 2 dropped.
- All **verified** (README, release notes).
- On macOS the python.org installer, which installs into `/Library/Frameworks/Python.framework` for all users, is
  the commonly recommended interpreter. Homebrew Python is often not picked up (**inferred**).
- The Resolve-side script must stay **stdlib-only and 3.6-compatible**: no f-string `=` specifiers, no
  `dataclasses`, no walrus operator. The contract already says stdlib-only.

**subprocess:**
- `subprocess.Popen` works in Resolve's in-process CPython. It is ordinary CPython, and community scripts use it
  (**inferred**).
- Launch the **venv's python by absolute path**. Do not use `sys.executable`, which can point at the Resolve binary
  (**inferred**).
- Remove `PYTHONHOME` and `PYTHONPATH` from the child's environment so Resolve's interpreter settings do not leak
  into the venv (**inferred**).
- On Windows pass `creationflags=subprocess.CREATE_NO_WINDOW` to avoid a console flash.

**PATH on macOS:**
- GUI apps launched from Finder or Dock inherit launchd's minimal PATH, `/usr/bin:/bin:/usr/sbin:/sbin`. So
  `/opt/homebrew/bin` and `/usr/local/bin`, where Homebrew installs ffmpeg, are **missing** (**inferred**: standard
  macOS behaviour, not Resolve-specific).
- Have `install_mac.sh` record absolute `ffmpeg`/`ffprobe` paths in a config file next to the venv. The script then
  passes `--ffmpeg/--ffprobe`, or builds
  `env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "")`.

## 12. Deliverable: `docs/sample_multicam.fcpxml`

What it contains:
- 25 fps, 1920x1080, three assets:
  - CAM1, TC 01:00:00:00, 60 s;
  - CAM2, TC 01:00:02:00, 58 s;
  - PGM, TC 01:00:01:00, 55 s.
- One multicam with three angles (`tcStart` 3600s, leading gaps for CAM2 and PGM).
- Project **Cut** with three mc-clips, CAM1 → CAM2 → PGM, with video/audio split angles (audio = PGM). Its
  `tcStart` equals the PGM TC, so record TC = PGM TC.
- Project **Stacked** with a spine gap and the three files connected on lanes 1, 2 and 3 at their synced positions.

**Validation** (run here):
- Apple's `FCPXMLv1_10.dtd`, from the CommandPost mirror
  (`https://raw.githubusercontent.com/CommandPost/CommandPost/develop/src/extensions/cp/apple/fcpxml/dtd/FCPXMLv1_10.dtd`,
  sha256 `32cbad28…59ff53`), loaded with lxml `etree.DTD(...).validate(doc)`. Result: **valid: True**.
- The same document with `version` bumped also validates against `FCPXMLv1_11.dtd` and `FCPXMLv1_13.dtd`.
- A negative control proves the validator rejects bad input: the same file as 1.8 without `src` fails with
  `DTD_MISSING_ATTRIBUTE: Element asset does not carry attribute src`.
- Additional script checks pass: every time is frame-aligned; spine and mc-angle offsets are contiguous; the
  sequence durations add up; every mc-clip range lies inside the multicam.
- Command used (the scripts live in the scratch dir and were not committed):
  `uv run --with lxml python -I validate.py docs/sample_multicam.fcpxml FCPXMLv1_10.dtd`.
- The engine's tests should do the same: vendor the DTD under `tests/` and validate the generated output.

### Mapping the Analysis JSON (CONTRACT v1) to FCPXML

All in integer frames at the PGM rate `fd = den/num` s (e.g. `fps {num:25,den:1}` gives `fd = 1/25`):

```
pgm_pos   = 0                                   # PGM angle file starts at PGM frame 0
iso_pos_i = -offset_frames_i                    # ISO-local frame 0 sits at PGM frame -offset_i
origin    = min(0, min_i iso_pos_i)             # earliest angle start, in PGM frames
lead_X    = pos_X - origin                      # leading gap of angle X (0 => no <gap>)
mc_len    = max over angles(pos_X + duration_X) - origin
mc_tc0    = pgm.start_tc_frames + origin  (if < 0: use 0 and accept a non-TC origin)   # multicam@tcStart
asset X   : start = start_tc_frames_X * fd, duration = duration_frames_X * fd
angle X   : [<gap offset=mc_tc0 start=mc_tc0 duration=lead_X>] <asset-clip offset=mc_tc0+lead_X start=asset.start duration=asset.duration>
Cut seq   : tcStart = pgm.start_tc_frames, duration = pgm.duration_frames
segment s : mc-clip offset = pgm.start_tc_frames + s.start
                    start  = mc_tc0 + (pgm_pos - origin) + s.start
                    duration = s.end - s.start
                    mc-source angleID=s.angle srcEnable=video (+ pgm audio)  | s.angle == "pgm": srcEnable=all
Stacked   : tcStart = mc_tc0, duration = mc_len, spine <gap offset=mc_tc0 start=mc_tc0 duration=mc_len>,
            connected <asset-clip lane=k offset=mc_tc0+lead_X start=asset.start duration=asset.duration>
```

(The sample uses exactly these numbers: offset_CAM1 = +25, offset_CAM2 = -25, so origin = -25 (CAM1), lead_CAM1 = 0,
lead_CAM2 = 50, lead_PGM = 25, mc_tc0 = 90025 - 25 = 90000 (01:00:00:00), mc-clip start = 90025 + s.start.)

Two cases in this mapping are uncertain:
- **mc-clip ranges on angles with no media.** An mc-clip may cover multicam time where the selected angle has no
  media, for example an ISO that started late. The engine already maps such frames to `pgm`, so this cannot
  happen. Any remaining edge would show black in Resolve, not fail (**inferred**).
- **ISOs that are not DTD-legal as-is.** If an ISO would need a negative `asset-clip@start` because the file starts
  before the multicam origin, that cannot happen with `origin = min(...)` (**verified** by construction).

## 13. Things to test first on a real Resolve (spike list)

1. Import `docs/sample_multicam.fcpxml`, with real 25 fps files dropped at those paths, via File > Import > Timeline
   in 18.5, 19.x and 20.x. Check:
   - Is a **Multicam** item created in the Media Pool, with badge and `GetClipProperty("Type")`, and not a
     Compound?
   - Are the angle names CAM1, CAM2 and PGM?
   - Do the Cut timeline's clips show as multicam clips with the right active angle?
   - Is the audio taken from PGM (split honoured)?
2. The same through `ImportTimelineFromFile` in the Console. Which project does a two-project file import? Is
   `timelineName` honoured for FCPXML? What happens to media that is already in the pool with
   `importSourceClips=False` + `sourceClipsFolders`?
3. 29.97 DF sample (`1001/30000s`, `tcFormat="DF"`): check TC display and alignment.
4. ISO with 4 mono audio tracks (`audioSources=4 audioChannels=1`): what audio ends up in the angle?
5. Stacked: which tracks do the lanes land on, and is V1 empty?
6. If step 1 yields compound clips: does "Convert Compound Clips (Timelines) to Multicam Clips" fix it in place,
   with the cut keeping its angles?

## 14. Source index

- Blackmagic release notes (full text): `https://www.blackmagicdesign.com/support/readme/<releaseId>`. The IDs come
  from `https://www.blackmagicdesign.com/api/support/us/downloads.json`.
- DaVinci Resolve 20 Reference Manual: https://documents.blackmagicdesign.com/UserManuals/DaVinci_Resolve_20_Reference_Manual.pdf
  - ch. 42 "Multicam Editing", pp. 889-899;
  - ch. 55 p. 1137 "About Nested Sequences and Compound Clips";
  - ch. 58 pp. 1178-1180 "Importing XML Project Files".
- Resolve scripting README 21.0.4 (X-Raym copy, with revision history): https://gist.github.com/X-Raym/2f2bf453fc481b9cca624d7ca0e19de8
  (formatted: https://extremraym.com/cloud/resolve-scripting-doc/).
- BMD scripting changelog 21.1 (copy): https://github.com/samuelgursky/davinci-resolve-mcp/blob/main/docs/reference/resolve_scripting_changelog_21.1.md
- Measured API behaviour: https://github.com/samuelgursky/davinci-resolve-mcp/blob/main/docs/reference/api-limitations.md
- Apple FCPXML Reference: https://developer.apple.com/documentation/professional-video-applications/fcpxml-reference
  - pages: `multicam-media`, `audio-components-and-multicam`, `mc-clip`, `asset`, `format`, `timing-attributes`,
    `story-elements`.
- Apple FCPXML DTDs 1.0 to 1.14 (mirror): https://github.com/CommandPost/CommandPost/tree/develop/src/extensions/cp/apple/fcpxml/dtd
- UIManager / free version: https://forum.blackmagicdesign.com/viewtopic.php?f=12&t=213158 and
  https://forum.blackmagicdesign.com/viewtopic.php?f=21&t=149311&p=1100366#p1100366 (read via web.archive.org
  snapshots from 2026-09-21).
