# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## Project

**Hylocropter** — UAV-based multispectral imaging for plant-stress detection in
dragon fruit (*Hylocereus* spp.) farms. Raspberry Pi 4 + Pi NoIR Camera v2 +
Rosco #2007 ("Storaro Blue") gel, carried by an F450 quadcopter with a Pixhawk,
flying autonomous Mission Planner missions over a farm in Tanauan City, Batangas.
Everything runs offline on the Pi, which serves the dashboard from its own Wi-Fi
hotspot.

Group 8 · De La Salle Lipa · CpE Design and Practice 1. The course brief is in
`Requirements.md`; the thesis is a *proposal* (Chapters 1–2 only, no results), so
several things the code needs are unspecified — those are catalogued in
`RESEARCH-GAPS.md`, which is the first place to look when a number seems
arbitrary.

All runnable code lives in `hylocropter/`. The repo root holds prose docs and
`ndvi_capture.old.py` — the archived original, **reference only — do not run**;
see "Hardware-specific physics" below for why.

## Commands

All commands run from `hylocropter/`.

```bash
pip install -r requirements.txt

# One-off CLI capture. Outputs to ./hylocropter_data/ground/.
python bndvi.py
python bndvi.py --dev                    # synthetic frame, no camera
python bndvi.py --dev --scene soil       # healthy|mixed|stressed|soil
python bndvi.py --correct-nir --k 0.35   # opt-in NIR-leakage correction
python bndvi.py --save-array             # float32 BNDVI as .npz
python bndvi.py --raw                    # also save the Bayer frame as DNG

# Dashboard
python app.py                            # localhost
python app.py --host 0.0.0.0 --port 5000 # expose on the LAN (for the Pi)
python app.py --dev                      # synthetic frames (laptop dev)
python app.py --debug                    # Flask reloader on
```

There is **no test suite, linter, or build step** — this is a small student
project. Verification during development was a Playwright script (described at
the end of `DEPLOYMENT.md`); if you add tests, put them under `hylocropter/` and
note the runner here.

## Hardware-specific physics (do NOT get this wrong)

The Rosco #2007 blue filter **passes blue + NIR and blocks red/green**. Behind
the NoIR sensor (no IR-cut filter) the Bayer channels then map to:

| Channel        | What it actually captures             |
|----------------|---------------------------------------|
| Red Bayer (0)  | NIR (red light is blocked by the gel) |
| Green Bayer(1) | Mostly blocked (8–18% transmission) — unused |
| Blue Bayer (2) | Visible blue + some NIR contamination |

So `BNDVI = (NIR − Blue) / (NIR + Blue) = (R − B) / (R + B)`. Healthy plants
reflect lots of NIR, which lands in the **red** channel — a raw capture of
vegetation should look **pinkish/magenta**, not bluish.

The archived `ndvi_capture.old.py` at the repo root has this mapping **reversed**
(treats Blue=NIR, Red=visible). It is kept as a historical artefact for the
writeup only — **do not run it, do not copy its logic into new code**. If you
ever see `(B - R)/(B + R)` outside that archived file, the old bug is being
reintroduced — fix it.

### NIR-leakage correction, and the `k` problem

Blue Bayer pixels also pick up NIR. `compute_bndvi` has an opt-in mode that
estimates visible blue as `max(ε, B − k·R)` before the index.

`DEFAULT_NIR_LEAK_COEF = 0.8` **is** Ned Horning's — it's the hard-coded default
in Public Lab's PhotoMonitoringPlugin. But it is for a MidOpt DB660/850
narrowband *red* filter with the channels reversed, justified by red and blue
pixels having similar NIR sensitivity at 850 nm. The Rosco #2007 passes NIR
broadly from ~695 nm, where Horning himself notes red pixels are much more
NIR-sensitive — so the right `k` here is well below 0.8. Do not describe 0.8 as
a value for this rig (`RESEARCH-GAPS.md` §2).

The right answer is to measure it. A white reference must read BNDVI ≈ 0, so
`R = B − k·R` gives **`k = B/R − 1`** — implemented as `bndvi.solve_leak_coef()`
and wired to a drag-a-box gesture in the Debug view. Dev mode's synthetic frames
include a simulated white card and model leakage at `SYNTH_LEAK = 0.35`, so the
whole calibration flow is exercisable with no camera.

## Architecture

Flask + plain Jinja + vanilla JS. **No build step, no frontend framework, no
database.** Django was considered and rejected: an ORM, migrations and an admin
panel buy nothing for one user and a flat list of flights, and cost roughly twice
the memory on a Pi 4.

### Backend modules (`hylocropter/`)

- **`bndvi.py`** — index maths + camera capture, also a CLI. **Must stay
  standalone**: `python bndvi.py --dev` has to work with Flask absent. This is
  the project's minimal reproducer.
- **`camera.py`** — the *single owner* of the camera. One lock arbitrates the
  preview loop and full captures; picamera2 does not tolerate concurrent callers.
  A capture pauses the preview and resumes it afterwards.
- **`telemetry.py`** — pymavlink reader thread. **Read-only: it never arms the
  aircraft.** ⚠️ Has never run against real hardware — see `RESEARCH-GAPS.md` §7.
- **`flights.py`** — flight/capture store, map-grid binning, legacy migration.
- **`settings.py`** — persisted settings with clamping and validation.
- **`system.py`** — device actions. Destructive ones return a confirmation
  contract instead of acting; the route requires an explicit `confirm: true`.
- **`tiles.py`** — offline basemap prefetch, coverage reporting, fallback tile.
- **`applog.py`** — logging to a rotating file plus an in-memory ring the UI reads.
- **`app.py`** — routes only. Keep it thin.

### The live debug feed

The server sends **raw NIR and blue channel planes** (160×120, binary) from
`GET /api/preview/frame`. The browser derives BNDVI and paints all seven canvases.

This is deliberate and worth preserving: the `k`, threshold and correction
controls respond with **zero server round-trips**, and the four renders cannot
drift out of sync because they all come from one array. The Pi's per-frame work is
grab → downsample → send. It reuses the `lores` stream `capture_image()` always
configured and never read.

### Data model

Capture records keep all 8 original keys (`id`, `timestamp`, `label`, `notes`,
`files`, `stats`, `classification`, `settings`) so pre-Hylocropter records still
render. Additions are **additive**: `flight_id`, `geo`, `trigger`, `channels`,
`exposure_check`, `process_ms`, and new keys inside `settings`. Legacy records
have `flight_id: None` and show as "Ground captures".

Flights carry `bounds`, a `grid` (14×9 cells binned from capture positions), and
their own `thresholds`. **Empty grid cells stay `null`** and render transparent —
painting an unvisited cell mid-range would invent healthy ground the drone never
flew over, which is exactly the sort of thing a farmer would act on.

### Storage

JSON files, no database. Two fixes from the old version worth keeping: all index
mutation happens under one lock (the old code released the capture lock *before*
the read-append-write, so overlapping requests could lose a record), and writes
are temp-file + `os.replace()` — a power cut mid-write on a drone must not
truncate the index.

## Conventions

- **No backwards-compat with the deleted `ndvi_capture.py`.** It had the bug;
  don't re-add it or re-introduce its naming.
- **Keep `bndvi.py` runnable standalone.**
- **Settings recorded with each capture.** New capture options go in the
  `settings` dict and get rendered on the detail page, so old captures stay
  self-describing. Thresholds are per-capture for the same reason — changing them
  later must not rewrite what was measured.
- **Dev mode is first-class.** Synthetic frames flow through the exact same
  pipeline as real ones; don't branch around `render_outputs` or stats.
- **One colormap.** `BNDVI_COLOR_STOPS` in `bndvi.py` is the source of truth,
  mirrored in `static/js/colormap.js` and handed to matplotlib. There used to be
  three different mappings for the same data — don't add a fourth.
- **Nothing may reach the network at run time.** Leaflet and the fonts are
  vendored; tiles come off local disk. This is the thesis's central claim. If you
  add an asset, vendor it, and re-check with devtools.
- **`[hidden]` is load-bearing.** Every filter and toggle hides things with the
  `hidden` attribute, and component classes set `display`, which beats the UA
  rule. `tokens.css` has `[hidden] { display: none !important }` for this — don't
  remove it.
- **Every failure state must be visible in the UI.** No camera, no drone, no
  tiles, no GPS — each has a designed state saying what's wrong and what to do.
  The user's requirement is that they never need a terminal, so a silent failure
  is a bug.
- **UAV future-proofing.** When asked about extensions, default to additions that
  work for both ground and UAV (geotag fields, not a Pi-only stat).

## Things that are known-unfinished

Read `RESEARCH-GAPS.md` before assuming a number is meaningful. Briefly: `k = 0.8`
is unsourced; the BNDVI thresholds are generic rather than dragon-fruit values;
the MAVLink path has never seen real hardware; `static/tiles/` ships empty; and
the thesis contradicts itself on in-flight vs post-flight processing (this
implementation captures in flight and processes after landing).
