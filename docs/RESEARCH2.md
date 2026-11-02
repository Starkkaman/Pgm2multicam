# pgm2multicam — research 2: FCPXML transitions, stills, transforms and crops for DaVinci Resolve

Researched 2026-11-02 for CONTRACT "v2 additions". Same rules as `RESEARCH.md`: nothing here was test-imported,
because this container has no Resolve. Each answer carries a confidence label:

- **verified**: confirmed in a primary source (Apple DTD or docs, a real Final Cut Pro export, the Blackmagic manual,
  release notes or scripting README), or checked here by running something (DTD validation).
- **documented**: stated by a third-party tool or library that targets FCP or Resolve and describes the behaviour.
- **inferred**: my reasoning from indirect evidence. Test it on a real Resolve before relying on it (§9).

---

## 0. TL;DR

1. **Transition:** a `<transition offset duration>` sits *between* two spine items. The neighbours keep contiguous
   offsets, because the transition takes no timeline time. It overlaps the cut: A plays on past its out point and
   B starts before its in point, so **both sides need handles**.
   - Offset rule: `offset = cut − floor(D/2)` (25 frames → 12 before the cut, 13 after).
   - Effect: `<filter-video ref>` pointing at `<effect name="Cross Dissolve"
     uid="FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265"/>` (**verified**: real FCP exports).
   - Resolve imports Cross Dissolve from FCPXML (**verified**: manual) and matches transitions **by name**
     (**documented**: auto-editor).
   - Between `<mc-clip>`s the DTD allows it (**verified**); whether Resolve imports it there is **inferred**.
2. **Dip to colour:** the FCP "Fade To Color" uid was **not found**. Build the dip from verified pieces instead:
   Cross Dissolve → solid-colour PNG slug → Cross Dissolve (§1.5, used in the sample). As a spike, also try a
   single transition named "Dip to Color Dissolve".
3. **Still (PNG):** declare it like this (**documented** for FCP; Resolve imports PNG stills from FCPXML,
   **verified**: manual):
   - format: `FFVideoFormatRateUndefined` with width and height and **no frameDuration**, `colorSpace="1-13-1"`;
   - asset: `duration="0s"`, `hasVideo="1"`, no audio attributes;
   - placement: a `<video ref>` element, not `<asset-clip>`.
4. **Transform units** (**verified**, Apple + DTD):
   - `position` = percent of the **frame height** for both x and y, measured from the frame centre, **+y up**.
   - `scale` = a factor applied after spatial conform. A missing `adjust-conform` means **fit**.
   - `trim-rect` left/top/right/bottom are also **percent of frame height**, so left/right need ×(w/h).
   - Formulas in §3.3 and §7.
5. **Q5 verdict:** Resolve documents importing **Position, Scale, Rotation and Opacity** from FCPXML (**verified**:
   manual table). **Crop is not in that table**, so it is presumably not imported.
   - Keep writing `adjust-transform` and `adjust-crop`: they are correct for FCP and harmless.
   - Have the Resolve script **re-apply every SuperSource box** after import with `TimelineItem.SetProperty`
     (`Pan`, `Tilt`, `ZoomX`, `ZoomY`, `CropLeft`, `CropRight`, `CropTop`, `CropBottom`, `Scaling`). The units are
     in §5.2.
6. **Lanes on an mc-clip:** the DTD allows connected clips (`lane="1..n"`) inside `<mc-clip>` (**verified**).
   Resolve putting lane n on track V(n+1) is **inferred**.
7. `docs/sample_v2.fcpxml` passes the DTDs for **1.10, 1.11 and 1.13**, plus timing and handle checks (§8).

---

## 1. Transitions (Q1)

### 1.1 Placement and overlap semantics

- The DTD (**verified**) says `<!ELEMENT spine (%clip_item; | transition)*>`, where `transition` has the attributes
  `name`, `offset` and `duration` (REQUIRED) and the children `filter-video?, filter-audio?, markers, metadata`.
  The DTD comment: "the transition element overlaps the last 2 seconds of the previous video … and the first 2
  seconds of the next video".
- The Apple `transition` page (**verified**): "Transition effect applies to elements contained in a `spine` or
  `mc-angle`. Use the `offset` and `duration` attributes to define the position and extent".
- **Real FCP 30 fps export** (OTIO adapter `fcpx_example.fcpxml`, **verified**):
  ```xml
  <asset-clip name="IMG_0268" offset="0s" ref="r4" duration="28500/3000s" .../>      <!-- ends at 9.5 s -->
  <transition name="Cross Dissolve" offset="9s" duration="1s"> ... </transition>     <!-- 9.0–10.0 s -->
  <clip name="IMG_0740" offset="28500/3000s" start="1500/3000s" duration="28500/3000s"> ...
  ```
  This shows four things:
  - The transition is **centred on the cut**: offset = 9.5 s − 0.5 s.
  - The neighbours stay **contiguous**: B's offset = A's offset + A's duration.
  - B starts 0.5 s into its media (`start="1500/3000s"`): that is the half-transition handle B gives up.
  - A has to play its media 0.5 s **past** its out point.
- **Rule for the writer:**
  - `pre = floor(D/2)`, `post = D − pre`.
  - `transition@offset = cut − pre`, `transition@duration = D`.
  - Handles needed: **A** must have `post` frames of media after its out point; **B** must have `pre` frames
    before its in point (`start − pre ≥ media start`).
  - Spine offsets and durations of the clips are unchanged.
  - Odd D (25 frames at 25 fps) is fine as a 12/13 split. Never emit `D/2` as a sub-frame offset.
  - Confidence: **verified** for even D (FCP exports). The 12/13 split is **inferred**; Resolve itself makes
    whole-frame transitions at odd lengths.
- A transition as the **first or last** spine element (fade from or to nothing) exists in FCP exports (offset = clip
  start). The contract does not need it.
- **Validator:** the spine contiguity check has to **skip `<transition>`** elements (the v1 validator would reject
  them).

### 1.2 Effect reference and UIDs

| FCP effect | `<effect>` | Confidence |
|---|---|---|
| Cross Dissolve | `name="Cross Dissolve" uid="FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265"` | **verified**: identical in the OTIO sample, swift-fcpxml FCP exports (`TransitionMarkers1.fcpxml`), the Montage Resolve-shaped fixture and auto-editor's writer |
| Audio Crossfade (`filter-audio`) | `name="Audio Crossfade" uid="FFAudioTransition"` | **verified** (same exports) |
| Fade To Color (FCP's dip) | uid **not found** in any reachable export or source | gap: get it from one real FCP or Resolve export (§9) |

- FCP writes `<param name="Look" key="1" value="11 (Video)"/>`, `Amount`, `Ease` and `Ease Amount`, plus a binary
  `effectConfig` `<data>`, inside `filter-video`. They are **optional**: the DTD allows an empty `filter-video`,
  and the Resolve-shaped Montage fixture uses `<filter-video ref="r7" name="Cross Dissolve"/>`. Leave them out.
- **Resolve mapping** (manual 20, ch. 55, "About Supported Transitions", **verified**):
  - FCP XML imports ten transitions: Cross Dissolve, Additive Dissolve, **Dip to Color Dissolve**, Edge Wipe,
    Center Wipe, Clock Wipe, Venetian Blind Wipe, Cross Iris, Diamond Iris and Oval Iris.
  - These are FCP 7 names, and the table has a single "FCP XML" column for both FCP 7 and FCP X.
  - auto-editor's FCPXML writer comments "FCP matches this uid …; **Resolve matches the transition by name**"
    (**documented**).
  - So write `transition@name` and `filter-video@name` = **"Cross Dissolve"** exactly.
- **Audio:** the manual's table says "Audio Transitions: No" for FCP X XML, although a release note fixed "importing
  a Final Cut Pro XML would not preserve audio transitions".
  - In our mc-clips the audio is the PGM angle on both sides, which is continuous, so an audio crossfade adds
    nothing.
  - **Omit `filter-audio`** from the transitions.

### 1.3 Between `<mc-clip>`s

- **DTD** (**verified**): any `%clip_item;` may neighbour a transition, and `mc-clip` is a clip_item.
- **Resolve** (**inferred**): Resolve allows transitions between multicam clips and between compound clips. Both
  outcomes of the open multicam-vs-compound question (RESEARCH §2.4) therefore support a dissolve.
- **Handles for mc-clips** are multicam time:
  - A: `A.start + A.duration + post ≤ multicam end`.
  - B: `B.start − pre ≥ multicam tcStart`.
  - The **active angle must also have media** there. If the ISO's angle has a leading or trailing `<gap>` inside
    the handle, the dissolve mixes with black.
  - The writer should check ISO coverage, `[−offset_frames, duration_frames − offset_frames)` in PGM frames,
    for the handle region.
- **Conform timeline:** the asset-clip handles must lie inside the asset (`asset.start ≤ start − pre`,
  `start + duration + post ≤ asset.start + asset.duration`).
- **When handles are short:**
  - shrink D to `2·min(available_pre, available_post)` (minimum 2 frames) and emit `WARN`;
  - or drop the transition.

### 1.4 Snippet: cross dissolve between two mc-clips

```xml
<effect id="r10" name="Cross Dissolve" uid="FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265"/>   <!-- in <resources> -->
...
<mc-clip ref="r11" offset="3600s" name="CAM1" start="3600s" duration="10s">
    <mc-source angleID="a1" srcEnable="video"/>
    <mc-source angleID="pgm" srcEnable="audio"/>
</mc-clip>
<transition name="Cross Dissolve" offset="90238/25s" duration="1s">   <!-- cut 3610 s − 12 frames, 25 frames -->
    <filter-video ref="r10" name="Cross Dissolve"/>
</transition>
<mc-clip ref="r11" offset="3610s" name="CAM2" start="3610s" duration="10s">
    <mc-source angleID="a2" srcEnable="video"/>
    <mc-source angleID="pgm" srcEnable="audio"/>
</mc-clip>
```

### 1.5 Dip to colour

**A. Recommended (used in the sample): build it from verified pieces.** The dip is
A → Cross Dissolve → solid-colour PNG slug → Cross Dissolve → B.

- **Pieces:** Cross Dissolve import and PNG still import are both **verified** in the manual.
- **Geometry:** the dip has duration D and is centred on `edit`.
  - `d1 = floor(D/2)`, `d2 = D − d1`.
  - The slug runs from `c1 = edit − ceil(d1/2)` to `c2 = edit + floor(d2/2)`, so it is at least 1 frame long.
  - Transition 1: `offset = c1 − floor(d1/2) = edit − d1`, duration d1.
  - Transition 2: `offset = c2 − floor(d2/2) = edit`, duration d2.
  - The picture is fully the colour exactly at `edit`, with no hold.
- **Slug:** a spine `<video ref=colour.png start=floor(d1/2) frames>`. That start value gives the slug the pre-handle
  transition 1 needs.
- **Handles:** A needs media up to `edit`, and B needs media from `edit`. That is the same as a real dip.
- **Generating the colour PNG:** the engine can write `NAME_stills/dip_RRGGBB.png` at PGM size from
  `transitions[].color`.
- **Cost:** two Cross Dissolves and a slug clip on V1 instead of one "Dip to Color Dissolve". The rendered picture
  is the same (a linear A→colour→B mix; Resolve's Dip has a colour-hold option, unused here).
- Confidence: pieces **verified**; overall behaviour **inferred**.

```xml
<asset id="r9" name="dip_000000" start="0s" duration="0s" hasVideo="1" format="r3" videoSources="1">
    <media-rep kind="original-media" src="file:///abs/OUTDIR/NAME_stills/dip_000000.png"/>
</asset>
...
<mc-clip ref="r11" offset="3635s" name="CAM1" start="3635s" duration="117/25s">...</mc-clip>   <!-- ends at c1 -->
<transition name="Cross Dissolve" offset="18197/5s" duration="3/5s"><filter-video ref="r10" name="Cross Dissolve"/></transition>
<video ref="r9" offset="90992/25s" name="dip to black" start="7/25s" duration="3/5s"/>
<transition name="Cross Dissolve" offset="3640s" duration="3/5s"><filter-video ref="r10" name="Cross Dissolve"/></transition>
<mc-clip ref="r11" offset="91007/25s" name="CAM2" start="91007/25s" duration="118/25s">...</mc-clip>
```

**B. Spike only: a single named transition.**

```xml
<transition name="Dip to Color Dissolve" offset="..." duration="..."/>
```
- No filter-video, so it needs no uid; the DTD allows that.
- Alternatively, use the FCP "Fade To Color" effect once its uid is known from a real export.
- If Resolve maps this to its Dip to Color Dissolve (colour black by default), the writer can switch to it.
- **Unknown:** how Resolve treats an unrecognised name (Cross Dissolve fallback, as for EDLs, or dropped).

### 1.6 Open design question: transitions into or out of lane content

- Stills and SuperSource content sit on **lanes above** the spine's PGM mc-clip (CONTRACT). A spine transition into
  a still or SuperSource segment dissolves only the V1 clips, so the lane clips still **cut in hard**.
- Options, for the orchestrator to decide:
  - (a) **Opacity ramp on the lane clips.** Use `<adjust-blend>` with `<param name="amount"><keyframeAnimation>`.
    The manual says imported Opacity is supported "and if keyframed, the animation will appear in DaVinci Resolve"
    (**verified** text, untested).
  - (b) **Put the still in the spine** instead of on a lane. Transitions then work natively, but the PGM-under-still
    fallback is lost.
  - (c) **A connected `<spine lane="1">`** (secondary storyline) holding a transition plus the still. This is DTD-valid,
    but Resolve's handling is **unknown**.

---

## 2. Still images (Q2)

- **FCP form** (**documented**: cutlass `go/CLAUDE.md` and `asset_builder.go`, which call these crash rules):
  - format: `name="FFVideoFormatRateUndefined"` with `width`/`height` and **no `frameDuration`**
    ("frameDuration on image formats → crash"), `colorSpace="1-13-1"` (sRGB);
  - asset: `duration="0s"`, `hasVideo="1"`, `videoSources="1"`, **no** `hasAudio`, `audioSources` or `audioRate`;
  - placement: a **`<video ref>`** element ("AssetClip for images → `addAssetClip:toObject:parentFormatID` crash").
- Real FCP exports in swift-fcpxml use `FFVideoFormatRateUndefined` with no frameDuration (**verified** for the
  format name).
- **DTD** (**verified**): `format@frameDuration` is `#IMPLIED`, so it may be omitted. `<video>` takes
  `%intrinsic-params-video;`, so it can carry `adjust-transform` and `adjust-crop`. It is an `anchor_item`
  (lanes allowed) and a `clip_item` (spine allowed).
- **Resolve:** the manual (ch. 55 "About Supported Still Image Formats", **verified**) says Resolve imports
  "greater-than-one-frame-in-duration TIF, JPG, PNG, DPX, TGA, and DNG still image files that appear in Final Cut
  Pro X … XML files. These clips appear as ordinary clips".
  - So **stills must be ≥ 2 frames**. `--min-shot 6` already ensures that.
  - The Montage fixture, hand-made "in the shape DaVinci Resolve exports", writes a PNG as `<asset-clip>` with
    `FFVideoFormatRateUndefined` and `duration="0/1s"`. That suggests Resolve's importer accepts both element
    forms (**inferred**).
- **`start`:** use `start="0s"` (cutlass and Montage do). If a still needs a pre-handle (the dip slug), give it a
  positive start equal to the handle. Stills are timeless.
- **Snippet:**
  ```xml
  <format id="r3" name="FFVideoFormatRateUndefined" width="1920" height="1080" colorSpace="1-13-1"/>
  <asset id="r7" name="s1" start="0s" duration="0s" hasVideo="1" format="r3" videoSources="1">
      <media-rep kind="original-media" src="file:///abs/OUTDIR/NAME_stills/s1.png"/>
  </asset>
  ...
  <mc-clip ref="r11" offset="3620s" name="PGM" start="3620s" duration="5s">
      <mc-source angleID="pgm" srcEnable="all"/>
      <video ref="r7" lane="1" offset="3620s" name="s1" start="0s" duration="5s"/>   <!-- offset = parent local time -->
  </mc-clip>
  ```

---

## 3. Transform and spatial conform (Q3)

### 3.1 Units (Apple `adjust-transform` page + DTD, **verified**)

- `position="x y"`: "percentages based on the original frame height. … if the original frame height is 1,080
  pixels, specifying `position="10 10"` moves the frame 108 pixels to the right and 108 pixels **up**."
  - Origin: the image sits centred at `0 0`.
  - Both axes are in **% of frame height**, so x is *not* % of width.
  - **+y is up**.
- `scale="sx sy"`: the DTD default is `"1 1"`, a **factor** (FCP exports write e.g. `0.45 0.45`). Apple's
  "percentage … (for example, `25 25`)" wording contradicts the DTD default; use factors.
- `anchor`: % of the scaled image height, default `0 0` = image centre. Scale and rotation pivot here; leave it at
  `0 0`.
- `rotation`: degrees, counter-clockwise positive.
- Element order inside `asset-clip` or `video` (DTD `%intrinsic-params-video;`): **`adjust-crop`,
  `adjust-corners`, `adjust-conform`, `adjust-transform`, `adjust-blend`, …**. lxml rejects any other order.
- Montage's FCPXML importer and exporter use the same model (**documented**): "Final Cut's position is measured
  from the frame's centre, upwards, in hundredths of the frame's height". Its code converts
  `x_frac = 0.5 + pos.x/100/aspect` and `y_frac = 0.5 − pos.y/100`, with aspect = sequence W/H.

### 3.2 Spatial conform

- DTD and Apple (**verified**): `<adjust-conform type="fit|fill|none">`, and "The absence 'adjust-conform' element
  implies 'fit'".
- The FCP 4K-in-1080 behaviour:
  - Fit scales the clip uniformly by `fit = min(W/w, H/h)` so it sits inside the sequence frame.
  - `scale` multiplies on top of that, so **scale 1 = fitted, not native pixels**. Montage's conformScale code
    agrees (fill → `max/min` ratio, none → `1/fit`).
  - `<conform-rate>` is about *frame rate*, not size. It is irrelevant here.
- **Which "frame height"?** With fit and a source no wider than the sequence (16:9 in 16:9, 4:3 in 16:9), the
  fitted image height equals the sequence height. "% of original frame height" and "% of sequence height" are then
  the same number.
  - Use **sequence height** for `position`.
  - Use the **clip's own (fitted) height** for `trim-rect`.
  - For wider-than-sequence sources (letterboxed), the two differ. **Inferred**: test before supporting it.
- Write `<adjust-conform type="fit"/>` **explicitly** on the box clips. It is the default in FCP, but explicit is
  safer for other importers. Whether Resolve reads it is unknown; the script fallback sets `Scaling` anyway.

### 3.3 Formulas: CONTRACT `rect`/`crop` → FCPXML

Inputs:
- sequence `W×H`, ISO `w×h`;
- `rect=[x0,y0,x1,y1]`: PGM fractions, top-left origin, y down;
- `crop=[l,t,r,b]`: fractions of the ISO frame.

```
fit      = min(W/w, H/h);  cw = w*fit;  ch = h*fit                # fitted ISO size in sequence px
sx       = (x1-x0)*W / (cw*(1-l-r))                               # scale factor, x
sy       = (y1-y0)*H / (ch*(1-t-b))                               # scale factor, y (≈ sx if the layout is uniform)
dcx, dcy = cw*(l-r)/2, ch*(t-b)/2                                 # centre of the kept area vs image centre (px, y down)
tcx, tcy = ((x0+x1)/2-0.5)*W, ((y0+y1)/2-0.5)*H                   # box centre vs frame centre (px, y down)
Px       = tcx - sx*dcx                                           # translation px, +right
Py       = -(tcy - sy*dcy)                                        # translation px, +UP
position = "{Px/H*100} {Py/H*100}"      scale = "{sx} {sy}"
trim-rect: left=l*cw/ch*100  right=r*cw/ch*100  top=t*100  bottom=b*100     # all % of frame height
```

- **Why it works:** a `trim` crop leaves the kept pixels where they were. Scale then acts about the image centre
  (anchor 0 0), and position translates.
- Order: **crop (in source space) → scale → position**.
- Confidence: **inferred** from FCP's documented "trim" semantics (§4). Montage notes Resolve's crop is likewise
  "cut before Position moves it".
- **Worked example** (the sample): ISO 3840×2160 in 1920×1080, `crop=[0.1,0,0.1,0]`.
  - Left box: `rect=[0.07,0.275,0.43,0.725]` gives `scale="0.45 0.45"`, `position="-44.4444 0"`,
    `trim-rect left="17.7778" right="17.7778"`.
  - Resolve values: Pan −480, Tilt 0, Zoom 0.45, CropLeft and CropRight 192.

---

## 4. Crop (Q4)

- **DTD and Apple** (**verified**):
  - `<adjust-crop mode="trim|crop|pan" enabled>` with children `crop-rect?`, `trim-rect?` and `(pan-rect, pan-rect)?`.
  - The mode enum is **`pan`, not "ken-burns"**. The negative control below shows lxml rejecting `ken-burns`.
  - `crop-rect` and `trim-rect` `left`, `top`, `right` and `bottom` are "a percentage of original frame height"
    (DTD comment and Apple `adjust-crop` page).
- **Modes:**
  - FCP's Inspector "Trim" mode masks the edges and leaves the image's size and position unchanged.
  - "Crop" enlarges the remaining area to fill the frame.
  - "Ken Burns" (`pan`) animates between two `pan-rect`s.
  - For SuperSource boxes use **`mode="trim"` + `<trim-rect>`**. The meaning of each mode comes from FCP's UI and
    is **documented** (common FCP knowledge; Apple's page names the modes only).
- **Units for left and right:** they are % of the frame **height** as well, so a 10 % width trim on 16:9 is
  `10 × 16/9 = 17.7778`.
  - This follows the DTD wording literally (**verified** text).
  - No real FCP export with a non-zero `trim-rect` was found to confirm the width axis (**inferred**). Spike item 4
    checks it.
- **Order:** FCP applies trim in the clip's own space before Transform (trimmed edges scale with the clip,
  **inferred**). Resolve likewise crops before Zoom and Position (Montage's DRP decoding, **documented**).
- **Resolve import of `adjust-crop`:** not listed among the imported settings (§5). Resolve maps FCP's **Ken Burns**
  to Dynamic Zoom (manual, **verified**); trim and crop are not mentioned.

---

## 5. Does Resolve import transforms and crops? (Q5)

### 5.1 Evidence

| Item | Evidence | Confidence |
|---|---|---|
| Position, Scale, Rotation from FCP X XML | Manual 20 ch. 55 table "Effects supported with imported AAF, XML, and EDL": "Position, Scale, Rotation — FCP X XML: Yes"; "About Supported Opacity, Position, Scale, and Rotation Settings … appear in the Inspector … If these settings have been keyframed, the animation will appear" | **verified** |
| Opacity, composite modes | Same table: Yes | **verified** |
| Crop / trim | **Absent** from the table and the text. Only "Ken Burns → Dynamic Zoom" is mentioned | **verified** absence → presumably not imported |
| Transforms on multicam | 12.5.3 release note: "Addressed an issue with FCPXML transforms for Multicam not getting imported correctly" | **verified** (shows it is supported and was once buggy) |
| Numeric fidelity (units, conform) | No forum or tool report found; search engines returned nothing usable from here | **gap** |
| Spatial conform `adjust-conform` | Not mentioned. Resolve's equivalent is the per-clip **Scaling** (Fit/Fill/Crop/Stretch) or the project input scaling | **unknown** |

**Verdict:**
- Emit `adjust-transform` and `adjust-crop` in the FCPXML: correct for FCP, likely right for position and scale in
  Resolve.
- Do not rely on Resolve for the crop or for exact numbers.
- The **Resolve script should re-apply all SuperSource box properties** after `ImportTimelineFromFile`. This makes
  the result deterministic whatever the importer did.
- Under §5.1 the script might compute them from `NAME_analysis.json` (path in `RESULT`) with the §7 function, or
  the writer could emit a sidecar. That design choice is for the orchestrator.

### 5.2 Fallback: `TimelineItem.SetProperty` (scripting README 21.0.4, "Looking up Timeline item properties", **verified** keys and ranges)

| Key | Range per README | Meaning | Value from §3.3 |
|---|---|---|---|
| `Pan` | −4·width … 4·width | Position X, **timeline px** from centre, +right | `Px` (for 1920 wide: −480) |
| `Tilt` | −4·height … 4·height | Position Y, timeline px from centre, **+up** | `Py` |
| `ZoomX`, `ZoomY` | 0 … 100 | factor, **1.0 = image at the clip's Scaling (Fit)** | `sx`, `sy` |
| `ZoomGang` | bool | link X/Y | `sx == sy` |
| `CropLeft`, `CropRight` | 0 … width | px | `l*cw`, `r*cw` (192 for 10 % of a fitted 1920-wide image) |
| `CropTop`, `CropBottom` | 0 … height | px | `t*ch`, `b*ch` |
| `CropRetain` | bool | "Retain Image Position" | `True` (kept pixels stay in place, matching FCP trim) |
| `Scaling` | `SCALE_USE_PROJECT=0, SCALE_CROP, SCALE_FIT, SCALE_FILL, SCALE_STRETCH` | per-clip input scaling | `2` = SCALE_FIT, so Zoom 1 = fit whatever the project setting |
| `AnchorPointX/Y` | ±4·width/height | leave 0 | — |
| `Opacity` | 0 … 100 | — | (for option 1.6a) |

- **Units and signs:**
  - That Pan and Tilt are timeline pixels from the centre with Tilt +up is **documented**: Montage decodes Resolve's
    stored Position as "fractions of the timeline's width and height, from its centre, up being positive", and the
    Inspector shows pixels.
  - That Zoom 1 = fit is **documented** (Montage: "Zoom X and Y … 1 fitting the frame").
  - That Crop is in timeline px of the fitted image, rather than source px (`l*w` = 384 for 4K), is **inferred**.
    Spike item 5 checks it.
- **Sketch for the Resolve script:**
  ```python
  # after tl = mp.ImportTimelineFromFile(path, {...}); box on lane k -> track V(k+1)
  for item in tl.GetItemListInTrack("video", track_index):
      if item.GetStart() == expected_start_frame:          # timeline frame incl. timeline start TC
          for k, v in (("Scaling", 2), ("ZoomGang", sx == sy), ("ZoomX", sx), ("ZoomY", sy), ("Pan", Px), ("Tilt", Py),
                       ("CropRetain", True), ("CropLeft", l*cw), ("CropRight", r*cw), ("CropTop", t*ch), ("CropBottom", b*ch)):
              item.SetProperty(k, v)
  ```
- **API limits** (README, **verified** absence): `SetProperty` sets **static** values only, with no keyframes, so
  `"animated": true` layouts cannot be rebuilt through it. Python scripting is Studio-only from 21.1 (RESEARCH §0).

---

## 6. Lanes above an `<mc-clip>` (Q6)

- **DTD** (**verified**):
  - `mc-clip` content is `(note?, timing, intrinsic-params-audio, mc-source*, (%anchor_item;)*, markers, filter-audio*, metadata?)`.
  - `anchor_item` includes `video`, `asset-clip`, `clip`, `mc-clip`, `title` and `spine`. "When present, anchored
    items must have a non-zero 'lane' value."
  - So connected clips on an mc-clip are valid. The sample validates.
- **Timing** (**verified**, DTD and Apple story elements): an anchored item's `offset` is in the **parent's local
  time**, which starts at the parent's `start`. For `<mc-clip start="3625s">`, a box that begins with the clip has
  `offset="3625s"`.
- **Compositing** (**verified**, Apple story elements): "items with higher lane indexes composite over elements
  with lower lane indexes". So use background lane 1, then boxes on lanes 2…n in CONTRACT `layer` order (layer 1 =
  lane 2).
- **Video transforms on an mc-clip itself** go on `<mc-source>`: the DTD gives `mc-source` the
  `%intrinsic-params-video;` and video filters, and `mc-clip` has none. Apple's `filter-video` page: "For multicam
  clips, apply the video filters on the individual angles, represented by `mc-source` elements".
  - The boxes therefore use plain **ISO `<asset-clip srcEnable="video">`**: simpler, independent of the
    multicam-vs-compound outcome, and no ISO audio.
- **Resolve** (**inferred**):
  - Lanes become tracks: spine = V1, lane 1 = V2, lane 2 = V3, lane 3 = V4. Lanes are per parent, so every mc-clip's
    lane 1 lands on the same V2.
  - The Montage fixture describes Resolve's own export as writing "V2 and A1 connected on lanes".
  - If Resolve imports the multicam as a compound clip, the connected clips should still attach to the timeline,
    not the compound's interior. Test it (spike item 6).

**Snippet: SuperSource segment**

```xml
<mc-clip ref="r11" offset="3625s" name="PGM (SuperSource ss1)" start="3625s" duration="10s">
    <mc-source angleID="pgm" srcEnable="all"/>
    <video ref="r8" lane="1" offset="3625s" name="s2 (SS background)" start="0s" duration="10s"/>
    <asset-clip ref="r4" lane="2" offset="3625s" name="CAM1 box" start="3625s" duration="10s" format="r2" tcFormat="NDF" srcEnable="video">
        <adjust-crop mode="trim">
            <trim-rect left="17.7778" top="0" right="17.7778" bottom="0"/>
        </adjust-crop>
        <adjust-conform type="fit"/>
        <adjust-transform position="-44.4444 0" scale="0.45 0.45"/>
    </asset-clip>
    <asset-clip ref="r5" lane="3" offset="3625s" name="CAM2 box" start="3625s" duration="10s" format="r2" tcFormat="NDF" srcEnable="video">
        <adjust-crop mode="trim"><trim-rect left="17.7778" top="0" right="17.7778" bottom="0"/></adjust-crop>
        <adjust-conform type="fit"/>
        <adjust-transform position="44.4444 0" scale="0.45 0.45"/>
    </asset-clip>
</mc-clip>
```

The box `start` is ISO media time: `asset.start + (p + offset_frames) × frameDuration` for PGM frame `p`. That is
the same mapping as v1 angles.

---

## 7. Pure functions for the writer (transcribe as-is)

```python
from fractions import Fraction

def box_params(rect, crop, src_w, src_h, seq_w, seq_h, uniform_tol=0.02):
    """CONTRACT rect [x0,y0,x1,y1] (PGM fractions, top-left, y down) + crop [l,t,r,b] (ISO fractions)
    -> FCPXML adjust-transform / trim-rect and Resolve SetProperty values. Spatial conform = fit."""
    x0, y0, x1, y1 = rect
    l, t, r, b = crop
    fit = min(seq_w / src_w, seq_h / src_h)
    cw, ch = src_w * fit, src_h * fit                       # fitted ISO size, sequence px
    sx = (x1 - x0) * seq_w / (cw * (1 - l - r))
    sy = (y1 - y0) * seq_h / (ch * (1 - t - b))
    if abs(sx - sy) <= uniform_tol * max(sx, sy):           # keep aspect when the detector is just noisy
        sx = sy = (sx + sy) / 2
    dcx, dcy = cw * (l - r) / 2, ch * (t - b) / 2           # kept-area centre vs image centre (px, y down)
    tcx, tcy = ((x0 + x1) / 2 - 0.5) * seq_w, ((y0 + y1) / 2 - 0.5) * seq_h
    px = tcx - sx * dcx                                     # px, +right
    py = -(tcy - sy * dcy)                                  # px, +up
    return {
        "fcpxml": {
            "position": (px / seq_h * 100, py / seq_h * 100),   # "%.4f %.4f"
            "scale": (sx, sy),
            "trim": {"left": l * cw / ch * 100, "top": t * 100, "right": r * cw / ch * 100, "bottom": b * 100},
        },
        "resolve": {"Scaling": 2, "ZoomGang": sx == sy, "ZoomX": sx, "ZoomY": sy, "Pan": px, "Tilt": py,
                    "CropRetain": True, "CropLeft": l * cw, "CropRight": r * cw, "CropTop": t * ch, "CropBottom": b * ch},
    }

def transition_place(cut, d):
    """Frames -> (offset, duration, frames needed from A after its out, frames needed from B before its in)."""
    pre = d // 2
    return cut - pre, d, d - pre, pre

def dip_layout(edit, d):
    """Dip as dissolve -> colour slug -> dissolve. Returns the A out point, the slug range [c1, c2), the slug's
    media start, and the two transitions as (offset, duration)."""
    d1 = d // 2; d2 = d - d1
    c1 = edit - (d1 - d1 // 2); c2 = edit + d2 // 2
    return {"a_out": c1, "slug": (c1, c2), "slug_start": d1 // 2,
            "t1": (c1 - d1 // 2, d1), "t2": (c2 - d2 // 2, d2), "b_in": c2}

def fcpx_time(frames, frame_duration=Fraction(1, 25)):
    v = Fraction(frames) * frame_duration
    return f"{v.numerator}s" if v.denominator == 1 else f"{v.numerator}/{v.denominator}s"
```

- Format numbers with up to 4 decimals and strip trailing zeros. FCP exports look like `position="0 22.5729"`.
- Write `-0` as `0`.
- Clamp each crop to `0 ≤ l+r < 1` and `0 ≤ t+b < 1`.

---

## 8. Deliverable: `docs/sample_v2.fcpxml`

- **Settings:** 25 fps, 1920×1080, TC 01:00:00:00.
- **Assets:**
  - CAM1 and CAM2: 3840×2160, 60 s each, with their own UHD format;
  - PGM: 1080p, 60 s;
  - stills `s1.png`, `s2.png` (SS background) and `black.png` (dip slug).
- **Multicam:** one multicam with angles a1, a2 and pgm.
- **Project "NAME Multicam Cut v2"** (45 s):
  1. CAM1 mc-clip, then a **1 s Cross Dissolve** (25 frames, 12/13), then a CAM2 mc-clip (video angle + PGM audio
     angle, as v1).
  2. PGM mc-clip with **PNG still s1 on lane 1**.
  3. PGM mc-clip carrying the **SuperSource**:
     - background PNG on lane 1;
     - CAM1 and CAM2 4K boxes on lanes 2 and 3, each `scale 0.45`, trimmed 10 % left and right
       (`17.7778` % of height), at `position ∓44.4444 0`, which centres them at x = 25 % and 75 %, the visible box
       36 % of the frame width.
  4. CAM1, then a **dip to black (30 frames)** built as dissolve (15 frames) → black slug (15 frames, start 7
     frames) → dissolve (15 frames), then CAM2.
- **Generator** (scratch, not committed): `/tmp/claude-1000/research/v2/gen_sample.py`. It computes the box values
  with the §7 function.

**Validation** (run here):
- Command: `uv run --with lxml python -I /tmp/claude-1000/research/v2/validate2.py docs/sample_v2.fcpxml FCPXMLv1_10.dtd FCPXMLv1_11.dtd FCPXMLv1_13.dtd`
  (DTDs from the CommandPost mirror, see RESEARCH §12).
- Output:
  ```
  DTD FCPXMLv1_10.dtd valid: True
  DTD FCPXMLv1_11.dtd valid: True
  DTD FCPXMLv1_13.dtd valid: True
  frame-aligned: True []
    transition Cross Dissolve at 3610.0s: 12 fr before / 13 fr after cut, handles OK
    transition Cross Dissolve at 3639.68s: 7 fr before / 8 fr after cut, handles OK
    transition Cross Dissolve at 3640.28s: 7 fr before / 8 fr after cut, handles OK
  timing checks: OK
  ```
- The script checks:
  - DTD validity;
  - frame alignment;
  - spine contiguity, skipping transitions;
  - each transition straddles its cut, with handles inside the multicam, asset or still range;
  - anchored clips lie inside their parent's local range and inside their media.
- **Negative control:** the same file with `mode="ken-burns"` fails with
  `DTD_ATTRIBUTE_VALUE: Value "ken-burns" for attribute mode of adjust-crop is not among the enumerated set`.

---

## 9. Spike list (real Resolve 18.5 / 19 / 20)

1. **Import `sample_v2.fcpxml`.**
   - Is the 25-frame dissolve between mc-clips present and placed 12/13?
   - Are both dip dissolves and the slug present?
   - Do lanes land on V2 to V4?
2. **Is the PNG `<video>` with a `duration="0s"` asset accepted?** If not, try the `<asset-clip>` form from the
   Montage fixture.
3. **Box geometry after import.** Read `GetProperty()` on the box items:
   - Pan ≈ ±480, Tilt 0, ZoomX 0.45? If so, FCPXML transforms are numerically faithful.
   - Any Crop* ≠ 0? That shows whether Resolve imports trim-rect, and in which units.
4. **Width-axis unit of `trim-rect`.** Ideally export one FCP project with a 10 % left trim and read the value. Or
   compare Resolve's imported CropLeft for `left="17.7778"` and `left="10"`.
5. **SetProperty crop units.** Set `CropLeft=192` on a UHD clip in a 1080 timeline with `Scaling=Fit`: does it crop
   10 % of the picture width or 5 %?
6. **Multicam imported as compound** (RESEARCH §2.4): are the connected stills and boxes still on V2 to V4 of the
   main timeline?
7. **Variant B dip:** does a bare `<transition name="Dip to Color Dissolve">` import as Resolve's Dip to Color
   Dissolve?
8. **Opacity keyframes** on a lane still (`adjust-blend` + `keyframeAnimation`) for option 1.6a.

---

## 10. Sources

- **Apple FCPXML DTD 1.10** (CommandPost mirror):
  https://raw.githubusercontent.com/CommandPost/CommandPost/develop/src/extensions/cp/apple/fcpxml/dtd/FCPXMLv1_10.dtd
  — `transition`, `spine`, `mc-clip`, `mc-source`, `video`, `adjust-crop`/`trim-rect` ("percentage of original
  frame height"), `adjust-conform` ("absence implies fit"), `adjust-transform` defaults.
- **Apple FCPXML Reference pages:**
  - https://developer.apple.com/documentation/professional-video-applications/transition
  - …/adjust-transform (position % of frame height, +up)
  - …/adjust-crop (modes trim/crop/pan, % of frame height)
  - …/adjust-conform (default fit)
  - …/effect (uid forms)
  - …/filter-video (mc-source for multicam filters)
  - …/story-elements (lanes, compositing order)
- **Real FCP exports:**
  - OTIO FCPX adapter sample:
    https://github.com/OpenTimelineIO/otio-fcpx-xml-adapter/blob/main/tests/sample_data/fcpx_example.fcpxml
    (centred transition, B `start` handle, Cross Dissolve uid).
  - swift-fcpxml test exports:
    https://github.com/orchetect/swift-fcpxml/tree/main/Tests/SwiftFCPXMLTests/TestResource/FCPXML%20Exports
    (`TransitionMarkers1.fcpxml`, `Occlusion3.fcpxml`: `adjust-transform position="0 22.5729"`,
    `adjust-conform type="fill"`, `FFVideoFormatRateUndefined`).
- **auto-editor FCPXML writer:** https://github.com/WyattBlue/auto-editor/blob/master/src/exports/fcp11.nim
  ("Resolve matches the transition by name"; transition overlaps the edit, neighbours keep their extents).
- **cutlass** (Go FCPXML generator; image rules): https://github.com/andrewarrow/cutlass/blob/main/go/CLAUDE.md and
  `go/fcp/asset_builder.go`.
- **Montage** (FCPXML ⇄ Resolve interchange):
  - https://github.com/AtelierSuite/Montage/blob/main/MontageFormats/FCPXML/FCPXMLImporter.swift
    (position/scale/conform model);
  - `MontageFormats/Resolve/ResolveClipSettings.swift` (Resolve Position/Zoom/Crop storage semantics);
  - `MontageTests/Formats/Fixtures/Resolve.fcpxml` (Resolve-shaped export, PNG asset).
- **DaVinci Resolve 20 Reference Manual**, ch. 55 "Preparing Timelines for Import and Comparison", pp. 1134–1138:
  - effects table;
  - supported transitions;
  - opacity/position/scale/rotation;
  - Ken Burns → Dynamic Zoom;
  - still image formats.

  https://documents.blackmagicdesign.com/UserManuals/DaVinci_Resolve_20_Reference_Manual.pdf
- **Resolve release notes** (`https://www.blackmagicdesign.com/support/readme/<id>`, IDs via downloads.json, see
  RESEARCH §14):
  - 12.5.3: "FCPXML transforms for Multicam not getting imported correctly";
  - a later note: "importing a Final Cut Pro XML would not preserve audio transitions".
- **Resolve scripting README 21.0.4** ("Looking up Timeline item properties"):
  https://gist.github.com/X-Raym/2f2bf453fc481b9cca624d7ca0e19de8
- **Not reachable from here:**
  - Blackmagic forum (scripted clients get empty pages);
  - grep.app (HTTP 429);
  - GitHub code search (needs auth).

  General search engines returned nothing relevant for the precise terms. Forum experience with FCPXML
  transforms in Resolve therefore remains a gap.
