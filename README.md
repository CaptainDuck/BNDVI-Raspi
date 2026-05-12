# BNDVI-RasPi

Plant-stress monitoring on a Raspberry Pi 4 using a Pi NoIR Camera v2 +
the bundled Rosco #2007 blue filter, with a small Flask web dashboard.

This is a ground-based mini-project preview of a UAV-based BNDVI
(Blue Normalised Difference Vegetation Index) workflow.

## How it works

The Pi NoIR sensor has no IR-cut filter, so it sees both visible and NIR.
The bundled Rosco #2007 ("Storaro Blue") gel **passes blue + NIR and
blocks red/green**, so the channels behave like this:

| Channel       | What it captures                       |
|---------------|----------------------------------------|
| Red Bayer     | NIR (red is blocked by the gel)        |
| Blue Bayer    | Visible blue + some NIR contamination  |

So `BNDVI = (NIR − Blue) / (NIR + Blue) = (R − B) / (R + B)`.

> Sanity check: pointing at healthy vegetation should produce a
> **pinkish/magenta** raw image, because plants reflect a lot of NIR
> which lands in the red channel.

### Optional: NIR-leakage correction

Blue Bayer pixels also pick up some NIR. To partially remove that
contamination we estimate visible blue as `max(ε, B − k·R)` before
computing BNDVI. Public Lab's Ned Horning uses **k = 0.8** as a default
for Pi NoIR + Rosco #2007; the practitioner range is 0.3–0.8.

This is opt-in (the dashboard's "Advanced: NIR-leakage correction"
disclosure, or `bndvi.py --correct-nir --k 0.8` on the CLI). See
[CALIBRATION.md](./CALIBRATION.md) for how to tune `k`.

## Hardware

- Raspberry Pi 4 Model B
- Pi NoIR Camera v2 with the bundled blue filter installed in the lens cap
- Power supply, microSD with Raspberry Pi OS (Bookworm)

## Setup on the Pi

```bash
cd bndvi_dashboard
sudo apt update
sudo apt install -y python3-picamera2 python3-pip libatlas-base-dev
pip install -r requirements.txt
```

Enable the camera (`sudo raspi-config` → Interface Options → Camera) and
reboot if needed. Test it:

```bash
python bndvi.py
```

That captures one frame and writes outputs to `./bndvi_output/`.

## Run the dashboard

```bash
cd bndvi_dashboard
python app.py --host 0.0.0.0 --port 5000
```

Open `http://<pi-ip>:5000/` from a laptop/phone on the same network.

### Dev mode (no Pi)

For developing the dashboard on a laptop without a camera:

```bash
cd bndvi_dashboard
python app.py --dev
```

This generates a synthetic Infrablue-looking frame so you can exercise
the full pipeline.

## What the dashboard does

- **Capture now** button — triggers the camera with locked AWB/exposure,
  runs BNDVI, saves four images (raw, heatmap, false-colour, thumbnail)
  plus statistics
- **Advanced disclosure** — toggle NIR-leakage correction and adjust `k`
- **Mean BNDVI over time** — line chart across all captures, useful when
  monitoring the same plant over days
- **Gallery** — every capture with thumbnail, healthy/moderate/stressed
  badge, and a vegetation-breakdown bar
- **Detail page** — raw / heatmap / false-colour side-by-side, full stats,
  capture settings (including the NIR-correction `k` used), label/notes
  editor, delete

## Calibration

The defaults in `bndvi.py` (gain 2.0, exposure 5000 μs, AWB/AE locked)
are a reasonable starting point for direct daylight, but you'll get much
better numbers if you tune them against a white reference card in your
actual lighting.

See **[CALIBRATION.md](./CALIBRATION.md)** for the full walkthrough:
what AWB/AE locking means, how to verify it's working, how to pick gain
and exposure, and how to tune the NIR-leakage coefficient `k`.

## File layout

```
.
├── README.md                     this file
├── CALIBRATION.md                calibration walkthrough
├── CLAUDE.md                     notes for future Claude Code sessions
├── Requirements.md               course assignment brief
├── ndvi_capture.old.py           archived original — REFERENCE ONLY,
│                                   has a channel-mapping bug, do not run
└── bndvi_dashboard/              project code
    ├── bndvi.py                  BNDVI capture + analysis (importable + CLI)
    ├── app.py                    Flask dashboard
    ├── requirements.txt          Python dependencies
    ├── templates/                Jinja templates (base, index, detail)
    ├── static/                   CSS + small JS (Chart.js from CDN)
    └── bndvi_output/             generated images + captures.json
                                    (created at runtime)
```

## Future work (UAV scaling)

- Tag each capture with GPS + altitude
- Replace the time-series with a spatial map (Leaflet) keyed on lat/lon
- Stitch overlapping captures into an orthomosaic before BNDVI
- Push captures off-Pi (S3 / sync directory) instead of storing locally

## References

- [What's that blue thing doing here? — Raspberry Pi blog](https://www.raspberrypi.com/news/whats-that-blue-thing-doing-here/)
- [Calibrating DIY NIR cameras — Ned Horning, Public Lab](https://publiclab.org/notes/nedhorning/10-21-2013/calibrating-diy-nir-cameras-part-1)
- [Raspberry NoIR + blue filter writeup — Public Lab](https://publiclab.org/notes/carolccarvalho/07-15-2016/raspberry-noir-cam-blue-filter)
- [Infragrammar — Public Lab](https://publiclab.org/notes/warren/08-17-2013/infragrammar-compositing-infrared-images-with-simple-mathematic-expressions)
- [Index Database: BNDVI](https://www.indexdatabase.de/db/i-single.php?id=135)
- [PYM: Plant Phenotyping with Raspberry Pi (Plant Methods, 2017)](https://plantmethods.biomedcentral.com/articles/10.1186/s13007-017-0248-5)
- [Low-cost NDVI imaging system (Plant Methods, 2023)](https://link.springer.com/article/10.1186/s13007-023-00981-8)
