# Research Gaps & Open Questions

What could and could not be verified while building the Hylocropter dashboard, and what
would close each remaining gap.

Sections 1, 2, 3 and 8 are **resolved** — network access was opened up mid-build, so the
practitioner sources have now been read and the basemap is downloaded. §2 in particular
changed the story materially: `k = 0.8` turned out to be a real Public Lab value for the
*wrong filter*, which is worse than being unsourced, and the code comments and docs have
been corrected.

What's left needs either the rig in your hands or a decision about the paper.

Each item is tagged with how much it matters:

- 🔴 **Blocks correctness** — the numbers may be wrong until this is resolved
- 🟠 **Affects quality** — the system works, but could be meaningfully better
- 🟡 **Documentation only** — needed for the paper/defense, not for the code
- ✅ **Resolved** — kept for the record, and because some of it belongs in the writeup

---

## 1. ✅ RESOLVED — the sources are readable now

Network access was opened up, so the sources below have been read and §2, §3 and §8 are
settled. Kept for the record, plus one practical note.

**Public Lab now serves a client-side static archive** (`publiclab/publiclab-archive`), so
the pages are an empty SPA shell to anything but a real browser. Append `.md` to get the
raw text — e.g.
`https://publiclab.org/notes/nedhorning/10-21-2013/calibrating-diy-nir-cameras-part-1.md`.
Useful if you're citing them and want to quote exactly.

The sources that mattered:

| Source | Why it matters |
|---|---|
| `publiclab.org` — Ned Horning's "Calibrating DIY NIR cameras" | the origin of the NIR-leakage correction method and the `k` value we use |
| `publiclab.org` — "Raspberry NoIR cam + blue filter" | the closest thing to a reference implementation of this exact rig |
| `us.rosco.com` | the official Rosco #2007 spectral transmission curve |
| `www.raspberrypi.com` — "What's that blue thing doing here?" | the official guidance on the bundled filter |
| `pip.raspberrypi.com` | the picamera2 manual PDF (the authoritative control reference) |
| `arxiv.org`, `mdpi.com`, `link.springer.com` | the academic literature on single-sensor NDVI |
| every satellite tile server (Esri, OSM, Carto, Mapbox, Bing) | offline basemap imagery |

All read. What they changed is in §2 (the `k` story), §3 (the spectral curve) and §5 (the
thresholds now have a source, even if a generic one).

---

## 2. ✅ RESOLVED — `k = 0.8` is real, but it belongs to a different filter

**Status: settled.** The practitioner sources are readable now and I've traced this to
the bottom. The short version: the *formula* in this repo is genuinely Ned Horning's, and
`0.8` is genuinely his default — but it is a value for the **opposite kind of filter**,
and using it here is wrong in a way his own writing predicts.

**What checks out.** `vis = B − k·R` is Horning's, from Public Lab's
[PhotoMonitoringPlugin](https://publiclab.org/notes/nedhorning/07-22-2015/introducing-the-calibration-plugin-for-imagej-fiji)
(2015): *"subtract a percentage of the NIR pixel values from the visible pixel values.
This is useful since the visible channel records both visible and NIR light."* The
[source](https://github.com/nedhorning/PhotoMonitoringPlugin) does exactly what we do:

```java
visPixel = visImage.getProcessor().getPixelValue(x, y) - (percentToSubtract * nirPixel);
```

And `0.8` is that plugin's hard-coded default — `percentToSubtract = 80.0`, subtraction
on by default. So the old attribution wasn't invented.

**What doesn't.** Horning's 80% is for a **MidOpt DB660/850 narrowband red/NIR filter**,
with the channels the other way round (blue ≈ NIR, red = red+NIR). His justification is
filter-specific:

> "this DB660/850 filter gets around that by centering the NIR band at 850nm where the
> sensitivity of the red detectors is roughly the same as the blue detectors"

That condition does **not** hold for a Rosco #2007, which passes NIR broadly from ~695 nm.
Horning says so himself, in the same paragraph:

> "The one advantage of blue filters over very broad band red filters is that the red
> detectors in the camera sensor are much more sensitive to the shorter NIR wavelengths"

So for this rig `k` should be **well below 0.8**. The `SYNTH_LEAK = 0.35` used by dev mode
is a more plausible neighbourhood.

**Where the bogus attribution probably came from.** On the canonical Pi NoIR + Rosco page,
[What's that blue thing doing here?](https://www.raspberrypi.com/news/whats-that-blue-thing-doing-here/),
Chris Fastie writes that with this rig you get *"healthy plants between 0.2 and 0.8"* —
an **NDVI output ceiling**, not a leakage coefficient. Two unrelated 0.8s appear to have
been merged.

**Also corrected:**
- *"Practitioner range is 0.3–0.8"* — **unsourced.** Not in any source; the plugin accepts
  0–100 with no guidance. Removed from the docs.
- *Horning's "part 1" is the origin of the method* — **wrong.** The 2013 three-part series
  is about reference-target regression and never mentions leakage subtraction. The method
  arrives in 2015.
- *The `carolccarvalho` note supports the method* — **no.** It's a student asking for help,
  with no `k` and no correction. Its value is confirming that #2007 ships with the NoIR and
  that `(R−B)/(R+B)` is the intended index.

### The white-card solver: keep it, but know its biases

Still the right thing to do — and now the docs say what it is rather than overselling it.
Three caveats worth carrying into the writeup:

1. **"White reflects equally in NIR and visible" is approximate.** Horning's measured
   printer paper is 0.867 at 660 nm vs 0.900 at 850 nm — a true NDVI of about **+0.02**,
   not 0. Forcing 0 biases `k` slightly high.
2. **Optical brighteners make it worse here.** Office paper is engineered to reflect more
   *blue*, which is the band we compare against. Use a grey card without brighteners.
   Horning's own advice: *"It's best if the reflectance samples have a fairly flat spectral
   curve."*
3. **One target can't separate gain from offset.** This `k` absorbs per-channel gain
   asymmetry along with physical leakage. It's a rig-and-settings fudge factor that makes
   your numbers self-consistent — not a physical responsivity ratio. Don't call it one.
4. **A passing white-card calibration does not prove the channels are clean.** This is the
   nastiest one, and it comes from §4: the camera's colour correction matrix is
   white-preserving, so it reads ≈0 on a neutral card whether or not it is corrupting your
   vegetation pixels. Confirm **ISP neutralised: yes** on the capture detail page before
   trusting a solved `k`.

**What Public Lab actually prescribes**, if you want defensible radiometry rather than a
working demo: a linear regression of reflectance on pixel value against **at least a bright
and a dark characterised target**. Put both in frame, look up their reflectance at your
passband centres, solve a per-channel gain and offset. Horning's own summary: *"Applying
the regression coefficients is just applying a gain and offset to each image."* His
procedure in full is in
[part 1](https://publiclab.org/notes/nedhorning/10-21-2013/calibrating-diy-nir-cameras-part-1)
— note it needs the *2014 November 25* Fiji life-line build; current Fiji is incompatible
with the plugin.

Chris Fastie, on single-target shortcuts like ours:
[*"This is a very subjective method and has probably never been tried…"*](https://publiclab.org/notes/cfastie/05-01-2016/calibration-cogitation)
Fair. It's within Public Lab practice, and explicitly not calibration.

---

## 3. ✅ RESOLVED — Rosco #2007 spectral transmission

**First: yes, the gel in the Pi NoIR box is this filter.** Raspberry Pi's
[announcement](https://www.raspberrypi.com/news/whats-that-blue-thing-doing-here/)
(29 Oct 2013): *"There's a little square of blue gel in there. What's it for? …
Our friend **Roscolux #2007 Storaro Blue** (that's the blue thing's full name)
turns out to be a great example — we buy it on giant reels and the guys at the
factory in Wales where we make the Raspberry Pi and both kinds of camera board cut
it up into little squares for you to use."* Chosen on Public Lab's research. So
"the blue thing in the box" and "Rosco #2007" are the same object.

Got the official data sheet:
[us.rosco.com/en/products/filters/r2007-storaro-blue](https://us.rosco.com/en/products/filters/r2007-storaro-blue),
chart at [2007.jpg](https://us.rosco.com/sites/default/files/content/filters//cinegel/2007.jpg).
Worth a footnote for the paper: **Rosco's own sheet calls it Cinegel, not Roscolux**, even
though everyone says "Roscolux #2007".

Header data: *#2007 VS BLUE*, "Color Effects Lighting Filter. Deep Reddish-Blue",
**transmission 10% / −3.3 stop loss**, deep-dyed polyester (PET) film, 2.0 mil (50 µm),
made in USA. No mired shift.

Published transmission, on the sheet's own 20 nm grid:

| nm | 360 | 380 | 400 | 420 | 440 | 460 | 480 | 500 | 520 | 540 |
|---|---|---|---|---|---|---|---|---|---|---|
| **T%** | 23 | 31 | 40 | 53 | 53 | 41 | 27 | 18 | 11 | 10 |

| nm | 560 | 580 | 600 | 620 | 640 | 660 | 680 | 700 | 720 | 740 |
|---|---|---|---|---|---|---|---|---|---|---|
| **T%** | 8 | 7 | 5 | 3 | 2 | 2 | 4 | 15 | 42 | 67 |

Shape: 23% at 360 nm rising to a broad blue maximum of **53–55% across 420–440 nm**,
falling monotonically through green to a deep red minimum of **2% at 640–660 nm**, then
climbing steeply into the NIR — **4% at 680, 15% at 700, 42% at 720, 67% at 740**, still
rising where the chart ends. Derived edges: NIR crosses 10% at ≈695 nm and 50% at ≈725 nm.

**Two findings that changed the code's comments:**

1. **Green is not blocked.** 8–18% between 500 and 560 nm, against a 53% blue peak — about
   a fifth of peak blue. Calling it "blocked" was too strong; the docs now say "mostly
   blocked (8–18%)". This is exactly the leakage Horning tried to absorb with a
   green-as-second-predictor multiple regression, and judged inconclusive.
2. **The sheet stops at 740 nm**, so it does *not* characterise the 750–900 nm range where
   the silicon collects most of its NIR. **The data sheet therefore cannot be used to
   derive `k`** — which independently justifies measuring it, as §2 concludes.

Best citation for the channel physics is now Fastie on the Raspberry Pi blog, which is
clearer than either Public Lab note:

> "The Rosco filter blocks most red light but passes the blue end of the spectrum and also
> near infrared light. The blue channel will now capture visible blue light as usual (plus
> some infrared) but there is no red light for the red channel to capture so it captures
> mostly near infrared light."

---

## 4. 🔴 The camera's image pipeline corrupts the index — partly fixed, one part can't be

This turned out to be the most serious finding of the whole review, and it is worse than
the two bugs I originally listed. Locking AWB and AE is **not enough**. Several ISP stages
keep running, are non-linear or per-channel, and one of them mixes the *green* channel —
the one this rig treats as blocked — into both red and blue.

### The colour correction matrix is the real problem

`ColourCorrectionMatrix` is *"the 3x3 matrix that converts camera RGB to sRGB … after
pixels have been white-balanced, but before any gamma transformation"*. And per libcamera's
own docs:

> If ColourTemperature is set (either directly, **or indirectly by setting ColourGains**)
> but ColourCorrectionMatrix is not, the ColourCorrectionMatrix is updated based on the
> ColourTemperature.

So our `ColourGains: (1.0, 1.0)` — the thing that locks white balance — *causes* a CCM to
be applied. Traced through `imx219.json`, gains of 1.0 resolve to a colour temperature of
5536 K, which interpolates to:

```
[  2.2861  -0.8375  -0.4485 ]
[ -0.6552   2.6316  -0.9764 ]
[ -0.2764  -0.5887   1.8651 ]
```

Look at the green column: **−0.84 into red and −0.59 into blue.** "R" and "B" are no longer
NIR and visible blue; they're mixtures containing the channel we claim is unused. Modelling
vegetation behind the gel through that matrix plus gamma 2.2:

| true BNDVI | measured | class shift |
|---|---|---|
| +0.55 | +0.426 | healthy → healthy |
| **+0.40** | **+0.294** | **healthy → moderate** |
| **+0.35** | **+0.263** | **healthy → moderate** |
| **+0.32** | **+0.246** | **healthy → moderate** |
| +0.20 | +0.167 | moderate → moderate |
| +0.10 | +0.098 | stressed → stressed |

A misclassification band sitting right on the 0.3 boundary — and the error isn't a constant
offset. Holding R and B fixed and varying green leak from 0 to 0.20 moves the measured value
from 0.248 to 0.310.

**And your own calibration cannot detect it.** The CCM's rows sum to 1.0 by construction, so
it is white-preserving: a neutral card reads BNDVI = +0.00002 through it. `solve_leak_coef()`
and the drag-a-box gesture will pass cleanly while vegetation readings are wrong. **A passing
white-card calibration is not evidence the channels are clean.**

### Three more stages that controls don't reach

- **`rpi.contrast`** — `imx219.json` sets `ce_enable: 1`, so an adaptive tone curve is
  restretched every frame from that frame's luminance histogram, on top of a 66-point gamma
  curve. `Contrast: 1.0` only skips the *manual* contrast path. There is no control for it.
- **`rpi.alsc`** — lens shading applies spatially varying, **per-channel** Cr/Cb gains
  (16×12 tables, iterated). So BNDVI drifts across the frame independently of vegetation.
  Worse: the shipped tables were calibrated for a stock IMX219 **with** its IR-cut filter,
  which this rig doesn't have. `imx219_noir.json` doesn't help — it still ships full
  `calibrations_Cr`/`calibrations_Cb` and `rpi.ccm`. No control disables it.
- **`NoiseReductionMode`** — we never set it, and picamera2 injects *different* defaults for
  stills (`HighQuality`) and previews (`Minimal`). So the saved capture and the live debug
  feed were running different ISP configurations, which quietly undercut the "all seven
  canvases come from one array so they can't drift" design property.

### What was fixed

`locked_controls()` now sets, filtered against `cam.camera_controls` so old stacks don't
choke:

```python
"ExposureTimeMode": 1,   # Manual — see below, AeEnable is not enough
"AnalogueGainMode": 1,   # Manual
"NoiseReductionMode": 0, # Off — also fixes the still/preview mismatch
"ColourCorrectionMatrix": (1,0,0, 0,1,0, 0,0,1),   # identity
```

Plus `neutral_tuning()`, on by default (**Settings → "Neutralise the image-processing
pipeline"**), which loads `imx219_noir.json` as a dict via `Picamera2(tuning=...)` and
neuters `rpi.ccm` (identity), `rpi.contrast` (`ce_enable: 0`, linear gamma), `rpi.alsc`
(flat), `rpi.sharpen` and `rpi.sdn`. No root, no installed files.

Plus `verify_controls()`, which compares capture metadata against what was requested. Every
capture records a `control_check`, mismatches are shown at the top of the photo detail page,
and the debug feed checks every frame and overrides the sanity note if a lock is being
ignored. **This is the fix that would have caught the AeEnable bug automatically** — and it
matters because `AeEnable` is deliberately absent from metadata, so you have to check the
*values*, not the flag.

### The AeEnable bug, properly diagnosed

[picamera2 #1269](https://github.com/raspberrypi/picamera2/issues/1269) — open, **zero
maintainer replies**. libcamera 0.5.0 redefined `AeEnable` as a wrapper pre-processed into
the two `*Mode` controls, but only on the `queueRequest()` path — not `start()`. David
Plowman's fix commit:

> "In Camera::queueRequest() the control list is updated transparently by converting
> AeEnable into ExposureTimeMode and AnalogueGainMode controls. However, this was not
> happening during Camera::start(), meaning that setting AeEnable there was having no
> effect."

`Camera::start()` is exactly the path `create_*_configuration(controls=...)` uses. Affected:
libcamera **0.5.0 and 0.5.1**; fixed from `0.5.1+rpt20250707`. Bookworm currently ships
0.5.2+rpt20250903, so **you are past it** — but the widely-cited maintainer advice that
*"setting ExposureTime and AnalogueGain will automatically disable the auto AGC/AEC"* is now
**stale**. Current libcamera: *"This control will only take effect if ExposureTimeMode is
Manual. If this control is set when ExposureTimeMode is Auto, the value will be ignored and
will not be retained."* The old dict was safe only by accident, via an undocumented
picamera2 shim. Now it's explicit.

[#825](https://github.com/raspberrypi/picamera2/issues/825) — open since 2023, also zero
maintainer replies, no documented workaround. The diagnosis is the CCM/contrast/ALSC
mechanism above: the reporter's frame-to-frame colour cast with identical metadata is
exactly what adaptive stages that don't appear in metadata look like.

### What is still open 🔴

**On Bookworm you cannot set the CCM via controls** — it's read-only until libcamera 0.6.0
(Trixie). I bisected it:

| libcamera | CCM settable | ships with |
|---|---|---|
| 0.5.0–0.5.2 | **no** | Bookworm |
| 0.6.0–0.7.1 | yes | Trixie |

So on a Pi 4 running Bookworm the identity-CCM control is silently dropped and you are
relying entirely on the tuning override. That should work — but **none of it has been run
against a camera**, so verify it on the bench:

1. Take a capture, open its detail page, confirm **ISP neutralised: yes** and no control
   mismatch warning.
2. Do the shade test from CALIBRATION.md step 6. If the BNDVI mean moves when only the
   illumination changes, something adaptive is still running.
3. Ideally: photograph a colour target and check that a green patch doesn't move the R/B
   ratio. That's the direct test for CCM contamination, and the one thing a white card can't
   tell you.

**The complete fix is to compute the index from the raw Bayer frame**, bypassing the ISP
entirely. `capture_raw_dng()` already grabs it and saves the DNG, but `compute_bndvi()` still
consumes the processed RGB. Doing it properly means subsampling the CFA for R and B pixels
and subtracting black level (`imx219.json` has `rpi.black_level: 4096` on a 16-bit scale =
64 in 10-bit; skipping it compresses the index toward zero). **I did not implement this** —
writing an untested raw imaging pipeline and making it the default is a worse risk than the
ISP contamination it fixes, and it needs a camera to validate. It is the right next step if
you want defensible radiometry.

### Smaller items, fixed

- `create_still_configuration` defaults to `queue=True`, so `capture_array()` could return a
  frame that completed *during warmup* — before the locked values took. Now `queue=False`
  with `capture_request()`, which also gives us the metadata for verification.
- `_capture_legacy()` set `shutter_speed` after `exposure_mode="off"` and won't run on
  Bookworm at all. Left as a dead fallback, but it would produce unlocked frames if it ever
  ran — don't rely on it.

### Adjacent issues worth watching

All open, none with visible maintainer replies:
[#859](https://github.com/raspberrypi/picamera2/issues/859) (request for linear 12/16-bit
ISP output; stated workaround is raw Bayer),
[#1316](https://github.com/raspberrypi/picamera2/issues/1316) (OpenFlexure, pipeline order —
answered above: CCM is *before* gamma),
[#1103](https://github.com/raspberrypi/picamera2/issues/1103) (tuning file support not
thread-safe — relevant, we now use a tuning dict),
[#1341](https://github.com/raspberrypi/picamera2/issues/1341) (lens shading table
intermittently not loaded),
[#908](https://github.com/raspberrypi/picamera2/issues/908) (IMX219 auto-exposure never
settles — moot once genuinely locked).

There are no NDVI-specific issues or discussions in the repo at all.

---

## 5. 🔴 BNDVI thresholds have no empirical basis for dragon fruit

The code classifies `> 0.3` healthy, `0.1–0.3` moderate, `< 0.1` stressed. Your thesis
defines the three **classes** (green / "latent stress" / red) but **never states a single
numeric threshold**. The only numbers anywhere are Table 1's five illustrative sample
rows — which are labelled as samples, not measurements.

The defaults now at least have a source, which they didn't before. Chris Fastie, on the
Public Lab note for **this exact rig**, gives
[*"healthy plants with NDVI values 0.3 to 0.7, and non plants with NDVI below 0.2"*](https://publiclab.org/notes/carolccarvalho/07-15-2016/raspberry-noir-cam-blue-filter),
and *"healthy plants between 0.2 and 0.8"* on the
[Raspberry Pi blog](https://www.raspberrypi.com/news/whats-that-blue-thing-doing-here/).
Table 1 in your paper is roughly consistent too (its "healthy vegetation" rows are 0.30
and 0.38; "slightly stressed" 0.03; "water stress" −0.33).

So `0.3 / 0.1` is defensible as a starting point rather than pulled from nowhere. **It is
still generic vegetation, not dragon fruit.** *Hylocereus* is a cactus with thick waxy
cladodes; its NIR/blue reflectance will not behave like the leafy crops those numbers come
from, and nobody in these sources was looking at one.

**To close — this is your Objective 4 validation, and it's the highest-value fieldwork:**

The dashboard now makes this straightforward. Every capture stores its full float32 BNDVI
array, and thresholds are editable live with instant recolouring.

1. Capture a set of plants you have **visually classified on the ground** — clearly
   healthy, clearly stem-cankered, and some in between. Note which is which.
2. Read each one's mean BNDVI from the capture detail page.
3. Pick the thresholds that best separate your own healthy and stressed groups, and set
   them in **Settings**. Drag the sliders in Debug to see the boundary move over a real
   frame.
4. Write the derived numbers into the paper with the sample size. *That* is a result, and
   right now the paper has none.

Note that healthy BNDVI on an uncorrected Infrablue rig runs much lower than true NDVI —
Table 1's healthy values of 0.30–0.38 are consistent with that, so don't expect the
+0.7–0.9 you'd see from a proper red/NIR sensor.

---

## 6. 🟡 IMX219 spectral response / quantum efficiency — still open, and probably fine

Sony does not publish QE curves for the IMX219, and with open network access I still found
nothing sensor-specific. Without it, the split of NIR between red and blue Bayer pixels
can't be derived from first principles — the other half of why `k` has to be measured.

One directional finding, from patent literature on NIR-sensitive imagers rather than this
sensor: around 850 nm a Bayer colour filter array becomes largely transparent, so all
pixels land in the same ballpark (~25% QE). That is *consistent* with meaningful leakage
into blue, but it is not IMX219 data and it says nothing about 700–750 nm, which is where
the Rosco #2007's passband actually opens (§3). Don't cite it as a number.

**Verdict: leave this open.** It's the cleanest possible illustration of why the empirical
route wins — between an uncharacterised sensor response and a filter curve that stops at
740 nm, `k` is simply not derivable for this rig, and measuring it is the answer rather
than a compromise.

---

## 7. 🔴 The MAVLink integration is written but has never been run

You chose real pymavlink over a simulated interface, which I've implemented — but there
was no flight controller and no SITL in this environment, so **not one line of the
telemetry path has executed against a real MAVLink stream.**

What I *did* verify: the dashboard boots, serves every page, and runs the debug view with
no flight controller attached, showing the "Drone not connected" state correctly. Nothing
hangs or crashes. So the failure mode is safe.

What is unverified: message field names and scaling, the mission read-back, arm/disarm
flight detection, and capture-on-`CAMERA_TRIGGER`.

**To close, cheapest path first:**

1. **ArduPilot SITL over UDP** — no hardware needed at all. Run SITL on a laptop, set the
   connection string in **Settings** to `udp:127.0.0.1:14550`, and the whole telemetry
   path becomes testable at a desk. Do this before touching the drone.
2. Then the real Pixhawk on `/dev/ttyAMA0` at 57600.
3. Then a trigger test: set `CAM_TRIGG_DIST` and confirm captures fire with GPS attached.

Budget one debugging session for this. Field names and integer scaling (lat/lon are
1e7-scaled, altitudes are mm) are the usual culprits.

---

## 8. ✅ RESOLVED — the offline map is downloaded and committed

Done through the dashboard's own downloader: **138 tiles, 1.9 MB, zoom 16–19**, centred on
14.1265 N / 121.0768 E, detail to **29 cm per pixel** at zoom 19. Committed, so the Pi
never needs the network for the basemap.

One thing the real imagery taught us: tiles sit on a fixed grid, so a 620 m box snaps
outward to whole tiles and you actually get **54.9 ha**, not the 38.4 ha requested.
Settings now reports both numbers, and the farm map draws the true coverage edge.

To move or resize it — e.g. once you've read the real plot coordinates in the field —
change the centre in **Settings → Offline map** and download again. Delete
`hylocropter/static/tiles/` first if you want to drop the old area rather than accumulate.

Also worth knowing: `DEPLOYMENT.md` originally suggested copying Mission Planner's
prefetched tiles. **Don't bother** — Mission Planner stores them in a proprietary
`gmapcache/TileDBv3` database, not a `{z}/{x}/{y}` tree. The built-in downloader is
simpler. I've corrected that doc.

---

## 9. 🟡 The farm's location is inconsistent between sources

- Your thesis says the site is **Brgy. Bilog-bilog, Tanauan City**.
- You told me the farm is near **Vis Compound, Brgy. Altura Bata, Tanauan City**.

These are different barangays, several kilometres apart. I defaulted the map centre to
Altura Bata (≈ **14.1265 N, 121.0768 E**, from PhilAtlas) because that's what you said,
and made it editable from the map.

I could not verify the Altura Bata coordinate independently — the geocoding services were
blocked, so it comes from a search result, and it's a *barangay centroid*, not your plot.

**To close:** stand in the field, read the coordinates off the dashboard (or any phone GPS),
and set the plot centre from **Settings**. Then reconcile the barangay name with the paper
before the defense — an examiner reading both will notice.

---

## 10. 🟡 Contradictions and omissions inside the thesis itself

Found while implementing. None are hard to fix in the paper, but they should be fixed.

**In-flight vs post-flight processing — the paper contradicts itself.**
Objective 2 and the Process Flow say the Pi "processes images natively **during flight**"
and that "this loop repeats until the flight concludes". The Hardware section says the
components are "for **post flight** image processing", and Scope says heatmaps are
retrieved "after processing" on landing.

I implemented **capture in flight, process after landing**, because a Pi 4 cannot
reliably run 8 MP BNDVI plus figure rendering per frame at a ~5 s cadence while also
servicing MAVLink — and because your own mockup has a dedicated post-landing "Processing"
view with a progress ring, so the design already assumes batch. Pick one story and make
Chapter 2 consistent.

**Other things the paper never specifies, which the code now has to decide:**

| Missing | What the code does |
|---|---|
| any camera setting — exposure, gain, AWB, resolution, format, focus | all are editable settings, defaults gain 2.0 / 5000 µs / AWB+AE locked, recorded per capture |
| any calibration procedure — no white reference, no exposure lock | `CALIBRATION.md`'s procedure, now built into the Debug view |
| flight parameters — altitude, GSD, image overlap, line spacing, speed | read live off the Pixhawk instead of hardcoded |
| capture trigger logic | all three modes from the mockup; Table 1 implies ~5 s interval |
| the green channel's role | unused, as in the paper; Debug labels it "mostly blocked" so it's visibly accounted for |
| storage formats / geotag encoding | JSON index + per-flight directories; float32 BNDVI saved as `.npz` |
| how the filter is physically mounted | only "applied to" the camera in the paper — `HARDWARE.md` is the sole record, cite it |
| software stack | only OpenCV and MAVLink are named in the paper; the implementation is Flask + NumPy + picamera2 + pymavlink, and does **not** use OpenCV |

That last row is worth a deliberate decision: **the paper names OpenCV as the image
processing library, and this implementation doesn't use it** — NumPy does everything the
pipeline needs, and adding OpenCV to a Pi 4 for channel arithmetic would be dead weight.
Either update the paper, or tell me and I'll reconcile the code to it.

**Also absent, and expected for a Design-and-Practice-1 proposal:** any results,
accuracy figures, processing-time measurements, or thermal/flight-time characterisation.
The paper lists these as things to be measured. The dashboard now records processing time
and CPU temperature per flight, so that data will accumulate on its own once you start
flying.

---

## Suggested order of attack

| # | Task | Where | Effort |
|---|---|---|---|
| 1 | Confirm the ISP neutralising actually works (§4) | Debug view + a capture, on the rig | 20 min |
| 2 | Verify the AWB/AE lock really holds — the shade test (§4) | Debug view, on the rig | 10 min |
| 3 | Measure your own `k` — expect well below 0.8 (§2) | Debug view, on the rig | 10 min |
| 4 | Test telemetry against ArduPilot SITL (§7) | laptop, no drone needed | 1 hour |
| 5 | Derive real thresholds from ground truth (§5) | field + dashboard | a field session |
| 6 | Set the plot centre from a real field reading (§9) | Settings | 5 min |
| 7 | Fix the in-flight/post-flight contradiction (§10) | the paper | 15 min |
| 8 | Reconcile the barangay name (§9) | the paper | 5 min |
| 9 | Decide the OpenCV question (§10) | the paper, or ask me | 10 min |

**Do 1, 2 and 3 in that order, in one sitting on the bench** — they're the same twenty
minutes with the camera, and until they pass every number the system produces is
provisional. Item 5 is what turns this from a working system into a result. Everything else
is polish or paperwork.

**Now available for the writeup**, courtesy of the resolved sections: the full Rosco #2007
transmission table (§3), the correct provenance and limits of the `B − k·R` correction
(§2), a real citation for the 0.3/0.1 thresholds (§5), and Public Lab's actual two-target
calibration procedure if an examiner pushes on radiometry (§2).
