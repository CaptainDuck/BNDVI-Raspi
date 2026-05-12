#!/usr/bin/env python3
"""
BNDVI Capture & Analysis
=========================
Hardware : Raspberry Pi 4 Model B
Camera   : Pi NoIR Camera + bundled blue filter (Rosco Roscolux #2007)

How it works
-------------
The Rosco #2007 blue filter:
  - PASSES visible blue (~400-500 nm) and NIR (>700 nm)
  - BLOCKS red (~550-700 nm)

Behind the NoIR sensor (no IR-cut filter) the Bayer pattern then sees:
  - Red  Bayer pixels: NIR only            -> Red channel  ~ NIR
  - Blue Bayer pixels: visible blue + NIR  -> Blue channel ~ visible blue

So:
    BNDVI = (NIR - Blue) / (NIR + Blue)
          = (R   - B   ) / (R   + B   )

BNDVI ranges -1 to +1:
    > 0.3   : Dense / healthy vegetation   (rendered green)
    0.1-0.3 : Sparse / moderate vegetation (rendered yellow)
    < 0.1   : Bare soil, water, stress     (rendered red)

Sanity check: pointing at healthy green vegetation should produce a
PINK / MAGENTA raw image (plants reflect a lot of NIR, which lands in
the red channel). If it looks natural-coloured, AWB is still on.

CLI usage
----------
    python bndvi.py             # capture + analyse + save
    python bndvi.py --dev       # synthetic test image (for laptop dev)

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

# NIR-leakage correction: blue Bayer pixels also pick up some NIR. We
# approximate visible_blue as max(eps, B - k*R) where k is the NIR
# responsivity ratio of the blue vs red Bayer pixels. Public Lab's
# Ned Horning uses k=0.8 as the standard for Pi NoIR + Rosco #2007;
# practitioner range is 0.3-0.8. Calibrate against a white reference
# under sunlight for best results.
DEFAULT_NIR_LEAK_COEF = 0.8


# ── camera capture ────────────────────────────────────────────────────────────

def capture_image(
    resolution=DEFAULT_RESOLUTION,
    warmup_s=DEFAULT_WARMUP_S,
    gain=DEFAULT_GAIN,
    exposure_us=DEFAULT_EXPOSURE_US,
    dev_mode=False,
):
    """Capture a still with AWB and AE locked. Returns (H, W, 3) uint8 RGB."""
    if dev_mode:
        return _synthetic_frame(resolution)

    try:
        from picamera2 import Picamera2
    except ImportError:
        try:
            return _capture_legacy(resolution, warmup_s, exposure_us)
        except ImportError:
            print("[ERROR] Neither picamera2 nor picamera is installed.")
            print("        Install:  sudo apt install python3-picamera2")
            print("        Or run with --dev for a synthetic test frame.")
            sys.exit(1)

    print("[INFO] Using picamera2 ...")
    cam = Picamera2()
    config = cam.create_still_configuration(
        main={"size": resolution, "format": "RGB888"},
        lores={"size": (640, 480)},
        display="lores",
    )
    cam.configure(config)
    cam.start()
    cam.set_controls({
        "AwbEnable": False,
        "AeEnable": False,
        "AnalogueGain": gain,
        "ExposureTime": exposure_us,
    })
    print(f"[INFO] Warming up {warmup_s}s ...")
    time.sleep(warmup_s)
    frame = cam.capture_array()
    cam.stop()
    cam.close()
    print(f"[INFO] Captured frame  shape={frame.shape}  dtype={frame.dtype}")
    return frame


def _capture_legacy(resolution, warmup_s, exposure_us):
    import picamera
    import picamera.array
    print("[INFO] picamera2 not found - falling back to legacy picamera ...")
    with picamera.PiCamera() as cam:
        cam.resolution = resolution
        cam.awb_mode = "off"
        cam.awb_gains = (1.0, 1.0)
        cam.exposure_mode = "off"
        cam.shutter_speed = exposure_us
        cam.iso = 100
        cam.start_preview()
        time.sleep(warmup_s)
        with picamera.array.PiRGBArray(cam) as output:
            cam.capture(output, format="rgb")
            return output.array


def _synthetic_frame(resolution):
    """Build a fake Infrablue-looking frame for laptop development."""
    w, h = resolution
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    # vegetation patch (high NIR -> red channel) on the left, soil on the right
    veg = (xx < w * 0.55).astype(np.float32)
    soil = 1.0 - veg
    noise = np.random.default_rng(42).normal(0, 8, (h, w))
    red = (veg * 210 + soil * 90 + noise).clip(0, 255)
    green = (veg * 80 + soil * 70 + noise).clip(0, 255)
    blue = (veg * 70 + soil * 60 + noise).clip(0, 255)
    frame = np.stack([red, green, blue], axis=-1).astype(np.uint8)
    print(f"[INFO] Generated synthetic frame  shape={frame.shape}")
    return frame


# ── BNDVI calculation ────────────────────────────────────────────────────────

def compute_bndvi(rgb_array, correct_nir_leakage=False, nir_leak_coef=DEFAULT_NIR_LEAK_COEF):
    """
    Compute per-pixel BNDVI from an Infrablue (RGB) image.

    With Pi NoIR + Rosco #2007 blue filter:
        Red channel  -> NIR proxy
        Blue channel -> visible blue + NIR contamination

    If correct_nir_leakage is False (default), use the raw blue channel:
        BNDVI = (R - B) / (R + B)

    If True, subtract estimated NIR leakage from the blue channel before
    computing BNDVI:
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


def bndvi_stats(bndvi):
    return {
        "min": float(np.min(bndvi)),
        "max": float(np.max(bndvi)),
        "mean": float(np.mean(bndvi)),
        "median": float(np.median(bndvi)),
        "std": float(np.std(bndvi)),
        "healthy_pct": float(np.mean(bndvi > 0.3) * 100),
        "moderate_pct": float(np.mean((bndvi >= 0.1) & (bndvi <= 0.3)) * 100),
        "stressed_pct": float(np.mean(bndvi < 0.1) * 100),
    }


def classify(mean_bndvi):
    if mean_bndvi > 0.3:
        return "healthy"
    if mean_bndvi >= 0.1:
        return "moderate"
    return "stressed"


# ── false-colour map ─────────────────────────────────────────────────────────

def bndvi_to_rgb(bndvi):
    """Map BNDVI [-1, 1] to an RGB image using a red-yellow-green gradient."""
    h, w = bndvi.shape
    rgb = np.zeros((h, w, 3), dtype=np.uint8)
    norm = (bndvi + 1.0) / 2.0
    stops = [
        (0.00,  64,   0,   0),
        (0.35, 255,   0,   0),
        (0.45, 255, 165,   0),
        (0.55, 255, 255,   0),
        (0.65,   0, 200,   0),
        (1.00,   0, 100,   0),
    ]
    for i in range(len(stops) - 1):
        p0, r0, g0, b0 = stops[i]
        p1, r1, g1, b1 = stops[i + 1]
        mask = (norm >= p0) & (norm < p1)
        t = np.where(mask, (norm - p0) / (p1 - p0), 0.0)
        rgb[:, :, 0] = np.where(mask, r0 + t * (r1 - r0), rgb[:, :, 0]).astype(np.uint8)
        rgb[:, :, 1] = np.where(mask, g0 + t * (g1 - g0), rgb[:, :, 1]).astype(np.uint8)
        rgb[:, :, 2] = np.where(mask, b0 + t * (b1 - b0), rgb[:, :, 2]).astype(np.uint8)
    rgb[norm >= stops[-1][0]] = np.array(stops[-1][1:], dtype=np.uint8)
    return rgb


# ── output rendering ─────────────────────────────────────────────────────────

def render_outputs(rgb_array, bndvi, bndvi_rgb, output_dir, capture_id):
    """Save raw, heatmap, false-colour, thumbnail. Return file paths (relative names)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from PIL import Image

    output_dir = Path(output_dir)
    output_dir.mkdir(exist_ok=True)

    raw_name = f"raw_{capture_id}.jpg"
    heatmap_name = f"heatmap_{capture_id}.png"
    fc_name = f"falsecolor_{capture_id}.png"
    thumb_name = f"thumb_{capture_id}.jpg"

    Image.fromarray(rgb_array).save(output_dir / raw_name, quality=92)

    # thumbnail from the false-colour image (more useful at a glance)
    thumb = Image.fromarray(bndvi_rgb)
    thumb.thumbnail((400, 400))
    thumb.save(output_dir / thumb_name, quality=85)

    Image.fromarray(bndvi_rgb).save(output_dir / fc_name)

    fig, axes = plt.subplots(1, 2, figsize=(16, 7), facecolor="#1a1a2e")
    axes[0].imshow(rgb_array)
    axes[0].set_title("Original (Infrablue)", color="white", fontsize=13, pad=8)
    axes[0].axis("off")

    img = axes[1].imshow(bndvi, cmap="RdYlGn", vmin=-1, vmax=1, interpolation="nearest")
    axes[1].set_title("BNDVI Heatmap", color="white", fontsize=13, pad=8)
    axes[1].axis("off")

    cbar = fig.colorbar(img, ax=axes[1], fraction=0.035, pad=0.03)
    cbar.set_label("BNDVI", color="white", fontsize=10)
    cbar.ax.yaxis.set_tick_params(color="white")
    plt.setp(cbar.ax.yaxis.get_ticklabels(), color="white")

    s = bndvi_stats(bndvi)
    overlay = (
        f"Mean : {s['mean']:+.3f}\n"
        f"Healthy  (>0.3) : {s['healthy_pct']:.1f}%\n"
        f"Stressed (<0.1) : {s['stressed_pct']:.1f}%"
    )
    axes[1].text(
        0.02, 0.02, overlay,
        transform=axes[1].transAxes,
        fontsize=9, va="bottom", color="white",
        bbox=dict(facecolor="black", alpha=0.55, boxstyle="round,pad=0.4"),
    )

    plt.suptitle(f"BNDVI Analysis  -  {capture_id}", color="white", fontsize=14, y=1.01)
    plt.tight_layout()
    fig.savefig(output_dir / heatmap_name, dpi=130, bbox_inches="tight", facecolor="#1a1a2e")
    plt.close(fig)

    return {
        "raw": raw_name,
        "heatmap": heatmap_name,
        "falsecolor": fc_name,
        "thumb": thumb_name,
    }


# ── high-level pipeline ──────────────────────────────────────────────────────

def capture_and_analyse(
    output_dir,
    label=None,
    notes=None,
    dev_mode=False,
    correct_nir_leakage=False,
    nir_leak_coef=DEFAULT_NIR_LEAK_COEF,
    **cam_kwargs,
):
    """Run the full pipeline. Returns a metadata record."""
    capture_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    settings = {
        "resolution": list(cam_kwargs.get("resolution", DEFAULT_RESOLUTION)),
        "exposure_us": cam_kwargs.get("exposure_us", DEFAULT_EXPOSURE_US),
        "gain": cam_kwargs.get("gain", DEFAULT_GAIN),
        "dev_mode": dev_mode,
        "correct_nir_leakage": bool(correct_nir_leakage),
        "nir_leak_coef": float(nir_leak_coef) if correct_nir_leakage else None,
    }

    rgb = capture_image(dev_mode=dev_mode, **cam_kwargs)
    bndvi = compute_bndvi(
        rgb,
        correct_nir_leakage=correct_nir_leakage,
        nir_leak_coef=nir_leak_coef,
    )
    bndvi_rgb = bndvi_to_rgb(bndvi)
    files = render_outputs(rgb, bndvi, bndvi_rgb, output_dir, capture_id)
    stats = bndvi_stats(bndvi)

    return {
        "id": capture_id,
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "label": label,
        "notes": notes,
        "files": files,
        "stats": stats,
        "classification": classify(stats["mean"]),
        "settings": settings,
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli():
    import argparse
    p = argparse.ArgumentParser(description="BNDVI capture (Pi NoIR + blue filter)")
    p.add_argument("--dev", action="store_true",
                   help="use a synthetic frame instead of the camera")
    p.add_argument("--correct-nir", action="store_true",
                   help="subtract estimated NIR leakage from the blue channel")
    p.add_argument("--k", type=float, default=DEFAULT_NIR_LEAK_COEF,
                   help=f"NIR leakage coefficient (default {DEFAULT_NIR_LEAK_COEF})")
    args = p.parse_args()

    output_dir = Path("./bndvi_output")
    print("=" * 56)
    print("  BNDVI Capture - Pi NoIR + Blue Filter (Rosco #2007)")
    print("=" * 56)
    record = capture_and_analyse(
        output_dir,
        dev_mode=args.dev,
        correct_nir_leakage=args.correct_nir,
        nir_leak_coef=args.k,
    )
    print(json.dumps(record, indent=2))
    print(f"\n[DONE] Outputs in: {output_dir.resolve()}")


if __name__ == "__main__":
    _cli()