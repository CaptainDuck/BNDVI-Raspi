# Calibration Guide

How to set up the Pi NoIR v2 + Rosco #2007 blue filter so the BNDVI
numbers are actually meaningful, and how to verify it.

## Why calibration matters

BNDVI is a ratio of channel intensities, so anything the camera does to
each channel independently corrupts the index. The two automatic
features we have to turn off are **AWB** and **AE**.

### AWB (Auto White Balance)

The camera continuously tweaks the gain on each colour channel
independently so "white things look white." If the scene leans green
(lots of vegetation), AWB *boosts* blue and red to compensate. This is
great for snapshots but **catastrophic for BNDVI** — the absolute ratio
between Red and Blue is exactly the quantity we're measuring. With AWB
on, your BNDVI number reflects how the firmware feels about the scene,
not what's actually there.

### AE (Auto Exposure)

The camera adjusts shutter time and gain to keep the image "well exposed"
on average. Bad for BNDVI because:

1. Two captures of the same plant seconds apart can end up at different
   exposures, making the numbers non-comparable across time.
2. Auto-gain can clip or under-expose individual channels differently.

### What "locked" means

Locking = turning both auto-modes off and pinning shutter and gain to
fixed numbers, so every frame uses identical camera settings.

## How the code does it

In `bndvi.py`, inside `capture_image()`:

```python
cam.set_controls({
    "AwbEnable":    False,    # turn off auto white balance
    "AeEnable":     False,    # turn off auto exposure
    "AnalogueGain": 2.0,      # fix ISO-style gain at 2×
    "ExposureTime": 5000,     # fix shutter at 5000 μs (≈ 1/200 s)
})
```

The four knobs you might want to change live near the top of `bndvi.py`:

| Constant | Default | What it controls |
|---|---|---|
| `DEFAULT_GAIN` | `2.0` | Sensor gain; raise for shade, lower for harsh sun |
| `DEFAULT_EXPOSURE_US` | `5000` (1/200 s) | Shutter time; longer = brighter, more motion-blur risk |
| `DEFAULT_WARMUP_S` | `3` | Time to let the sensor settle after the controls are applied |
| `DEFAULT_RESOLUTION` | `(3280, 2464)` | Full 8 MP; drop to `(1640, 1232)` for faster testing |

## Step-by-step calibration

1. **Pick your lighting and stick with it.** Direct midday sun is the
   easiest because the spectrum is broad and stable. Bright overcast
   also works. Avoid mixing sun and shade in one frame.

2. **Put a white reference in the scene.** A piece of plain white
   printer paper is fine for a course project. A grey card or Spectralon
   panel is better but unnecessary here. Place it in the same lighting
   as the plant you'll be measuring.

3. **Take a test capture** with the current defaults:
   ```bash
   python bndvi.py
   ```

4. **Open the raw JPEG** (`bndvi_output/raw_*.jpg`) and inspect the
   white paper region:
   - **Too dark** (looks grey/dim) → raise `DEFAULT_GAIN` (try 3.0,
     then 4.0) or raise `DEFAULT_EXPOSURE_US` (try 8000, then 12000).
   - **Saturated / blown out** (pure 255, no detail) → lower them
     (gain 1.0, exposure 2000).
   - **Goal**: the brightest pixels of the white paper should sit around
     **180–230** in the Red and Blue channels — bright but not clipped.

5. **Check the white paper's BNDVI** on the heatmap output. White
   reflects roughly equally across NIR and visible, so a well-calibrated
   rig should produce **BNDVI ≈ 0** on the paper.

   If the paper reads:
   - **Strongly positive** (red-channel dominant): there's NIR
     contamination leaking through the blue channel. Turn on
     **NIR-leakage correction** (Advanced section in the dashboard, or
     `--correct-nir` on the CLI) and tune `k` until the paper reads
     near zero. Start at `k = 0.8` and adjust in steps of ~0.1.
   - **Strongly negative**: exposure on the blue channel is too high —
     lower exposure or gain.

6. **Now point at a known-healthy plant.** The raw image should look
   pinkish/magenta. Mean BNDVI should land somewhere in **+0.3 to +0.7**.
   If you get a negative value, the channel mapping has been reversed —
   that would mean someone copied the logic from `ndvi_capture.old.py`
   into the live code by mistake.

7. **Lock those settings for the rest of your captures.** Don't change
   gain/exposure between captures you want to compare — otherwise your
   time-series chart is meaningless.

## Common gotchas

- **Indoor LED light** has almost no NIR. Plants under indoor lighting
  won't show useful BNDVI even with a perfect rig. Calibrate and test
  outdoors.
- **Through glass** (windows) — most window glass blocks NIR. Don't
  shoot through it.
- **The 3-second warmup matters.** If you reduce `DEFAULT_WARMUP_S`,
  the sensor's analog circuitry may not have settled, and your locked
  exposure won't actually be applied to the captured frame.
- **Changing time of day** changes the sun's spectrum (more red near
  sunset). For day-over-day comparisons, capture at roughly the same
  time of day.

## Quick sanity-check checklist

When something looks off, walk through this before changing the code:

- [ ] AWB is off (look at the raw JPEG — does white paper read white-ish, or strongly tinted?)
- [ ] AE is off (do two consecutive captures of the same scene give similar pixel values?)
- [ ] No mixed sun/shade in the frame
- [ ] Shooting in daylight, not under LEDs
- [ ] No glass between camera and subject
- [ ] White reference in frame and reads BNDVI ≈ 0
- [ ] Healthy plant looks pinkish/magenta in the raw image

## References

- [Ned Horning — Calibrating DIY NIR cameras, Public Lab](https://publiclab.org/notes/nedhorning/10-21-2013/calibrating-diy-nir-cameras-part-1)
- [What's that blue thing doing here? — Raspberry Pi blog](https://www.raspberrypi.com/news/whats-that-blue-thing-doing-here/)
- [Raspberry NoIR + blue filter writeup — Public Lab](https://publiclab.org/notes/carolccarvalho/07-15-2016/raspberry-noir-cam-blue-filter)