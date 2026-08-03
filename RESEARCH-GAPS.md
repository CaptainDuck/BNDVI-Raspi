# Research Gaps & Open Questions

Things I could **not** verify while building the Hylocropter dashboard, why, and what
would close each gap. Written so you can pick these up yourself — most need either a
browser on an unrestricted network, or ten minutes with the actual rig.

Each item is tagged with how much it matters:

- 🔴 **Blocks correctness** — the numbers may be wrong until this is resolved
- 🟠 **Affects quality** — the system works, but could be meaningfully better
- 🟡 **Documentation only** — needed for the paper/defense, not for the code

---

## 1. Sources I was blocked from reading

The environment I was working in restricts outbound HTTPS by policy. Every one of the
authoritative practitioner sources for this exact camera rig returned `403` at the
proxy's CONNECT stage:

| Source | Why it matters |
|---|---|
| `publiclab.org` — Ned Horning's "Calibrating DIY NIR cameras" | the origin of the NIR-leakage correction method and the `k` value we use |
| `publiclab.org` — "Raspberry NoIR cam + blue filter" | the closest thing to a reference implementation of this exact rig |
| `us.rosco.com` | the official Rosco #2007 spectral transmission curve |
| `www.raspberrypi.com` — "What's that blue thing doing here?" | the official guidance on the bundled filter |
| `pip.raspberrypi.com` | the picamera2 manual PDF (the authoritative control reference) |
| `arxiv.org`, `mdpi.com`, `link.springer.com` | the academic literature on single-sensor NDVI |
| every satellite tile server (Esri, OSM, Carto, Mapbox, Bing) | offline basemap imagery |

What **did** work: `pypi.org`, `registry.npmjs.org`, `fonts.gstatic.com`,
`raw.githubusercontent.com`, and web search (which returns titles and snippets, not full
pages). So the picamera2 code patterns in this repo are verified against the official
`raspberrypi/picamera2` GitHub examples — but nothing from the list above is.

**To close:** open those URLs in a normal browser. They're all public. The two Public Lab
notes are the important ones.

---

## 2. 🔴 The NIR-leakage coefficient `k = 0.8` has no verified provenance

This repo says, in `bndvi.py`, `README.md` and `CALIBRATION.md`, that `k = 0.8` is
"Public Lab / Ned Horning's value for this exact rig", with a practitioner range of
0.3–0.8.

**I could not verify any part of that claim.** The Public Lab pages are blocked, and web
search surfaced no source for either the specific `vis_blue = B − k·R` formulation or for
`k = 0.8`. It also appears **nowhere in your thesis** — the paper proposes no leakage
correction at all.

So right now `k = 0.8` is an unsourced magic number sitting in three files and being
presented to the reader as a citation. That's the kind of thing that gets asked about in
a defense.

**Three ways to close it, best first:**

1. **Measure it.** This is now a one-click operation in the dashboard and it beats any
   citation. A white reference reflects roughly equally across NIR and visible, so it
   should read BNDVI ≈ 0. Setting `R = B − k·R` and solving gives:

   ```
   k = B/R − 1
   ```

   Put white paper in the frame, open **Debug**, drag a box over the paper, and the
   dashboard computes `k` from the mean R and B inside it. That's *your* `k`, for *your*
   gel, sensor and lighting — strictly better than a number off the internet.
   *(If it comes out negative, no positive `k` can help; that means the red channel is
   over-responding and you should lower exposure or gain instead. The UI says so.)*
2. Read Horning's Public Lab note and either confirm the value or correct the attribution.
3. If neither, weaken the wording in the docs from a citation to "a commonly quoted
   starting value" and lean on the measured number.

---

## 3. 🟠 Rosco #2007 spectral transmission curve

We assert the gel "passes blue (~400–500 nm) and NIR (>700 nm), blocks red and green".
Directionally this is corroborated by multiple independent search snippets and it's
consistent with what the rig actually produces, but **I have no curve and no numbers** —
no cut-on/cut-off wavelengths, no transmission percentages. One snippet mentioned
"transmission of 10%" without context.

This matters beyond documentation: the shape of the blue passband determines how much
NIR the blue channel picks up, which is exactly what `k` corrects for. A curve would let
you *predict* `k` rather than measure it.

**To close:** Rosco publishes spectral data per gel on their site (blocked here). Search
"Rosco Roscolux 2007 Storaro Blue spectral energy distribution". Drop the curve into
`HARDWARE.md` — it's a good figure for the paper.

---

## 4. 🟠 Two picamera2 bugs that could silently invalidate every capture

Search surfaced these two open issues in `raspberrypi/picamera2`. I could only see the
titles, not the threads:

- **#1269 — "[BUG] AeEnable control parameter is not respected"**
- **#825 — "[BUG] color different (AWB, gain) not working"**

Both land directly on this project's foundation. The entire premise of BNDVI here is that
AWB and AE are **locked**; if either lock is silently ineffective on your Bookworm build,
the camera is quietly re-gaining the channels and every number in every flight is
meaningless — with no visible symptom.

**To close, two parts:**

1. Read the issues, check whether they affect your picamera2 version.
2. **Test it on the rig regardless**, because this is cheap and definitive: point the
   camera at a fixed scene, open **Debug**, and change the *scene brightness* (shade the
   subject with your hand, or wait for a cloud). With AE/AWB genuinely locked, the
   histogram should shift bodily brighter/darker but the **BNDVI mean should barely
   move**. If the BNDVI mean stays pinned while the raw image brightness also stays
   pinned, the camera is still auto-adjusting and the lock failed.

Related, and already applied in the code: the official picamera2 examples pass fixed
controls into `create_still_configuration(controls=...)` rather than calling
`set_controls()` after `start()` and hoping a 3-second sleep is enough. The old code did
the latter. Worth knowing which pattern your version actually honours.

---

## 5. 🔴 BNDVI thresholds have no empirical basis for dragon fruit

The code classifies `> 0.3` healthy, `0.1–0.3` moderate, `< 0.1` stressed. Your thesis
defines the three **classes** (green / "latent stress" / red) but **never states a single
numeric threshold**. The only numbers anywhere are Table 1's five illustrative sample
rows — which are labelled as samples, not measurements.

Interestingly, Table 1 is *roughly* consistent with the code's defaults (its "healthy
vegetation" rows are 0.30 and 0.38; "slightly stressed" is 0.03; "water stress" is
−0.33), so the defaults aren't unreasonable. But nothing in either the code or the paper
derives them, and they're almost certainly not right for *Hylocereus* — dragon fruit is a
cactus with thick waxy cladodes, whose NIR/blue reflectance is not going to behave like
the leafy crops these generic NDVI thresholds come from.

**To close — this is your Objective 4 validation, and it's the highest-value fieldwork:**

The dashboard now makes this straightforward. Every capture stores its full float32 BNDVI
array, and thresholds are editable live with instant recolouring.

1. Capture a set of plants you have **visually classified on the ground** — clearly
   healthy, clearly stem-cankered, and some in between. Note which is which.
2. Read each one's mean BNDVI from the capture detail page.
3. Pick the thresholds that best separate your own healthy and stressed groups, and set
   them in **Settings**. Drag the sliders in Debug to see the boundary move over a real
   frame.
4. Write the derived numbers into the paper with the sample size. *That* is a result, and
   right now the paper has none.

Note that healthy BNDVI on an uncorrected Infrablue rig runs much lower than true NDVI —
Table 1's healthy values of 0.30–0.38 are consistent with that, so don't expect the
+0.7–0.9 you'd see from a proper red/NIR sensor.

---

## 6. 🟡 IMX219 spectral response / quantum efficiency

Sony does not publish QE curves for the IMX219 publicly, and I found nothing usable.
Without it, the exact split of NIR between the red and blue Bayer pixels can't be derived
from first principles — which is the other half of why `k` has to be measured rather than
calculated.

**To close:** probably can't be, cleanly. Some third parties have published measured
IMX219 response curves; worth a search, but treat anything you find with care. Not a
blocker — the empirical white-reference method sidesteps it entirely.

---

## 7. 🔴 The MAVLink integration is written but has never been run

You chose real pymavlink over a simulated interface, which I've implemented — but there
was no flight controller and no SITL in this environment, so **not one line of the
telemetry path has executed against a real MAVLink stream.**

What I *did* verify: the dashboard boots, serves every page, and runs the debug view with
no flight controller attached, showing the "Drone not connected" state correctly. Nothing
hangs or crashes. So the failure mode is safe.

What is unverified: message field names and scaling, the mission read-back, arm/disarm
flight detection, and capture-on-`CAMERA_TRIGGER`.

**To close, cheapest path first:**

1. **ArduPilot SITL over UDP** — no hardware needed at all. Run SITL on a laptop, set the
   connection string in **Settings** to `udp:127.0.0.1:14550`, and the whole telemetry
   path becomes testable at a desk. Do this before touching the drone.
2. Then the real Pixhawk on `/dev/ttyAMA0` at 57600.
3. Then a trigger test: set `CAM_TRIGG_DIST` and confirm captures fire with GPS attached.

Budget one debugging session for this. Field names and integer scaling (lat/lon are
1e7-scaled, altitudes are mm) are the usual culprits.

---

## 8. 🟠 Satellite tiles could not be downloaded

Every tile server is blocked here, so `static/tiles/` ships **empty**. The map falls back
to plain sand-coloured tiles and the dashboard tells you so rather than showing a broken
grid.

**To close:** open **Settings → Offline map** on a machine with internet, confirm the
centre, and click download. It shows the tile count and size before committing. For a
620 m box (38 ha) at zoom 16–19 that's about 138 tiles / 2.4 MB — then commit them and
the Pi never needs the network again.

Also worth knowing: `DEPLOYMENT.md` originally suggested copying Mission Planner's
prefetched tiles. **Don't bother** — Mission Planner stores them in a proprietary
`gmapcache/TileDBv3` database, not a `{z}/{x}/{y}` tree. The built-in downloader is
simpler. I've corrected that doc.

---

## 9. 🟡 The farm's location is inconsistent between sources

- Your thesis says the site is **Brgy. Bilog-bilog, Tanauan City**.
- You told me the farm is near **Vis Compound, Brgy. Altura Bata, Tanauan City**.

These are different barangays, several kilometres apart. I defaulted the map centre to
Altura Bata (≈ **14.1265 N, 121.0768 E**, from PhilAtlas) because that's what you said,
and made it editable from the map.

I could not verify the Altura Bata coordinate independently — the geocoding services were
blocked, so it comes from a search result, and it's a *barangay centroid*, not your plot.

**To close:** stand in the field, read the coordinates off the dashboard (or any phone GPS),
and set the plot centre from **Settings**. Then reconcile the barangay name with the paper
before the defense — an examiner reading both will notice.

---

## 10. 🟡 Contradictions and omissions inside the thesis itself

Found while implementing. None are hard to fix in the paper, but they should be fixed.

**In-flight vs post-flight processing — the paper contradicts itself.**
Objective 2 and the Process Flow say the Pi "processes images natively **during flight**"
and that "this loop repeats until the flight concludes". The Hardware section says the
components are "for **post flight** image processing", and Scope says heatmaps are
retrieved "after processing" on landing.

I implemented **capture in flight, process after landing**, because a Pi 4 cannot
reliably run 8 MP BNDVI plus figure rendering per frame at a ~5 s cadence while also
servicing MAVLink — and because your own mockup has a dedicated post-landing "Processing"
view with a progress ring, so the design already assumes batch. Pick one story and make
Chapter 2 consistent.

**Other things the paper never specifies, which the code now has to decide:**

| Missing | What the code does |
|---|---|
| any camera setting — exposure, gain, AWB, resolution, format, focus | all are editable settings, defaults gain 2.0 / 5000 µs / AWB+AE locked, recorded per capture |
| any calibration procedure — no white reference, no exposure lock | `CALIBRATION.md`'s procedure, now built into the Debug view |
| flight parameters — altitude, GSD, image overlap, line spacing, speed | read live off the Pixhawk instead of hardcoded |
| capture trigger logic | all three modes from the mockup; Table 1 implies ~5 s interval |
| the green channel's role | unused, as in the paper; Debug labels it "mostly blocked" so it's visibly accounted for |
| storage formats / geotag encoding | JSON index + per-flight directories; float32 BNDVI saved as `.npz` |
| how the filter is physically mounted | only "applied to" the camera in the paper — `HARDWARE.md` is the sole record, cite it |
| software stack | only OpenCV and MAVLink are named in the paper; the implementation is Flask + NumPy + picamera2 + pymavlink, and does **not** use OpenCV |

That last row is worth a deliberate decision: **the paper names OpenCV as the image
processing library, and this implementation doesn't use it** — NumPy does everything the
pipeline needs, and adding OpenCV to a Pi 4 for channel arithmetic would be dead weight.
Either update the paper, or tell me and I'll reconcile the code to it.

**Also absent, and expected for a Design-and-Practice-1 proposal:** any results,
accuracy figures, processing-time measurements, or thermal/flight-time characterisation.
The paper lists these as things to be measured. The dashboard now records processing time
and CPU temperature per flight, so that data will accumulate on its own once you start
flying.

---

## Suggested order of attack

| # | Task | Where | Effort |
|---|---|---|---|
| 1 | Measure your own `k` against white paper | Debug view, no network needed | 10 min |
| 2 | Verify the AWB/AE lock really holds (§4) | Debug view, on the rig | 10 min |
| 3 | Download the offline map tiles | Settings, needs internet once | 5 min |
| 4 | Test telemetry against ArduPilot SITL | laptop, no drone needed | 1 hour |
| 5 | Derive real thresholds from ground truth | field + dashboard | a field session |
| 6 | Read the two Public Lab notes, fix the `k` citation | any browser | 30 min |
| 7 | Get the Rosco #2007 curve for the paper | any browser | 15 min |
| 8 | Fix the in-flight/post-flight contradiction | the paper | 15 min |
| 9 | Reconcile the barangay name | the paper | 5 min |

Items 1, 2 and 5 are the ones that decide whether the numbers mean anything. Everything
else is polish or paperwork.
