#!/usr/bin/env python3
"""
BNDVI Capture & Analysis  —  Hylocropter
=========================================
Hardware : Raspberry Pi 4 Model B (ground rig, or F450 payload)
Camera   : Pi NoIR Camera v2 + bundled blue filter (Rosco Roscolux #2007)

How it works
-------------
The Rosco #2007 blue filter:
  - PASSES visible blue (~400-500 nm) and NIR (>700 nm)
  - BLOCKS red (~550-700 nm)

Behind the NoIR sensor (no IR-cut filter) the Bayer pattern then sees:
  - Red  Bayer pixels: NIR only            -> Red channel  ~ NIR
  - Blue Bayer pixels: visible blue + NIR  -> Blue channel ~ visible blue
  - Green Bayer pixels: mostly blocked     -> unused by the index

So:
    BNDVI = (NIR - Blue) / (NIR + Blue)
          = (R   - B   ) / (R   + B   )

BNDVI ranges -1 to +1. Default class boundaries (see THRESHOLD note below):
    > 0.3   : Dense / healthy vegetation   (rendered green)
    0.1-0.3 : Sparse / moderate vegetation (rendered yellow)
    < 0.1   : Bare soil, water, stress     (rendered red)

Sanity check: pointing at healthy green vegetation should produce a
PINK / MAGENTA raw image (plants reflect a lot of NIR, which lands in
the red channel). If it looks natural-coloured, AWB is still on.

THRESHOLD note
---------------
The defaults above are generic vegetation values, NOT values derived for
dragon fruit (Hylocereus). They are overridable everywhere -- the dashboard
stores them per capture so old records keep their original meaning. See
RESEARCH-GAPS.md section 5 for how to derive real ones from ground truth.

CLI usage
----------
    python bndvi.py                       # capture + analyse + save
    python bndvi.py --dev                 # synthetic test image (laptop dev)
    python bndvi.py --dev --scene soil    # pick a synthetic scene
    python bndvi.py --correct-nir --k 0.8
    python bndvi.py --save-array          # also write the float32 BNDVI as .npz

This module deliberately has NO Flask dependency -- `python bndvi.py --dev`
is the project's minimal reproducer and must always work standalone.

Module usage
-------------
    from bndvi import capture_and_analyse
    record = capture_and_analyse(output_dir, label="tomato row 2")
"""

import datetime
import json
import sys
import time
from pathlib import Path

import numpy as np

DEFAULT_RESOLUTION = (3280, 2464)
DEFAULT_WARMUP_S = 3
DEFAULT_GAIN = 2.0
DEFAULT_EXPOSURE_US = 5000

# Fixed colour gains (red, blue) applied when locking white balance. Setting
# ColourGains is what actually pins AWB in libcamera -- AwbEnable=False alone
# leaves whatever gains the algorithm last chose, which is not repeatable.
DEFAULT_COLOUR_GAINS = (1.0, 1.0)

# Class boundaries for the three-band health split.
DEFAULT_THRESHOLD_HEALTHY = 0.3
DEFAULT_THRESHOLD_MODERATE = 0.1

# NIR-leakage correction: blue Bayer pixels also pick up some NIR. We
# approximate visible_blue as max(eps, B - k*R) where k is the NIR
# responsivity ratio of the blue vs red Bayer pixels.
#
# CAUTION: 0.8 is widely quoted for this rig but its provenance could not be
# verified, and it does not appear in the project thesis. Prefer measuring your
# own k against a white reference -- solve_leak_coef() below does it in one
# step, and the dashboard's Debug view wires it to a drag-a-box interaction.
# See RESEARCH-GAPS.md section 2.
DEFAULT_NIR_LEAK_COEF = 0.8

# ── the one true colormap ────────────────────────────────────────────────────
# Single source of truth for how a BNDVI value becomes a colour. Mirrored
# verbatim in static/js/colormap.js -- if you change one, change both.
# Also handed to matplotlib so the heatmap PNG matches the browser exactly.
BNDVI_COLOR_STOPS = (
    (-1.00, (90, 24, 18)),
    (-0.20, (193, 68, 46)),
    (0.15, (224, 160, 32)),
    (0.35, (242, 227, 74)),
    (0.60, (87, 168, 63)),
    (1.00, (24, 107, 43)),
)

# Flat band colours, used when rendering the three-class false-colour view.
BAND_COLORS = {
    "healthy": (47, 143, 62),
    "moderate": (224, 160, 32),
    "stressed": (193, 68, 46),
}


class CameraUnavailable(RuntimeError):
    """No usable camera. Raised instead of exiting, so a web request can 500
    cleanly rather than killing the Flask worker."""


# ── camera capture ────────────────────────────────────────────────────────────

def locked_controls(gain=DEFAULT_GAIN, exposure_us=DEFAULT_EXPOSURE_US,
                    colour_gains=DEFAULT_COLOUR_GAINS):
    """The control dict that pins the camera so BNDVI stays comparable.

    Everything here exists to stop the ISP touching the channels
    independently -- the index is a ratio between R and B, so any per-channel
    automatic adjustment corrupts it.
    """
    return {
        "AwbEnable": False,                       # no auto white balance
        "AeEnable": False,                        # no auto exposure/gain
        "ColourGains": tuple(float(g) for g in colour_gains),  # pin AWB properly
        "AnalogueGain": float(gain),
        "ExposureTime": int(exposure_us),
        "Sharpness": 0.0,                         # sharpening is non-linear
        "Brightness": 0.0,
        "Contrast": 1.0,
        "Saturation": 1.0,                        # do not stretch channel ratios
    }


def capture_image(
    resolution=DEFAULT_RESOLUTION,
    warmup_s=DEFAULT_WARMUP_S,
    gain=DEFAULT_GAIN,
    exposure_us=DEFAULT_EXPOSURE_US,
    colour_gains=DEFAULT_COLOUR_GAINS,
    dev_mode=False,
    scene="mixed",
    jitter=0.0,
):
    """Capture a still with AWB and AE locked. Returns (H, W, 3) uint8 RGB."""
    if dev_mode:
        return synthetic_frame(resolution, scene=scene, jitter=jitter)

    try:
        from picamera2 import Picamera2
    except ImportError:
        try:
            return _capture_legacy(resolution, warmup_s, exposure_us)
        except ImportError:
            raise CameraUnavailable(
                "Neither picamera2 nor picamera is installed. Install with: "
                "sudo apt install python3-picamera2 (or use dev mode for a "
                "synthetic frame)."
            )

    cam = Picamera2()
    try:
        # Controls go in the configuration, not a set_controls() call after
        # start(). The official picamera2 examples do it this way and it removes
        # the race where the first frames are captured before the locked values
        # have actually been applied.
        config = cam.create_still_configuration(
            main={"size": tuple(resolution), "format": "RGB888"},
            controls=locked_controls(gain, exposure_us, colour_gains),
        )
        cam.configure(config)
        cam.start()
        # Still worth a short settle even with controls pre-applied: the sensor's
        # analogue chain needs a few frames to reach the requested exposure.
        time.sleep(max(0.0, warmup_s))
        return cam.capture_array("main")
    finally:
        try:
            cam.stop()
        except Exception:
            pass
        cam.close()


def capture_raw_dng(dng_path, resolution=DEFAULT_RESOLUTION,
                    gain=DEFAULT_GAIN, exposure_us=DEFAULT_EXPOSURE_US,
                    colour_gains=DEFAULT_COLOUR_GAINS,
                    warmup_s=DEFAULT_WARMUP_S):
    """Capture RGB and also save the unprocessed Bayer frame as DNG.

    Why bother: JPEG/RGB output is gamma-encoded, and gamma is a per-channel
    non-linear curve. (R-B)/(R+B) computed on gamma-encoded 8-bit values is not
    the same number as on linear sensor counts. For calibration-grade work the
    raw frame is the honest input. Returns (rgb_array, dng_filename_or_None).
    """
    try:
        from picamera2 import Picamera2
    except ImportError:
        raise CameraUnavailable("picamera2 is required for raw DNG capture.")

    cam = Picamera2()
    try:
        config = cam.create_still_configuration(
            main={"size": tuple(resolution), "format": "RGB888"},
            raw={},
            controls=locked_controls(gain, exposure_us, colour_gains),
        )
        cam.configure(config)
        cam.start()
        time.sleep(max(0.0, warmup_s))
        buffers, metadata = cam.switch_mode_and_capture_buffers(
            config, ["main", "raw"])
        rgb = cam.helpers.make_array(buffers[0], config["main"])
        try:
            cam.helpers.save_dng(buffers[1], metadata, config["raw"],
                                 str(dng_path))
            return rgb, Path(dng_path).name
        except Exception:
            # DNG is a bonus; never lose the capture over it.
            return rgb, None
    finally:
        try:
            cam.stop()
        except Exception:
            pass
        cam.close()


def _capture_legacy(resolution, warmup_s, exposure_us):
    import picamera
    import picamera.array
    with picamera.PiCamera() as cam:
        cam.resolution = tuple(resolution)
        cam.awb_mode = "off"
        cam.awb_gains = DEFAULT_COLOUR_GAINS
        cam.exposure_mode = "off"
        cam.shutter_speed = int(exposure_us)
        cam.iso = 100
        cam.start_preview()
        time.sleep(warmup_s)
        with picamera.array.PiRGBArray(cam) as output:
            cam.capture(output, format="rgb")
            return output.array


def probe_camera():
    """Report whether a real camera is usable, without grabbing a frame.

    Lets the dashboard show 'Camera ready' / 'No camera detected' without
    blocking on a capture. Never raises.
    """
    try:
        from picamera2 import Picamera2
    except ImportError:
        return {"available": False, "backend": None,
                "detail": "picamera2 is not installed"}
    try:
        cameras = Picamera2.global_camera_info()
    except Exception as exc:
        return {"available": False, "backend": "picamera2",
                "detail": f"camera enumeration failed: {exc}"}
    if not cameras:
        return {"available": False, "backend": "picamera2",
                "detail": "no camera detected on the CSI port"}
    model = cameras[0].get("Model", "unknown")
    return {"available": True, "backend": "picamera2", "detail": model,
            "model": model, "count": len(cameras)}


# ── synthetic frames (dev mode) ──────────────────────────────────────────────
# Dev mode is first-class: synthetic frames flow through the identical pipeline
# as real ones. The original version was seeded at a constant with static
# geometry, so every dev capture produced near-identical stats and the trend
# chart was a flat line. These scenes vary by scene key and by `jitter`, which
# the live preview advances each frame.

SCENES = {
    "healthy": {"label": "Healthy row", "plants": 9, "health": (0.72, 1.00),
                "size": (26, 46)},
    "mixed": {"label": "Mixed plot", "plants": 6, "health": (0.25, 0.95),
              "size": (18, 38)},
    "stressed": {"label": "Stressed row", "plants": 6, "health": (0.05, 0.40),
                 "size": (18, 38)},
    "soil": {"label": "Bare soil", "plants": 1, "health": (0.00, 0.10),
             "size": (10, 18)},
}

SYNTH_W, SYNTH_H = 160, 120

# The synthetic scenes model NIR leaking into the blue channel, the way the real
# gel does, so the correction path is genuinely exercised in dev mode rather
# than being a no-op. Correcting with k = SYNTH_LEAK recovers visible blue
# exactly, and solve_leak_coef() on the white patch should return this value --
# which makes the whole calibration workflow demonstrable with no camera.
SYNTH_LEAK = 0.35

# Where the synthetic white reference card sits (x0, y0, x1, y1), and its
# reflectance. White reflects about equally in NIR and visible, so a correctly
# corrected rig must read BNDVI ~ 0 here.
SYNTH_WHITE_BOX = (8, 8, 34, 27)
SYNTH_WHITE_LEVEL = 150.0


def synthetic_field(scene="mixed", jitter=0.0, seed=None, white_ref=True):
    """Generate the small (SYNTH_H, SYNTH_W) NIR and blue planes for a scene.

    Returns the *observed* channels, i.e. blue already contains NIR leakage,
    exactly as the sensor would deliver them.

    Kept separate from synthetic_frame() because the live preview wants the
    small planes directly -- no point upsampling to 8 MP and straight back down.
    """
    spec = SCENES.get(scene, SCENES["mixed"])
    base_seed = seed if seed is not None else (
        sum(ord(c) for c in scene) * 7919 + 13)
    rng = np.random.default_rng(base_seed)

    yy, xx = np.mgrid[0:SYNTH_H, 0:SYNTH_W].astype(np.float32)
    cover = np.zeros((SYNTH_H, SYNTH_W), np.float32)
    health = np.zeros((SYNTH_H, SYNTH_W), np.float32)
    lo, hi = spec["size"]
    for _ in range(spec["plants"]):
        px, py = rng.uniform(0, SYNTH_W), rng.uniform(0, SYNTH_H)
        rx, ry = rng.uniform(lo, hi), rng.uniform(lo - 2, hi - 4)
        hv = rng.uniform(*spec["health"])
        d = ((xx - px) / rx) ** 2 + ((yy - py) / ry) ** 2
        wobble = 0.85 + 0.30 * np.sin(xx * 0.35 + yy * 0.22 + jitter * 2.4)
        c = np.clip(np.maximum(0.0, 1.0 - d) * 1.5 * wobble, 0, 1)
        take = c > cover
        cover = np.where(take, c, cover)
        health = np.where(take, hv, health)

    nrng = np.random.default_rng(int(base_seed * 131 + jitter * 100) % (2 ** 32))
    noise = nrng.normal(0, 5, (SYNTH_H, SYNTH_W)).astype(np.float32)

    # Soil: modest NIR, fairly bright visible blue.
    soil_nir = 85 + np.sin(xx * 0.06) * 8
    soil_vis = 70 + np.cos(yy * 0.05) * 6
    # Vegetation: NIR climbs steeply with health, visible blue is absorbed by
    # chlorophyll, so healthier leaves are *darker* in blue.
    plant_nir = 110 + 140 * health
    plant_vis = 62 - 30 * health

    nir = soil_nir + (plant_nir - soil_nir) * cover + noise
    vis = soil_vis + (plant_vis - soil_vis) * cover + noise * 0.6

    if white_ref:
        x0, y0, x1, y1 = SYNTH_WHITE_BOX
        nir[y0:y1, x0:x1] = SYNTH_WHITE_LEVEL
        vis[y0:y1, x0:x1] = SYNTH_WHITE_LEVEL

    # What the sensor actually sees in the blue channel.
    blue = vis + SYNTH_LEAK * nir
    return nir.clip(0, 255), blue.clip(0, 255)


def synthetic_frame(resolution=DEFAULT_RESOLUTION, scene="mixed", jitter=0.0,
                    seed=None):
    """Fake Infrablue frame: NIR-bright plant blobs on soil, upsampled.

    Renders small then upsamples -- generating 8 MP of per-pixel noise is
    pointlessly slow on a Pi and the result is only ever a stand-in.
    """
    nir, blue = synthetic_field(scene, jitter, seed)
    green = 0.28 * nir + 0.25 * blue          # mostly blocked by the gel
    small = np.stack([nir, green, blue], -1).clip(0, 255).astype(np.uint8)

    w, h = int(resolution[0]), int(resolution[1])
    ys = (np.arange(h) * SYNTH_H // h).clip(0, SYNTH_H - 1)
    xs = (np.arange(w) * SYNTH_W // w).clip(0, SYNTH_W - 1)
    return small[ys][:, xs]


# ── BNDVI calculation ────────────────────────────────────────────────────────

def compute_bndvi(rgb_array, correct_nir_leakage=False,
                  nir_leak_coef=DEFAULT_NIR_LEAK_COEF):
    """
    Compute per-pixel BNDVI from an Infrablue (RGB) image.

    With Pi NoIR + Rosco #2007 blue filter:
        Red channel  -> NIR proxy
        Blue channel -> visible blue + NIR contamination

    If correct_nir_leakage is False (default), use the raw blue channel:
        BNDVI = (R - B) / (R + B)

    If True, subtract estimated NIR leakage from the blue channel first:
        vis_blue = max(eps, B - k * R)
        BNDVI    = (R - vis_blue) / (R + vis_blue)
    """
    nir = rgb_array[:, :, 0].astype(np.float32)
    blue_raw = rgb_array[:, :, 2].astype(np.float32)

    if correct_nir_leakage:
        # clamp to a small positive value so dense-vegetation pixels
        # (where k*R can exceed B) don't blow up or flip sign
        vis = np.clip(blue_raw - float(nir_leak_coef) * nir, 1.0, None)
    else:
        vis = blue_raw

    denom = nir + vis
    bndvi = np.where(denom == 0, 0.0, (nir - vis) / denom)
    return np.clip(bndvi, -1.0, 1.0).astype(np.float32)


def solve_leak_coef(nir_mean, blue_mean):
    """Derive k from a white reference, where BNDVI should read ~0.

    A white/grey target reflects roughly equally across NIR and visible, so a
    correctly corrected rig reads BNDVI = 0 on it. That means nir == vis:

        R = B - k*R   ->   k = B/R - 1

    Returns (k, message). k is None when no positive coefficient can work,
    which happens when B <= R on the reference -- i.e. the red channel is
    over-responding. The fix then is less exposure or gain, not a bigger k.
    """
    if nir_mean <= 0:
        return None, ("The reference area reads black in the NIR channel. Point "
                      "at a lit white target and try again.")
    k = (blue_mean / nir_mean) - 1.0
    if k <= 0:
        return None, (
            "Blue is already below NIR on this reference, so BNDVI reads "
            "positive on white and no leakage coefficient can correct it. "
            "Lower the exposure or the analogue gain instead.")
    if k > 1.2:
        return round(k, 3), (
            f"Solved k = {k:.2f}, which is unusually high. Check the box covers "
            f"only the white reference and that it is not in shade.")
    return round(k, 3), f"Solved k = {k:.2f} from the white reference."


def bndvi_stats(bndvi, threshold_healthy=DEFAULT_THRESHOLD_HEALTHY,
                threshold_moderate=DEFAULT_THRESHOLD_MODERATE):
    """Summary statistics. Percentages are 0-100 and sum to ~100."""
    return {
        "min": float(np.min(bndvi)),
        "max": float(np.max(bndvi)),
        "mean": float(np.mean(bndvi)),
        "median": float(np.median(bndvi)),
        "std": float(np.std(bndvi)),
        "healthy_pct": float(np.mean(bndvi > threshold_healthy) * 100),
        "moderate_pct": float(np.mean((bndvi >= threshold_moderate)
                                      & (bndvi <= threshold_healthy)) * 100),
        "stressed_pct": float(np.mean(bndvi < threshold_moderate) * 100),
    }


def classify(mean_bndvi, threshold_healthy=DEFAULT_THRESHOLD_HEALTHY,
             threshold_moderate=DEFAULT_THRESHOLD_MODERATE):
    if mean_bndvi > threshold_healthy:
        return "healthy"
    if mean_bndvi >= threshold_moderate:
        return "moderate"
    return "stressed"


def channel_means(rgb_array):
    """Mean of each channel, for the debug readout and the AWB sanity check."""
    return {
        "nir": float(np.mean(rgb_array[:, :, 0])),
        "green": float(np.mean(rgb_array[:, :, 1])),
        "blue": float(np.mean(rgb_array[:, :, 2])),
        "nir_max": float(np.max(rgb_array[:, :, 0])),
        "blue_max": float(np.max(rgb_array[:, :, 2])),
    }


def exposure_warning(rgb_array):
    """Flag the two exposure faults that silently wreck the index.

    CALIBRATION.md aims for the brightest white-reference pixels at 180-230:
    bright but not clipped. This makes the equivalent judgement on the whole
    frame, so the dashboard can say something useful without a reference card.
    """
    nir = rgb_array[:, :, 0]
    blue = rgb_array[:, :, 2]
    clipped = float(np.mean((nir >= 254) | (blue >= 254)) * 100)
    dark = float(np.mean((nir < 25) & (blue < 25)) * 100)
    if clipped > 2.0:
        return {"level": "warn", "text": (
            f"{clipped:.1f}% of pixels are clipped at 255. Clipped channels make "
            f"BNDVI read falsely flat -- lower the exposure or gain.")}
    if dark > 60.0:
        return {"level": "warn", "text": (
            f"{dark:.0f}% of the frame is nearly black. Raise the exposure or "
            f"gain, or move into daylight -- indoor LEDs emit almost no NIR.")}
    return {"level": "ok", "text": "Exposure looks usable."}


# ── false-colour maps ────────────────────────────────────────────────────────

def bndvi_to_rgb(bndvi):
    """Map BNDVI [-1, 1] to RGB using BNDVI_COLOR_STOPS (smooth colormap)."""
    out = np.empty(bndvi.shape + (3,), dtype=np.uint8)
    stops = BNDVI_COLOR_STOPS
    out[...] = np.array(stops[0][1], dtype=np.uint8)
    for i in range(len(stops) - 1):
        v0, c0 = stops[i]
        v1, c1 = stops[i + 1]
        mask = (bndvi >= v0) & (bndvi < v1)
        if not mask.any():
            continue
        t = (bndvi - v0) / (v1 - v0)
        for ch in range(3):
            out[..., ch] = np.where(
                mask, c0[ch] + t * (c1[ch] - c0[ch]), out[..., ch]
            ).astype(np.uint8)
    out[bndvi >= stops[-1][0]] = np.array(stops[-1][1], dtype=np.uint8)
    return out


def bndvi_to_bands_rgb(bndvi, threshold_healthy=DEFAULT_THRESHOLD_HEALTHY,
                       threshold_moderate=DEFAULT_THRESHOLD_MODERATE):
    """Flat three-colour rendering at the given thresholds."""
    out = np.empty(bndvi.shape + (3,), dtype=np.uint8)
    out[...] = np.array(BAND_COLORS["stressed"], dtype=np.uint8)
    out[bndvi >= threshold_moderate] = np.array(BAND_COLORS["moderate"],
                                                dtype=np.uint8)
    out[bndvi > threshold_healthy] = np.array(BAND_COLORS["healthy"],
                                              dtype=np.uint8)
    return out


def matplotlib_colormap():
    """BNDVI_COLOR_STOPS as a matplotlib colormap, so the heatmap PNG uses the
    same colours as the browser instead of matplotlib's own RdYlGn."""
    from matplotlib.colors import LinearSegmentedColormap
    pts = [((v + 1.0) / 2.0, tuple(c / 255 for c in rgb))
           for v, rgb in BNDVI_COLOR_STOPS]
    return LinearSegmentedColormap.from_list("bndvi", pts)


# ── output rendering ─────────────────────────────────────────────────────────

def render_outputs(rgb_array, bndvi, bndvi_rgb, output_dir, capture_id,
                   save_array=False, threshold_healthy=DEFAULT_THRESHOLD_HEALTHY,
                   threshold_moderate=DEFAULT_THRESHOLD_MODERATE):
    """Save raw, heatmap, false-colour, thumbnail (+ optional .npz).
    Returns a dict of relative filenames."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from PIL import Image

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    names = {
        "raw": f"raw_{capture_id}.jpg",
        "heatmap": f"heatmap_{capture_id}.png",
        "falsecolor": f"falsecolor_{capture_id}.png",
        "thumb": f"thumb_{capture_id}.jpg",
    }

    Image.fromarray(rgb_array).save(output_dir / names["raw"], quality=92)
    Image.fromarray(bndvi_rgb).save(output_dir / names["falsecolor"])

    thumb = Image.fromarray(bndvi_rgb)
    thumb.thumbnail((400, 400))
    thumb.save(output_dir / names["thumb"], quality=85)

    if save_array:
        names["array"] = f"bndvi_{capture_id}.npz"
        np.savez_compressed(output_dir / names["array"], bndvi=bndvi)

    fig, axes = plt.subplots(1, 2, figsize=(16, 7), facecolor="#f5ead8")
    axes[0].imshow(rgb_array)
    axes[0].set_title("Original (Infrablue)", color="#201e1d", fontsize=13, pad=8)
    axes[0].axis("off")

    img = axes[1].imshow(bndvi, cmap=matplotlib_colormap(), vmin=-1, vmax=1,
                         interpolation="nearest")
    axes[1].set_title("BNDVI Heatmap", color="#201e1d", fontsize=13, pad=8)
    axes[1].axis("off")

    cbar = fig.colorbar(img, ax=axes[1], fraction=0.035, pad=0.03)
    cbar.set_label("BNDVI", color="#201e1d", fontsize=10)
    cbar.ax.yaxis.set_tick_params(color="#201e1d")
    plt.setp(cbar.ax.yaxis.get_ticklabels(), color="#201e1d")

    s = bndvi_stats(bndvi, threshold_healthy, threshold_moderate)
    overlay = (
        f"Mean : {s['mean']:+.3f}\n"
        f"Healthy  (>{threshold_healthy:g}) : {s['healthy_pct']:.1f}%\n"
        f"Stressed (<{threshold_moderate:g}) : {s['stressed_pct']:.1f}%"
    )
    axes[1].text(
        0.02, 0.02, overlay,
        transform=axes[1].transAxes,
        fontsize=9, va="bottom", color="#f5ead8",
        bbox=dict(facecolor="#201e1d", alpha=0.7, boxstyle="round,pad=0.4"),
    )

    plt.suptitle(f"BNDVI Analysis  -  {capture_id}", color="#201e1d",
                 fontsize=14, y=1.01)
    plt.tight_layout()
    fig.savefig(output_dir / names["heatmap"], dpi=130, bbox_inches="tight",
                facecolor="#f5ead8")
    plt.close(fig)

    return names


# ── high-level pipeline ──────────────────────────────────────────────────────

def capture_and_analyse(
    output_dir,
    label=None,
    notes=None,
    dev_mode=False,
    correct_nir_leakage=False,
    nir_leak_coef=DEFAULT_NIR_LEAK_COEF,
    threshold_healthy=DEFAULT_THRESHOLD_HEALTHY,
    threshold_moderate=DEFAULT_THRESHOLD_MODERATE,
    save_array=False,
    capture_format="rgb888",
    flight_id=None,
    geo=None,
    trigger="manual",
    rgb=None,
    **cam_kwargs,
):
    """Run the full pipeline. Returns a metadata record.

    `rgb` lets a caller supply an already-captured frame -- the debug view's
    "save this frame as a capture" does exactly that, so a saved debug frame
    flows through the identical analysis and rendering path as a real capture.
    """
    capture_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    started = time.time()
    dng_name = None

    if rgb is None:
        if capture_format == "raw_dng" and not dev_mode:
            rgb, dng_name = capture_raw_dng(
                Path(output_dir) / f"raw_{capture_id}.dng",
                resolution=cam_kwargs.get("resolution", DEFAULT_RESOLUTION),
                gain=cam_kwargs.get("gain", DEFAULT_GAIN),
                exposure_us=cam_kwargs.get("exposure_us", DEFAULT_EXPOSURE_US),
                colour_gains=cam_kwargs.get("colour_gains",
                                            DEFAULT_COLOUR_GAINS),
                warmup_s=cam_kwargs.get("warmup_s", DEFAULT_WARMUP_S),
            )
        else:
            rgb = capture_image(dev_mode=dev_mode, **cam_kwargs)

    settings = {
        "resolution": [int(rgb.shape[1]), int(rgb.shape[0])],
        "exposure_us": cam_kwargs.get("exposure_us", DEFAULT_EXPOSURE_US),
        "gain": cam_kwargs.get("gain", DEFAULT_GAIN),
        "dev_mode": dev_mode,
        "correct_nir_leakage": bool(correct_nir_leakage),
        "nir_leak_coef": float(nir_leak_coef) if correct_nir_leakage else None,
        # additive fields -- records written before these existed simply lack
        # them, and the detail page renders a dash rather than breaking
        "warmup_s": cam_kwargs.get("warmup_s", DEFAULT_WARMUP_S),
        "colour_gains": list(cam_kwargs.get("colour_gains",
                                            DEFAULT_COLOUR_GAINS)),
        "awb_locked": not dev_mode,
        "ae_locked": not dev_mode,
        "threshold_healthy": float(threshold_healthy),
        "threshold_moderate": float(threshold_moderate),
        "capture_format": capture_format,
    }

    bndvi = compute_bndvi(rgb, correct_nir_leakage=correct_nir_leakage,
                          nir_leak_coef=nir_leak_coef)
    bndvi_rgb = bndvi_to_rgb(bndvi)
    files = render_outputs(rgb, bndvi, bndvi_rgb, output_dir, capture_id,
                           save_array=save_array,
                           threshold_healthy=threshold_healthy,
                           threshold_moderate=threshold_moderate)
    if dng_name:
        files["dng"] = dng_name
    stats = bndvi_stats(bndvi, threshold_healthy, threshold_moderate)

    return {
        "id": capture_id,
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "label": label,
        "notes": notes,
        "files": files,
        "stats": stats,
        "classification": classify(stats["mean"], threshold_healthy,
                                   threshold_moderate),
        "settings": settings,
        # UAV fields -- null on a ground capture
        "flight_id": flight_id,
        "geo": geo,
        "trigger": trigger,
        "channels": channel_means(rgb),
        "exposure_check": exposure_warning(rgb),
        "process_ms": int((time.time() - started) * 1000),
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli():
    import argparse
    p = argparse.ArgumentParser(
        description="Hylocropter BNDVI capture (Pi NoIR + Rosco #2007)")
    p.add_argument("--dev", action="store_true",
                   help="use a synthetic frame instead of the camera")
    p.add_argument("--scene", default="mixed", choices=sorted(SCENES),
                   help="synthetic scene to render in dev mode")
    p.add_argument("--correct-nir", action="store_true",
                   help="subtract estimated NIR leakage from the blue channel")
    p.add_argument("--k", type=float, default=DEFAULT_NIR_LEAK_COEF,
                   help=f"NIR leakage coefficient (default {DEFAULT_NIR_LEAK_COEF})")
    p.add_argument("--healthy", type=float, default=DEFAULT_THRESHOLD_HEALTHY,
                   help="BNDVI above this counts as healthy")
    p.add_argument("--moderate", type=float, default=DEFAULT_THRESHOLD_MODERATE,
                   help="BNDVI below this counts as stressed")
    p.add_argument("--save-array", action="store_true",
                   help="also write the float32 BNDVI array as .npz")
    p.add_argument("--raw", action="store_true",
                   help="also save the unprocessed Bayer frame as DNG")
    p.add_argument("-o", "--output", default="./hylocropter_data/ground",
                   help="output directory")
    args = p.parse_args()

    output_dir = Path(args.output)
    print("=" * 60)
    print("  Hylocropter BNDVI Capture - Pi NoIR + Rosco #2007")
    print("=" * 60)
    kwargs = {}
    if args.dev:
        kwargs["scene"] = args.scene
    try:
        record = capture_and_analyse(
            output_dir,
            dev_mode=args.dev,
            correct_nir_leakage=args.correct_nir,
            nir_leak_coef=args.k,
            threshold_healthy=args.healthy,
            threshold_moderate=args.moderate,
            save_array=args.save_array,
            capture_format="raw_dng" if args.raw else "rgb888",
            **kwargs,
        )
    except CameraUnavailable as exc:
        print(f"[ERROR] {exc}")
        print("        Try:  python bndvi.py --dev")
        sys.exit(1)

    print(json.dumps(record, indent=2))
    print(f"\n  mean BNDVI {record['stats']['mean']:+.3f}"
          f"  ->  {record['classification']}")
    print(f"  {record['exposure_check']['text']}")
    print(f"\n[DONE] Outputs in: {output_dir.resolve()}")


if __name__ == "__main__":
    _cli()
