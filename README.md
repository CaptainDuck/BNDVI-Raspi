# Hylocropter

UAV-based multispectral imaging for plant-stress detection in dragon fruit
(*Hylocereus* spp.) farms, running entirely offline on a Raspberry Pi.

A Pi 4 + Pi NoIR Camera v2 + Rosco #2007 blue gel rides on an F450 quadcopter
with a Pixhawk. During an autonomous Mission Planner flight the Pi captures
GPS-tagged frames, computes BNDVI per pixel, and after landing serves a farm
stress map from its own Wi-Fi hotspot — no internet at any point.

Group 8 · Dimayuga, Ilag, Virtucio · De La Salle Lipa · CpE Design and Practice 1

## How it works

> **The little blue square in your Pi NoIR box *is* the Rosco #2007.** Raspberry
> Pi's own announcement says so: *"There's a little square of blue gel in there.
> What's it for? … Our friend **Roscolux #2007 Storaro Blue** (that's the blue
> thing's full name) … we buy it on giant reels and the guys at the factory in
> Wales … cut it up into little squares for you to use."* Same object, two names —
> nothing to buy.

The Pi NoIR sensor has no IR-cut filter, so it sees both visible and NIR. That
gel passes blue and NIR while blocking red, so the channels stop meaning what
their names say:

| Channel       | What it actually captures              |
|---------------|----------------------------------------|
| Red Bayer     | NIR (red is blocked by the gel)        |
| Green Bayer   | Mostly blocked — unused by the index   |
| Blue Bayer    | Visible blue + some NIR contamination  |

So `BNDVI = (NIR − Blue) / (NIR + Blue) = (R − B) / (R + B)`.

Healthy leaves reflect NIR strongly and absorb blue, so stress shows up as
BNDVI falling.

From Rosco's own Cinegel data sheet for #2007 (10% overall transmission, −3.3
stops), the numbers behind that table:

| Band | Transmission |
|---|---|
| Blue peak | **53%** at 420–440 nm |
| Green | 18% at 500 nm falling to 8% at 560 nm — leaks, not blocked |
| Red minimum | **2%** at 640–660 nm |
| NIR cut-on | 4% at 680 nm → 15% at 700 → 42% at 720 → **67% at 740** and still rising |

Two things worth noting. Green is only *mostly* blocked — at ~1/5 of peak blue
it's a real if unused leak. And the data sheet stops at 740 nm, so it doesn't
characterise the 750–900 nm range where the silicon collects most of its NIR;
that's why the leakage coefficient has to be measured rather than derived.

> **Sanity check:** pointing at healthy vegetation must produce a
> **pinkish/magenta** raw image, because plants reflect a lot of NIR and that
> lands in the red channel. If it looks like a normal colour photo, auto white
> balance is still on and every number is meaningless.

### NIR-leakage correction

Blue Bayer pixels also pick up NIR. To remove that contamination we estimate
visible blue as `max(ε, B − k·R)` before computing the index.

That form is Ned Horning's, from Public Lab's
[PhotoMonitoringPlugin](https://publiclab.org/notes/nedhorning/07-22-2015/introducing-the-calibration-plugin-for-imagej-fiji)
— "subtract a percentage of the NIR pixel values from the visible pixel values"
— and `k = 0.8` is that plugin's hard-coded default.

**But 0.8 is not a value for this rig.** Horning's 80% is for a MidOpt DB660/850
narrowband *red* filter with the channels the other way round, and he justifies
it by that filter "centering the NIR band at 850nm where the sensitivity of the
red detectors is roughly the same as the blue detectors". The Rosco #2007 passes
a broad NIR band from ~695 nm, and Horning says of exactly that case that "the
red detectors in the camera sensor are much more sensitive to the shorter NIR
wavelengths" — so `k` here should be **well below 0.8**.

So measure your own. Open **Debug**, put a white card in frame, drag a box over
it, and the dashboard solves `k = B/R − 1` from requiring BNDVI ≈ 0 on white.
That's a shortcut rather than a calibration — see
[RESEARCH-GAPS.md](./RESEARCH-GAPS.md) §2 for its biases and what Public Lab
actually prescribes instead.

## The dashboard

Nine views, all driveable from a phone on the Pi's hotspot. The design goal was
that you never need a terminal.

| View | What it does |
|---|---|
| **Set up the camera** | Guided calibration walkthrough — see below |
| **Farm map** | Satellite basemap with four ways to see a flight — every photo drawn at the ground it actually covers, an averaged grid, three bands, or imagery alone — plus photo pins, the flight track, a per-cell hover readout, plain-language summary and trend. Also where you draw and name each plot you fly, corner to corner on the imagery |
| **Plan a flight** | The mission planner on its own — altitude, photo spacing, photo count, flight time and storage for any area. Needs no camera, no drone and no map, so you can work the numbers out at a desk and rehearse over a car park first |
| **New flight** | Pre-flight checklist (camera, storage, MAVLink, GPS fix, mission), mission details read live off the Pixhawk, a mission planner that turns an altitude into the two numbers Mission Planner needs, trigger source, then live telemetry while recording |
| **Processing** | Post-landing progress: aggregate, map, save |
| **All flights** | Every flight, grouped by month, searchable and filterable by the block names you drew on the map |
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
sudo apt update
sudo apt install -y python3-picamera2 python3-pip libatlas-base-dev
pip install -r hylocropter/requirements.txt
```

Enable the camera (`sudo raspi-config` → Interface Options → Camera), reboot if
needed, then test the capture path on its own:

```bash
python hylocropter/bndvi.py            # one capture
python hylocropter/bndvi.py --dev      # synthetic frame, no camera needed
python hylocropter/bndvi.py --dev --scene soil
python hylocropter/bndvi.py --correct-nir --k 0.35
python hylocropter/bndvi.py --save-array   # float32 BNDVI as .npz
python hylocropter/bndvi.py --raw          # also save the Bayer frame as DNG
```

**Working directory doesn't matter.** Both scripts resolve their paths relative to
their own file, so you can run them from the repo root, from inside
`hylocropter/`, or by absolute path — output always lands in
`hylocropter/hylocropter_data/`. Use `-o` on `bndvi.py` to put it elsewhere.

`bndvi.py` has no Flask dependency and is the project's minimal reproducer —
`python bndvi.py --dev` should always work.

## Run the dashboard

```bash
python hylocropter/app.py --host 0.0.0.0 --port 5000   # on the Pi
python hylocropter/app.py --dev                        # laptop, synthetic frames
python hylocropter/app.py --debug                      # Flask reloader
```

(`cd hylocropter && python app.py …` works identically.)

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

Imagery for the Tanauan **vicinity** is already downloaded and committed: **138
tiles, 1.9 MB, zoom 16–19**, covering 54.9 ha at up to 29 cm per pixel.

That 54.9 ha is deliberately much larger than anything the drone flies. It is
ground to *search*, because the farm's exact outline near Vis Compound isn't known
yet. Two things, kept separate everywhere in the UI:

| | What it is | Where you set it |
|---|---|---|
| **Vicinity** | The imagery on the Pi's disk. Tens of hectares, one box. | Settings → Offline map |
| **Survey blocks** | The plots the drone actually flies. A *list* of named rectangles. | Farm map → *Add a block* |

Open the farm map, pan around until you recognise the dragon fruit rows, then
click one corner of a plot and the opposite corner, and name it. Repeat for each
plot — a farm has several, and none of them are square, so blocks are rectangles
drawn corner to corner rather than a centre and a radius.

The names you give them are the choices on the **All flights** filter, so there is
one list to manage rather than two that drift apart. Everything the mission
planner says — altitude, photo spacing, photo count, flight time, storage — is
scaled to whichever block you pick. With nothing drawn it plans against a
placeholder square and says so.

If the farm turns out to lie outside the downloaded imagery, move the vicinity
centre in Settings and download again.

## Planning the mission

**Plan a flight** (or the same card on New flight) works the two numbers out from
the lens geometry (62.2° × 48.8°, so `2·h·tan(fov/2)`) rather than a rule of thumb:

| Altitude | Each photo covers | `CAM_TRIGG_DIST` | Line spacing | Detail |
|---|---|---|---|---|
| 8 m | 9.7 × 7.3 m | 4.4 m | 6.8 m | 0.29 cm/px |
| **12 m** | **14.5 × 10.9 m** | **6.5 m** | **10.1 m** | **0.44 cm/px** |
| 20 m | 24.1 × 18.1 m | 10.9 m | 16.9 m | 0.73 cm/px |
| 30 m | 36.2 × 27.2 m | 16.3 m | 25.3 m | 1.10 cm/px |

At 40% overlap along the line and 30% between lines. Those defaults are modest on
purpose: photos are placed by telemetry, not stitched into a true orthomosaic, so
they need only enough overlap to survive GPS wander. Raise both to 70–80% if you
ever switch to real photogrammetry.

**Flight lines run along each block's longer side.** Every turn costs battery and
altitude hold, so a 200 × 60 m strip flown the long way is 6 lines and 5 turns;
flown the short way it is 20 lines and 19 turns for exactly the same ground. The
planner tells you which way round to set the survey grid.

The card also warns when a plan doesn't fit a battery. A 1 ha block at 12 m is
170 photos and about 6 minutes; the whole 54.9 ha vicinity would be 1600 photos
and an hour, which is five packs — hence blocks being separate from the vicinity,
and being a list rather than one big box.

### Planning before you go to the farm

You don't need a drawn block, a camera, a flight controller or the offline map to
plan — it's trigonometry. **Plan a flight** is the planner on its own, with a set
of practice areas so you can rehearse somewhere you can walk to:

| Practice area | Size | At 12 m |
|---|---|---|
| Basketball court | 28 × 15 m | 12 photos, under a minute |
| Yard or car park | 40 × 30 m | 32 photos, ~1.5 min |
| **Football field** | 105 × 68 m | **126 photos, 4.4 min** |
| One hectare | 100 × 100 m | 170 photos, ~6 min |

All four fit inside one 3S pack on purpose — a rehearsal you can't finish teaches
you nothing. Fly the same altitude and trigger distance you plan to use at the
farm, then **compare the photo count you actually got against the number the page
predicted.** If they disagree, the trigger isn't firing the way you think, and
that is much better to discover at school than in Tanauan.

The page remembers your last plan, and prints, so you can take the numbers with
you.

## Calibration

**Start with the guided walkthrough: open the dashboard and go to “Set up the
camera”.** Nine pages that explain what the blue filter does and then check each
thing that needs calibrating against the live feed — including the two failures
you cannot spot by eye:

1. **How it works** — what the gel does, with Rosco's transmission numbers
2. **Camera found** — is the ribbon cable seated
3. **Filter fitted** — is the gel actually there? Measured off the green channel
4. **Settings locked** — why the camera has to stop being clever
5. **Exposure set** — drag a box on a white card, hit the 180–230 target
6. **Leakage measured** — solves your own `k` from the same box
7. **Bands chosen** — where green becomes yellow becomes red
8. **Plant checked** — point at a healthy plant, confirm +0.3 to +0.7
9. **Farm map** — offline imagery, marking the block, and the drone link

It runs on synthetic frames too, so you can walk the whole flow before you're at
the rig. Resumable, and re-runnable from Settings.

**[CALIBRATION.md](./CALIBRATION.md)** is the same material in prose, for when you
want the reasoning rather than the wizard. Nothing needs a text editor.

## Known gaps

**[RESEARCH-GAPS.md](./RESEARCH-GAPS.md)** tracks what is and isn't verified,
with how to close each remaining item.

Resolved there, and useful for the writeup: the correct provenance and limits of
the `B − k·R` correction, the full Rosco #2007 transmission table, and a real
citation for the 0.3/0.1 thresholds.

Still open: no dragon-fruit-specific thresholds (that's your Objective 4
validation and the highest-value fieldwork left), two picamera2 bugs that could
silently defeat the AWB/AE lock, and the MAVLink path never having run against
real hardware.

## File layout

```
.
├── README.md  HARDWARE.md  CALIBRATION.md  DEPLOYMENT.md
├── RESEARCH-GAPS.md              open questions and unverified claims
├── CLAUDE.md                     notes for future Claude Code sessions
├── Requirements.md               course assignment brief
├── pytest.ini                    test config (runs from anywhere)
├── .github/workflows/verify.yml  CI: the whole suite in one job
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
    ├── tests/                    pytest — index maths, mapping, store, routes
    ├── templates/                Jinja, one file per view (setup.html is the
    │                               guided calibration walkthrough)
    ├── static/
    │   ├── css/                  tokens + layout, no framework
    │   ├── js/                   feed.js is the shared live-feed engine
    │   ├── vendor/leaflet/       vendored for offline use
    │   ├── fonts/                Caprasimo + Figtree, local
    │   └── tiles/                offline basemap (download in Settings)
    └── hylocropter_data/         runtime state (created on first run)
```

## Tests

```bash
pip install -r hylocropter/requirements-dev.txt
pytest
```

195 tests, about 10 seconds, no hardware needed:

| File | What it pins down |
|---|---|
| `test_index.py` | The plant-health maths. That NIR is the **red** channel and healthy foliage reads positive; that the bands have no gap or overlap at the thresholds; that a `k` solved off a white card actually zeroes that card; that the gel-missing check catches a missing gel; and that `colormap.js` still matches `BNDVI_COLOR_STOPS` |
| `test_mapping.py` | Photo → footprint → bounds → grid → mission plan, plus the block rectangles: corners sorted whichever way they were clicked, lines along the longer axis, and the two rules that keep the map honest — **unvisited cells stay `null`**, and **row 0 is the northern edge** |
| `test_store.py` | The JSON indexes under concurrent writes, legacy migration, settings clamping, and the survey blocks — validation, de-duplication, and migrating the old single square into one |
| `test_routes.py` | Every page renders with no camera and no drone, and `/api/*` errors return JSON |

`.github/workflows/verify.yml` runs all of that in **one job**, plus four things
pytest can't: `bndvi.py` in a venv with Flask absent, the working directory
staying irrelevant, the app booting through `__main__`, and greps for a CDN
reference or a reintroduced `(B − R)/(B + R)`.

**What CI cannot prove:** the camera path and the MAVLink path. Those still need a
bench run — see DEPLOYMENT.md and RESEARCH-GAPS.md §7. UI behaviour is checked by
hand with the Playwright script described in DEPLOYMENT.md.

## References

- [What's that blue thing doing here? — Raspberry Pi blog](https://www.raspberrypi.com/news/whats-that-blue-thing-doing-here/)
- [Calibrating DIY NIR cameras — Ned Horning, Public Lab](https://publiclab.org/notes/nedhorning/10-21-2013/calibrating-diy-nir-cameras-part-1)
- [Raspberry NoIR + blue filter writeup — Public Lab](https://publiclab.org/notes/carolccarvalho/07-15-2016/raspberry-noir-cam-blue-filter)
- [Index Database: BNDVI](https://www.indexdatabase.de/db/i-single.php?id=135)
- [ArduPilot camera triggering (`CAM_TRIGG_DIST`)](https://ardupilot.org/copter/docs/common-camera-shutter-with-servo.html)
- [picamera2 examples](https://github.com/raspberrypi/picamera2/tree/main/examples)
