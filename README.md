# Hylocropter

UAV-based multispectral imaging for plant-stress detection in dragon fruit
(*Hylocereus* spp.) farms, running entirely offline on a Raspberry Pi.

A Pi 4 + Pi NoIR Camera v2 + Rosco #2007 blue gel rides on an F450 quadcopter
with a Pixhawk. During an autonomous Mission Planner flight the Pi captures
GPS-tagged frames, computes BNDVI per pixel, and after landing serves a farm
stress map from its own Wi-Fi hotspot — no internet at any point.

Group 8 · Dimayuga, Ilag, Virtucio · De La Salle Lipa · CpE Design and Practice 1

## How it works

The Pi NoIR sensor has no IR-cut filter, so it sees both visible and NIR. The
bundled Rosco #2007 ("Storaro Blue") gel **passes blue + NIR and blocks
red/green**, so the channels stop meaning what their names say:

| Channel       | What it actually captures              |
|---------------|----------------------------------------|
| Red Bayer     | NIR (red is blocked by the gel)        |
| Green Bayer   | Mostly blocked — unused by the index   |
| Blue Bayer    | Visible blue + some NIR contamination  |

So `BNDVI = (NIR − Blue) / (NIR + Blue) = (R − B) / (R + B)`.

Healthy leaves reflect NIR strongly and absorb blue, so stress shows up as
BNDVI falling.

> **Sanity check:** pointing at healthy vegetation must produce a
> **pinkish/magenta** raw image, because plants reflect a lot of NIR and that
> lands in the red channel. If it looks like a normal colour photo, auto white
> balance is still on and every number is meaningless.

### NIR-leakage correction

Blue Bayer pixels also pick up NIR. To remove that contamination we estimate
visible blue as `max(ε, B − k·R)` before computing the index.

`k = 0.8` is widely quoted for this rig, but **that value's provenance could not
be verified and it does not appear in the thesis** — see
[RESEARCH-GAPS.md](./RESEARCH-GAPS.md) §2. Don't trust it: measure your own.
Open **Debug**, put a white card in frame, drag a box over it, and the dashboard
solves `k = B/R − 1` directly, because a white reference must read BNDVI ≈ 0.

## The dashboard

Seven views, all driveable from a phone on the Pi's hotspot. The design goal was
that you never need a terminal.

| View | What it does |
|---|---|
| **Farm map** | Satellite basemap with the flight's BNDVI overlay, a three-band legend, per-cell hover readout, plain-language summary, trend against previous flights, and the flight's photos |
| **New flight** | Pre-flight checklist (camera, storage, MAVLink, GPS fix, mission), mission details read live off the Pixhawk, trigger source, then live telemetry while recording |
| **Processing** | Post-landing progress: aggregate, map, save |
| **All flights** | Every flight, grouped by month, searchable and filterable by block |
| **Photo detail** | False colour / heatmap / raw, statistics, channel means, capture settings, GPS |
| **Debug** | Live feed with four synchronised renders + channel split + histogram, every calibration knob as a live control, white-card calibration, and the device log |
| **Settings** | Persisted camera values, thresholds, offline-map download and coverage, MAVLink connection, device actions, activity log |

The debug view computes BNDVI **in the browser** from raw channel planes the Pi
sends, so moving the `k` or threshold sliders repaints all seven canvases with no
server round-trip — and the renders can't drift out of sync, because they all
come from one array.

## Hardware

- Raspberry Pi 4 Model B
- Pi NoIR Camera v2 with the Rosco #2007 gel in the lens cap
- F450 quadcopter, Pixhawk (2.4.8 or 4) + GPS, 2212 920KV motors, 30 A ESCs
- 3S 11.1 V 5200 mAh LiPo, 5 V/3 A UBEC for the Pi
- microSD with Raspberry Pi OS (Bookworm)

See **[HARDWARE.md](./HARDWARE.md)** for the full bill of materials, wiring, and
how to mount the gel without damaging the lens.

## Setup

```bash
cd hylocropter
sudo apt update
sudo apt install -y python3-picamera2 python3-pip libatlas-base-dev
pip install -r requirements.txt
```

Enable the camera (`sudo raspi-config` → Interface Options → Camera), reboot if
needed, then test the capture path on its own:

```bash
python bndvi.py                    # one capture, writes to ./hylocropter_data/ground/
python bndvi.py --dev              # synthetic frame, no camera needed
python bndvi.py --dev --scene soil # pick a synthetic scene
python bndvi.py --correct-nir --k 0.35
python bndvi.py --save-array       # also write the float32 BNDVI as .npz
python bndvi.py --raw              # also save the unprocessed Bayer frame as DNG
```

`bndvi.py` has no Flask dependency and is the project's minimal reproducer —
`python bndvi.py --dev` should always work.

## Run the dashboard

```bash
cd hylocropter
python app.py --host 0.0.0.0 --port 5000     # on the Pi
python app.py --dev                          # laptop, synthetic frames
python app.py --debug                        # Flask reloader
```

Open `http://<pi-ip>:5000/`, or `http://hylocropter.local:5000/` once the Pi is
set up as an access point (see [DEPLOYMENT.md](./DEPLOYMENT.md)).

**Dev mode is first-class.** Synthetic frames model NIR leaking into the blue
channel and include a simulated white reference card, so the entire calibration
workflow — including solving `k` — is exercisable with no camera attached.

## Offline operation

Nothing on the page reaches the network at run time. Leaflet and both fonts are
vendored into `hylocropter/static/`. The satellite basemap has to be downloaded
once: **Settings → Offline map**, which shows the tile count and size before you
commit and then reports exactly how far coverage extends.

For a 620 m box (38 ha, comfortable around a 5–10 ha plot) at zoom 16–19 that's
about 138 tiles / 2.4 MB. `static/tiles/` ships empty — see
[RESEARCH-GAPS.md](./RESEARCH-GAPS.md) §8.

## Calibration

The defaults (gain 2.0, exposure 5000 µs, AWB/AE locked) are a reasonable start
for direct daylight, but the numbers only mean something once tuned against a
white reference in your actual light.

**[CALIBRATION.md](./CALIBRATION.md)** walks through it. All of it is now doable
from the Debug view rather than a text editor.

## Known gaps

**[RESEARCH-GAPS.md](./RESEARCH-GAPS.md)** lists what could not be verified and
how to close each item — including the unsourced `k = 0.8`, the absence of
dragon-fruit-specific thresholds, two picamera2 bugs that could silently defeat
the AWB/AE lock, and the fact that the MAVLink code has never been run against
real hardware.

## File layout

```
.
├── README.md  HARDWARE.md  CALIBRATION.md  DEPLOYMENT.md
├── RESEARCH-GAPS.md              open questions and unverified claims
├── CLAUDE.md                     notes for future Claude Code sessions
├── Requirements.md               course assignment brief
├── ndvi_capture.old.py           archived original — REFERENCE ONLY,
│                                   has a channel-mapping bug, do not run
└── hylocropter/
    ├── bndvi.py                  index maths + camera capture (importable + CLI)
    ├── camera.py                 single owner of the camera; preview loop
    ├── telemetry.py              MAVLink reader (UNVERIFIED — see gaps §7)
    ├── flights.py                flight/capture store, map grid, migration
    ├── settings.py               persisted settings
    ├── system.py                 device actions (USB copy, shutdown, …)
    ├── tiles.py                  offline basemap prefetch + coverage
    ├── applog.py                 logging + the in-UI log buffer
    ├── app.py                    Flask routes
    ├── templates/                Jinja, one file per view
    ├── static/
    │   ├── css/  js/             no build step, no framework
    │   ├── vendor/leaflet/       vendored for offline use
    │   ├── fonts/                Caprasimo + Figtree, local
    │   └── tiles/                offline basemap (download in Settings)
    └── hylocropter_data/         runtime state (created on first run)
```

There is **no test suite or linter** — this is a student project. The Playwright
verification used during development is described in DEPLOYMENT.md.

## References

- [What's that blue thing doing here? — Raspberry Pi blog](https://www.raspberrypi.com/news/whats-that-blue-thing-doing-here/)
- [Calibrating DIY NIR cameras — Ned Horning, Public Lab](https://publiclab.org/notes/nedhorning/10-21-2013/calibrating-diy-nir-cameras-part-1)
- [Raspberry NoIR + blue filter writeup — Public Lab](https://publiclab.org/notes/carolccarvalho/07-15-2016/raspberry-noir-cam-blue-filter)
- [Index Database: BNDVI](https://www.indexdatabase.de/db/i-single.php?id=135)
- [ArduPilot camera triggering (`CAM_TRIGG_DIST`)](https://ardupilot.org/copter/docs/common-camera-shutter-with-servo.html)
- [picamera2 examples](https://github.com/raspberrypi/picamera2/tree/main/examples)
