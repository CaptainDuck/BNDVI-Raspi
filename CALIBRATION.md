# Calibration Guide

How to set up the Pi NoIR v2 + Rosco #2007 blue filter so the BNDVI numbers
actually mean something, and how to verify it.

Everything here is now doable from the **Debug** view in the dashboard. The old
version of this guide told you to edit constants in `bndvi.py` with a text editor
and inspect pixel values in a file browser; you don't need to any more.

## Why calibration matters

BNDVI is a ratio between two channels, so anything the camera does to each
channel *independently* corrupts it. Two automatic features have to be off.

### AWB (Auto White Balance)

The camera continuously adjusts the gain on each colour channel so "white things
look white". If the scene leans green, AWB boosts blue and red to compensate.
Great for snapshots, **catastrophic for BNDVI** — the ratio between Red and Blue
is exactly the quantity being measured. With AWB on, your BNDVI reflects how the
firmware feels about the scene, not what's there.

### AE (Auto Exposure)

The camera adjusts shutter and gain to keep the image "well exposed". Bad here
because:

1. Two captures of the same plant seconds apart can land at different exposures,
   making them non-comparable over time.
2. Auto-gain can clip or under-expose individual channels differently.

### What "locked" means

Both autos off, and shutter, gain **and colour gains** pinned to fixed numbers.
That last one matters: `AwbEnable: False` on its own just freezes whatever gains
the algorithm last chose, which is not repeatable between boots. Setting
`ColourGains` is what actually pins it.

## How the code does it

In `bndvi.py`, `locked_controls()`:

```python
{
    "AwbEnable":    False,      # no auto white balance
    "AeEnable":     False,      # no auto exposure/gain
    "ColourGains":  (1.0, 1.0), # pin AWB properly
    "AnalogueGain": 2.0,
    "ExposureTime": 5000,       # µs, ≈ 1/200 s
    "Sharpness":    0.0,        # sharpening is non-linear
    "Contrast":     1.0,
    "Saturation":   1.0,        # do not stretch channel ratios
}
```

These go into `create_still_configuration(controls=...)`, **not** a
`set_controls()` call after `start()`. The official picamera2 examples do it this
way, and it removes the race where the first frames come back before the locked
values have been applied.

Every tunable is a setting in the dashboard, stored in
`hylocropter_data/settings.json`, and **recorded onto every capture** — so an old
photo always tells you what it was taken with, even after you change your mind.

| Setting | Default | What it controls |
|---|---|---|
| Analogue gain | 2.0 | Sensor gain; raise for shade, lower for harsh sun |
| Exposure | 5000 µs | Shutter time; longer = brighter, more motion blur |
| Warm-up | 3 s | Time for the sensor to settle after the controls apply |
| Resolution | 3280 × 2464 | Full 8 MP; drop to 1280 × 960 for faster testing |
| Capture format | JPEG | Add raw DNG for calibration-grade work (see below) |

### Why JPEG isn't ideal

JPEG/RGB output is gamma-encoded, and gamma is a per-channel *non-linear* curve.
`(R−B)/(R+B)` computed on gamma-encoded 8-bit values is not the same number as on
linear sensor counts. For a course demo the difference doesn't change the
conclusions, but if you want defensible radiometry, switch **Settings → Capture
format** to "+ raw DNG" and work from the Bayer frame. Costs about 10 MB per
photo.

## Step by step

### 1. Fix your lighting and stick with it

Direct midday sun is easiest — broad, stable spectrum. Bright overcast works too.
Avoid mixing sun and shade in one frame.

### 2. Put a white reference in the scene

Plain white printer paper is fine for a course project. A grey card is better,
Spectralon is overkill. Put it in the same light as the plant you'll measure.

### 3. Open Debug and check the exposure

The **Live readout** and **sanity note** do the judging for you. What you want:

- The white card's brightest pixels around **180–230** in both Red and Blue —
  bright but not clipped. The Channel split panel shows each channel on its own,
  so you can see this directly.
- The note tells you if channels are clipping at 255 or the frame is nearly
  black, and what to do about it.

Adjust the **Exposure** and **Analogue gain** sliders. Both go to the camera, so
the preview updates on its next frame.

- Too dark → raise gain (3.0, then 4.0) or exposure (8000, then 12000)
- Blown out → lower them (gain 1.0, exposure 2000)

### 4. Solve the NIR-leakage coefficient

A white reference reflects about equally across NIR and visible, so a correctly
corrected rig reads **BNDVI ≈ 0** on it. That gives a direct solution rather than
trial and error:

```
BNDVI = 0  ⟹  R = B − k·R  ⟹  k = B/R − 1
```

In Debug, **drag a box over the white card** in the raw feed and press **Solve k
from the selection**. The dashboard computes it from the mean R and B inside your
box, turns the correction on, and tells you the value.

That number is *yours* — your gel, your sensor, your light. It is more useful
than the `k = 0.8` default this project quotes, which is real (it's the
hard-coded default in Public Lab's PhotoMonitoringPlugin) but belongs to a
*different filter*: a MidOpt DB660/850 narrowband red filter, channels reversed,
justified by red and blue pixels having similar NIR sensitivity at 850 nm. The
Rosco #2007 passes NIR broadly from ~695 nm, where red pixels are much more
sensitive than blue — so expect your `k` to come out well below 0.8.

If the solver says no positive `k` can work, that means blue is already *below*
NIR on white — the red channel is over-responding. Lower the exposure or gain and
try again.

**Three honest caveats**, because this is a shortcut, not a calibration:

1. "White reflects equally in NIR and visible" is approximate. Horning's measured
   printer paper is 0.867 at 660 nm vs 0.900 at 850 nm — a true NDVI of about
   +0.02, not 0.
2. Office paper contains optical brighteners that specifically lift *blue*
   reflectance, which is the band we compare against. That inflates B and pushes
   the solved `k` higher still. A grey card without brighteners is better.
3. With one target you cannot separate gain from offset, so this `k` absorbs
   per-channel gain asymmetry along with the physical leakage. It makes your own
   numbers self-consistent; it is not a physical responsivity ratio.

**What Public Lab actually prescribes** is a linear regression of reflectance on
pixel value using at least a bright *and* a dark characterised target — put both
in frame, look up their reflectance at your filter's passband centres, and solve
a per-channel gain and offset. That's the defensible route if an examiner pushes
on radiometry; the drag-a-box solver is the one that gets you flying today.
See [RESEARCH-GAPS.md](./RESEARCH-GAPS.md) §2.

### 5. Check a real plant

Point at known-healthy vegetation. The raw feed should look **pinkish/magenta**,
and mean BNDVI should land in **+0.3 to +0.7**.

A negative value means the channel mapping got reversed somewhere — that would
mean someone copied logic from `ndvi_capture.old.py`, which has the bug.

### 6. Verify the lock is real

This is the check that catches the failure mode with no visible symptom. Point at
a fixed scene, then shade the subject with your hand or wait for a cloud.

- **Correct:** the histogram shifts bodily brighter or darker, and the **BNDVI
  mean barely moves**. The scene got darker; the *ratio* didn't change.
- **Broken:** the raw brightness stays pinned. The camera is still auto-adjusting
  and every number is meaningless.

The dashboard also checks this for you continuously. Every preview frame and every
capture compares what the camera *reports* doing against what was requested; a
mismatch replaces the sanity note in Debug and appears at the top of the photo
detail page. That check exists because there are open picamera2 bugs where the
lock is accepted and then silently ignored — see
[RESEARCH-GAPS.md](./RESEARCH-GAPS.md) §4.

### 6b. Confirm the image pipeline is neutralised

Locking exposure and white balance is **not sufficient**, and this is the step most
easily skipped. The camera still applies a colour-correction matrix which, at
locked colour gains, mixes the **green** channel into both red and blue with
weights around −0.8 and −0.6. Green is the channel this rig treats as blocked. The
result is a misclassification band sitting right on the 0.3 healthy boundary.

**A white card cannot detect this** — the matrix is white-preserving, so it reads
zero either way while vegetation readings are wrong.

So: leave **Settings → "Neutralise the image-processing pipeline"** on, and confirm
each capture's detail page says **ISP neutralised: yes**. If you have a colour
target, the direct test is to check that a green patch doesn't shift the R/B ratio.

Note this path has not been run against a real camera. Full detail, including why
Bookworm can't fix the matrix through controls and needs the tuning override, is in
[RESEARCH-GAPS.md](./RESEARCH-GAPS.md) §4.

### 7. Then stop changing things

Don't change gain or exposure between captures you want to compare, or the trend
across flights is noise. Each capture records its own settings, so the dashboard
can tell you what a photo was taken with, but it can't retroactively make two
different exposures comparable.

## Thresholds

The three health bands default to `> 0.3` healthy, `0.1–0.3` moderate, `< 0.1`
stressed. Those line up with what Public Lab practitioners report for this rig —
Chris Fastie gives "healthy plants with NDVI values 0.3 to 0.7, and non plants
with NDVI below 0.2" for a Pi NoIR + Rosco #2007, and "healthy plants between 0.2
and 0.8" on the Raspberry Pi blog. **But they are still generic vegetation
values, not derived for dragon fruit.**
*Hylocereus* is a cactus with thick waxy cladodes; its reflectance won't behave
like the leafy crops those numbers come from.

Deriving real ones is your Objective 4 validation, and it's the single most
valuable thing left to do:

1. Photograph plants you have **visually classified on the ground** — clearly
   healthy, clearly stem-cankered, and some in between.
2. Read each mean BNDVI off its detail page.
3. Pick the thresholds that best separate your groups. Drag the sliders in Debug
   to watch the boundary move over a real frame.
4. Set them in **Settings** and write the numbers into the paper with the sample
   size.

Photos keep the thresholds they were taken with, so changing these never rewrites
history. See [RESEARCH-GAPS.md](./RESEARCH-GAPS.md) §5.

## Common gotchas

- **Indoor LED light** has almost no NIR. Plants indoors won't show useful BNDVI
  even with a perfect rig. Calibrate outdoors.
- **Through glass** — most window glass blocks NIR. Don't shoot through it.
- **The warm-up matters.** Below ~3 s the sensor's analogue chain may not have
  settled, so the locked exposure isn't really applied to the frame.
- **Time of day changes the solar spectrum** (more red near sunset). For
  day-over-day comparisons, capture at roughly the same hour. The thesis's own
  sample timestamps are all ~10:00 AM.
- **Motion blur on the drone.** 5000 µs is fine hovering; at speed, shorten the
  exposure and raise the gain rather than accepting smear.

## Quick checklist

- [ ] AWB off — white paper reads white-ish, not strongly tinted
- [ ] AE off — shading the scene moves brightness but not the BNDVI mean
- [ ] No mixed sun/shade in the frame
- [ ] Daylight, not LEDs
- [ ] No glass between camera and subject
- [ ] White card peaks at 180–230 in both R and B
- [ ] White card reads BNDVI ≈ 0 after solving `k`
- [ ] Healthy plant looks pink and reads +0.3 to +0.7

## References

- [Ned Horning — Introducing the calibration plugin for ImageJ/Fiji](https://publiclab.org/notes/nedhorning/07-22-2015/introducing-the-calibration-plugin-for-imagej-fiji)
  — where the `vis − k·NIR` subtraction actually comes from, and where `k = 0.8`
  is the default
- [Ned Horning — Automating NDVI calibration](https://publiclab.org/notes/nedhorning/06-30-2015/automating-ndvi-calibration)
  — his 80% figure, and why it is specific to a DB660/850 red filter
- [Ned Horning — Calibrating DIY NIR cameras, part 1](https://publiclab.org/notes/nedhorning/10-21-2013/calibrating-diy-nir-cameras-part-1)
  — the reference-target regression procedure (note: no leakage subtraction here)
- [nedhorning/PhotoMonitoringPlugin](https://github.com/nedhorning/PhotoMonitoringPlugin)
  — the source, if you want to read the maths
- [Chris Fastie — Calibration cogitation](https://publiclab.org/notes/cfastie/05-01-2016/calibration-cogitation)
- [What's that blue thing doing here? — Raspberry Pi blog](https://www.raspberrypi.com/news/whats-that-blue-thing-doing-here/)
  — the canonical Pi NoIR + Rosco #2007 page, and Fastie's channel explanation
- [Rosco #2007 Storaro Blue — official filter data](https://us.rosco.com/en/products/filters/r2007-storaro-blue)
- [picamera2 examples — fixed exposure, raw capture](https://github.com/raspberrypi/picamera2/tree/main/examples)
