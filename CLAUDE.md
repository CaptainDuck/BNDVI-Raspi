# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Mini-project for a Raspberry Pi course: ground-based BNDVI plant-stress
monitoring on a Pi 4 Model B + Pi NoIR Camera v2 + the bundled Rosco
#2007 ("Storaro Blue") gel filter, with a small Flask dashboard. The
brief is in `Requirements.md`. The longer-term plan is to apply the same
methodology on a UAV over a farm, so suggestions should keep one eye on
that scale-up (GPS-tagged captures, spatial maps, orthomosaicing).

All runnable code lives in `bndvi_dashboard/`. The repo root holds the
project's prose docs (`README.md`, `CALIBRATION.md`, this file), the
course brief (`Requirements.md`), and `ndvi_capture.old.py` — the
archived original, **reference only — do not run**; see "Hardware-specific
physics" below for why.

## Commands

All commands run from `bndvi_dashboard/`.

```bash
pip install -r requirements.txt

# One-off CLI capture (Pi only). Outputs to ./bndvi_output/.
python bndvi.py
python bndvi.py --correct-nir --k 0.8    # opt-in NIR-leakage correction
python bndvi.py --dev                    # synthetic frame, no camera

# Dashboard
python app.py                            # localhost
python app.py --host 0.0.0.0 --port 5000 # expose on LAN (for the Pi)
python app.py --dev                      # synthetic frames (laptop dev)
python app.py --debug                    # Flask reloader on
```

There is **no test suite, linter, or build step** — this is a small
student project. If you add tests, put them under `bndvi_dashboard/` and
note the runner here.

## Hardware-specific physics (do NOT get this wrong)

The Rosco #2007 blue filter **passes blue + NIR and blocks red/green**.
Behind the NoIR sensor (no IR-cut filter) the Bayer channels then map to:

| Channel        | What it actually captures             |
|----------------|---------------------------------------|
| Red Bayer (0)  | NIR (red light is blocked by the gel) |
| Blue Bayer (2) | Visible blue + some NIR contamination |

So `BNDVI = (NIR − Blue) / (NIR + Blue) = (R − B) / (R + B)`. Healthy
plants reflect lots of NIR, which lands in the **red** channel — a raw
capture of vegetation should look **pinkish/magenta**, not bluish.

The archived `ndvi_capture.old.py` at the repo root has this mapping
**reversed** (treats Blue=NIR, Red=visible). It is kept around as a
historical artefact for the writeup only — **do not run it, do not copy
its logic into new code**. If you ever see `(B - R)/(B + R)` outside
that archived file, the old bug is being reintroduced — fix it.

### Optional NIR-leakage correction

Blue Bayer pixels also pick up some NIR. `compute_bndvi` has an opt-in
mode that estimates visible blue as `max(ε, B − k·R)` before the index.
Default `k = 0.8` (Public Lab / Ned Horning's value for this exact rig).
Practitioner range is 0.3–0.8; ideally calibrate against a white
reference under sunlight.

## Architecture

Two Python files, with a clear seam between them.

**`bndvi.py`** — self-contained capture + analysis library, also runnable
as a CLI. Pipeline:

1. `capture_image()` — picamera2 (preferred) → legacy picamera fallback →
   synthetic dev frame if `dev_mode=True`. AWB and AE are **locked** to
   fixed exposure/gain; auto-WB would destroy the channel ratios BNDVI
   depends on. Do not re-enable AWB.
2. `compute_bndvi(rgb, correct_nir_leakage, nir_leak_coef)` — the index.
3. `bndvi_to_rgb(bndvi)` — false-colour map (red→yellow→green stops).
4. `render_outputs(...)` — saves four files per capture (raw jpg,
   matplotlib heatmap png, false-colour png, thumbnail jpg).
5. `capture_and_analyse(...)` — orchestrates 1–4, returns a metadata
   record (id, timestamp, files, stats, classification, settings).

**`app.py`** — thin Flask app. State is just `bndvi_output/captures.json`,
a list of those metadata records. No database. Routes split into HTML
(`/`, `/capture/<id>`) and JSON API (`/api/captures` GET/POST,
`/api/captures/<id>` GET/PATCH/DELETE). A module-level
`_capture_lock` serialises capture requests — picamera2 does not handle
concurrent callers. The lock is non-blocking (returns 409 on contention).

**Capture IDs** are `YYYYMMDD_HHMMSS` strings and double as the
filename suffix for all artefacts (`raw_<id>.jpg`, `heatmap_<id>.png`,
etc.). The dashboard finds files by looking them up in the index, not by
globbing the directory.

**Frontend** is plain Jinja + a tiny JS file per page. Chart.js loaded
from CDN (only used on the index page). No build step, no framework.

## Conventions

- **No backwards-compat with the deleted `ndvi_capture.py`.** It had the
  bug; don't re-add it or re-introduce its naming.
- **Keep `bndvi.py` runnable standalone.** It's the demo's "minimal
  reproducer" — `python bndvi.py --dev` should always work without Flask.
- **Settings recorded with each capture.** When adding new capture
  options, store them in the `settings` dict on the record and render
  them on the detail page so old captures stay self-describing.
- **Dev mode is first-class.** Synthetic frames have to flow through the
  exact same pipeline as real ones; don't branch around `render_outputs`
  or stats.
- **UAV future-proofing.** When the user asks about extensions, default
  to additions that work for both ground and UAV (e.g. geotag fields,
  not a Pi-only stat).