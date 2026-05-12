#!/usr/bin/env python3
"""
*** ARCHIVED / REFERENCE ONLY -- DO NOT RUN ***

This is the ORIGINAL NDVI capture script that shipped with the first commit
of the project. It is preserved here for the writeup / report only.

DO NOT USE IT FOR REAL CAPTURES. It has a channel-mapping bug:
  - It assumes Blue Bayer = NIR and Red Bayer = visible.
  - With the Pi NoIR + Rosco #2007 blue filter, the mapping is the
    other way around (the gel blocks red, so Red Bayer = NIR and
    Blue Bayer = visible + some NIR). The result is the negative of
    what it should be -- healthy plants register as "stressed".

The fixed implementation is in:
    bndvi_dashboard/bndvi.py        (importable + CLI)
    bndvi_dashboard/app.py          (Flask dashboard around it)

The unmodified original follows below this banner.
=================================================================
"""

# ─────────────────────────────────────────────────────────────────
# Original file content (unchanged from initial commit):
# ─────────────────────────────────────────────────────────────────

"""
Infrablue NDVI Capture & Visualization  -  v3
===============================================
Hardware : Raspberry Pi 4 Model B
Camera   : Pi NoIR Camera + Blue filter (Infrablue setup)

Changes from v2
----------------
  - Added SAVE_NPY toggle at the top (default False)
  - Fixed TabError: all indentation is now spaces only

How Infrablue NDVI works
-------------------------
With a blue filter blocking visible blue light and passing NIR:
  - Blue channel (index 2 in RGB) -> captures Near-Infrared (NIR)
  - Red  channel (index 0 in RGB) -> captures visible Red

  NDVI = (NIR - Red) / (NIR + Red)
       = (Blue_ch - Red_ch) / (Blue_ch + Red_ch)

NDVI ranges from -1 to +1:
  > 0.3  : Dense/healthy vegetation  (rendered green)
  0.1-0.3: Sparse / moderate vegetation
  < 0.1  : Bare soil, water, stress  (rendered red)

Sanity check: pointing at healthy green vegetation should produce a
bluish-purple raw image (plants reflect a lot of NIR into the blue
channel). If it looks natural-coloured, AWB is still interfering.

Outputs (saved in ./ndvi_output/)
----------------------------------
  raw_capture_<ts>.jpg       - Original photo as captured
  ndvi_heatmap_<ts>.png      - Side-by-side: original + NDVI map with colour bar
  ndvi_falsecolor_<ts>.png   - False-colour image (green=healthy, red=stressed)
  ndvi_values_<ts>.npy       - Raw float32 NDVI array (only if SAVE_NPY = True)
  ndvi_stats_<ts>.txt        - Summary statistics
"""

import os
import sys
import time
import datetime
import numpy as np
from pathlib import Path

# ── settings ──────────────────────────────────────────────────────────────────

OUTPUT_DIR       = Path("./ndvi_output")
CAPTURE_RESOLUTION  = (3280, 2464)   # full 8MP; use (1640, 1232) for faster testing
WARMUP_SECONDS      = 3             # time for settings to settle before capture
ANALOGUE_GAIN       = 2.0           # increase if image too dark (try 4.0 outdoors in shade)
EXPOSURE_TIME_US    = 5000          # microseconds; 5000 = ~1/200s (good for bright daylight)
SAVE_NPY            = False         # set True to save raw float32 NDVI array (~30 MB)

OUTPUT_DIR.mkdir(exist_ok=True)
TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


# ── camera capture ────────────────────────────────────────────────────────────

def capture_image():
    """
    Capture a still with AWB and AE locked.
    Returns a NumPy array (H, W, 3) in RGB channel order.
    """
    try:
        from picamera2 import Picamera2

        print("[INFO] Using picamera2 ...")
        cam = Picamera2()

        config = cam.create_still_configuration(
            main={"size": CAPTURE_RESOLUTION, "format": "RGB888"},
            lores={"size": (640, 480)},
            display="lores",
        )
        cam.configure(config)
        cam.start()

        cam.set_controls({
            "AwbEnable":    False,
            "AeEnable":     False,
            "AnalogueGain": ANALOGUE_GAIN,
            "ExposureTime": EXPOSURE_TIME_US,
        })

        print("[INFO] Waiting " + str(WARMUP_SECONDS) + "s for camera settings to settle ...")
        time.sleep(WARMUP_SECONDS)

        frame = cam.capture_array()
        cam.stop()
        cam.close()
        print("[INFO] Captured frame  shape=" + str(frame.shape) + "  dtype=" + str(frame.dtype))
        return frame

    except ImportError:
        try:
            import picamera
            import picamera.array

            print("[INFO] picamera2 not found - falling back to picamera (legacy) ...")
            with picamera.PiCamera() as cam:
                cam.resolution    = CAPTURE_RESOLUTION
                cam.awb_mode      = "off"
                cam.awb_gains     = (1.0, 1.0)
                cam.exposure_mode = "off"
                cam.shutter_speed = EXPOSURE_TIME_US
                cam.iso           = 100
                cam.start_preview()
                time.sleep(WARMUP_SECONDS)
                with picamera.array.PiRGBArray(cam) as output:
                    cam.capture(output, format="rgb")
                    frame = output.array
            print("[INFO] Captured frame  shape=" + str(frame.shape))
            return frame

        except ImportError:
            print("[ERROR] Neither picamera2 nor picamera is installed.")
            print("        Install with:  sudo apt install python3-picamera2")
            sys.exit(1)


# ── NDVI calculation ──────────────────────────────────────────────────────────

def compute_ndvi(rgb_array):
    """
    Compute per-pixel NDVI from an Infrablue (RGB) image.

    Channel mapping (blue filter on NoIR cam):
        rgb_array[:,:,0]  ->  Red   channel  = visible Red
        rgb_array[:,:,1]  ->  Green channel  (not used)
        rgb_array[:,:,2]  ->  Blue  channel  = NIR proxy
    """
    red = rgb_array[:, :, 0].astype(np.float32)
    nir = rgb_array[:, :, 2].astype(np.float32)

    denominator = nir + red
    ndvi = np.where(denominator == 0, 0.0, (nir - red) / denominator)
    ndvi = np.clip(ndvi, -1.0, 1.0)
    return ndvi.astype(np.float32)


# ── false-colour map ──────────────────────────────────────────────────────────

def ndvi_to_rgb(ndvi):
    """
    Map NDVI [-1, 1] to an RGB image using a red-yellow-green gradient.
    """
    h, w = ndvi.shape
    rgb = np.zeros((h, w, 3), dtype=np.uint8)

    norm = (ndvi + 1.0) / 2.0

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

    mask_last = norm >= stops[-1][0]
    rgb[mask_last] = np.array(stops[-1][1:], dtype=np.uint8)

    return rgb


# ── save outputs ──────────────────────────────────────────────────────────────

def save_outputs(rgb_array, ndvi, ndvi_rgb):
    """Save raw image, NDVI heatmap, false-colour image, optional npy, and stats."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from PIL import Image

    mean_ndvi    = float(np.mean(ndvi))
    pct_healthy  = float(np.mean(ndvi >  0.3) * 100)
    pct_moderate = float(np.mean((ndvi >= 0.1) & (ndvi <= 0.3)) * 100)
    pct_stressed = float(np.mean(ndvi <  0.1) * 100)

    # 1 -- raw capture
    raw_path = OUTPUT_DIR / ("raw_capture_" + TIMESTAMP + ".jpg")
    Image.fromarray(rgb_array).save(str(raw_path), quality=95)
    print("[SAVED] Raw capture  -> " + str(raw_path))

    # 2 -- NDVI heatmap
    fig, axes = plt.subplots(1, 2, figsize=(16, 7), facecolor="#1a1a2e")

    ax_orig = axes[0]
    ax_orig.imshow(rgb_array)
    ax_orig.set_title("Original (Infrablue)", color="white", fontsize=13, pad=8)
    ax_orig.axis("off")

    ax_ndvi = axes[1]
    img_plot = ax_ndvi.imshow(ndvi, cmap="RdYlGn", vmin=-1, vmax=1, interpolation="nearest")
    ax_ndvi.set_title("NDVI Map", color="white", fontsize=13, pad=8)
    ax_ndvi.axis("off")

    cbar = fig.colorbar(img_plot, ax=ax_ndvi, fraction=0.035, pad=0.03)
    cbar.set_label("NDVI Value", color="white", fontsize=10)
    cbar.ax.yaxis.set_tick_params(color="white")
    plt.setp(cbar.ax.yaxis.get_ticklabels(), color="white")

    stats_text = (
        "Mean NDVI : " + "{:+.3f}".format(mean_ndvi) + "\n"
        "Healthy  (>0.3) : " + "{:.1f}".format(pct_healthy) + "%\n"
        "Stressed (<0.1) : " + "{:.1f}".format(pct_stressed) + "%"
    )
    ax_ndvi.text(
        0.02, 0.02, stats_text,
        transform=ax_ndvi.transAxes,
        fontsize=9, verticalalignment="bottom",
        color="white",
        bbox=dict(facecolor="black", alpha=0.55, boxstyle="round,pad=0.4"),
    )

    plt.suptitle(
        "Infrablue NDVI Analysis  -  " + TIMESTAMP,
        color="white", fontsize=14, y=1.01,
    )
    plt.tight_layout()

    heatmap_path = OUTPUT_DIR / ("ndvi_heatmap_" + TIMESTAMP + ".png")
    fig.savefig(str(heatmap_path), dpi=150, bbox_inches="tight", facecolor="#1a1a2e")
    plt.close(fig)
    print("[SAVED] NDVI heatmap -> " + str(heatmap_path))

    # 3 -- false-colour image
    fc_path = OUTPUT_DIR / ("ndvi_falsecolor_" + TIMESTAMP + ".png")
    Image.fromarray(ndvi_rgb).save(str(fc_path))
    print("[SAVED] False-colour -> " + str(fc_path))

    # 4 -- optional npy array
    if SAVE_NPY:
        npy_path = OUTPUT_DIR / ("ndvi_values_" + TIMESTAMP + ".npy")
        np.save(str(npy_path), ndvi)
        print("[SAVED] NDVI array   -> " + str(npy_path))
    else:
        print("[SKIP]  NDVI array   (SAVE_NPY = False)")

    # 5 -- statistics text file
    stats_path = OUTPUT_DIR / ("ndvi_stats_" + TIMESTAMP + ".txt")
    with open(stats_path, "w") as f:
        f.write("NDVI Statistics - " + TIMESTAMP + "\n")
        f.write("=" * 40 + "\n")
        f.write("Image resolution : " + str(ndvi.shape[1]) + " x " + str(ndvi.shape[0]) + " px\n")
        f.write("Exposure time    : " + str(EXPOSURE_TIME_US) + " us\n")
        f.write("Analogue gain    : " + str(ANALOGUE_GAIN) + "\n")
        f.write("AWB              : disabled\n\n")
        f.write("Min NDVI         : " + "{:+.4f}".format(float(np.min(ndvi))) + "\n")
        f.write("Max NDVI         : " + "{:+.4f}".format(float(np.max(ndvi))) + "\n")
        f.write("Mean NDVI        : " + "{:+.4f}".format(float(np.mean(ndvi))) + "\n")
        f.write("Median NDVI      : " + "{:+.4f}".format(float(np.median(ndvi))) + "\n")
        f.write("Std Dev          : " + "{:.4f}".format(float(np.std(ndvi))) + "\n")
        f.write("\nVegetation breakdown\n")
        f.write("-" * 40 + "\n")
        f.write("Healthy   (NDVI > 0.3)  : " + "{:.1f}".format(pct_healthy) + "%\n")
        f.write("Moderate (0.1 - 0.3)    : " + "{:.1f}".format(pct_moderate) + "%\n")
        f.write("Stressed  (NDVI < 0.1)  : " + "{:.1f}".format(pct_stressed) + "%\n")
    print("[SAVED] Stats file   -> " + str(stats_path))


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 52)
    print("  Infrablue NDVI Capture v3 - Pi NoIR + Blue Filter")
    print("=" * 52)

    rgb = capture_image()

    print("[INFO] Computing NDVI ...")
    ndvi = compute_ndvi(rgb)
    print("[INFO] NDVI  min=" + "{:+.3f}".format(ndvi.min()) +
          "  max=" + "{:+.3f}".format(ndvi.max()) +
          "  mean=" + "{:+.3f}".format(ndvi.mean()))

    print("[INFO] Generating false-colour image ...")
    ndvi_rgb = ndvi_to_rgb(ndvi)

    print("[INFO] Saving outputs ...")
    save_outputs(rgb, ndvi, ndvi_rgb)

    print("\n[DONE] All outputs saved to: " + str(OUTPUT_DIR.resolve()))


if __name__ == "__main__":
    main()