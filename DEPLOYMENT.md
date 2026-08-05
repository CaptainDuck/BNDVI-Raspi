# Deploying Hylocropter on the Pi

The dashboard must work with no internet — that is the argument of the paper, so
nothing in the page may reach the network at run time.

Most of that is already done. This file covers what's left: the basemap, the
Wi-Fi hotspot, the two privileged actions, running it as a service, and the
bench checklist for the parts that could not be tested without hardware.

## What's already offline

| Asset | Where it lives |
|---|---|
| Leaflet 1.9.4 (js, css, marker images) | `hylocropter/static/vendor/leaflet/` |
| Caprasimo + Figtree | `hylocropter/static/fonts/` with local `@font-face` |
| Charts | none — the trend is flexbox, the histogram is a canvas. Chart.js is gone |
| Basemap tiles | `hylocropter/static/tiles/` — **you download these once, see below** |

To prove it: load the dashboard, open devtools → Network, and confirm every
request goes to the Pi. The development check asserted zero external requests
across all seven views; if you add anything, re-check.

## 1. Download the basemap

Do this once, on the Pi or a laptop **with internet**:

1. Open **Settings → Offline map**.
2. Set the plot centre. It defaults to 14.1265 N, 121.0768 E (Brgy. Altura Bata,
   Tanauan City). Better: stand in the field and read the coordinates off a
   phone, then type them in.
3. Pick the box size. 620 m covers 38 ha, which is comfortable around a 5–10 ha
   plot and leaves context when the drone drifts past the boundary.
4. Check the estimate — it shows tiles and megabytes before starting.
5. Click **Download the map**.

Sizes at zoom 16–19 for this latitude:

| Box | Area | Tiles | On disk |
|---|---|---|---|
| 320 m | 10.2 ha | 51 | ~0.9 MB |
| 620 m | 38.4 ha | 138 | ~2.4 MB |
| 1 km | 100 ha | 267 | ~4.7 MB |

Esri World Imagery runs out of detail around zoom 19 (≈29 cm/pixel), so asking
for more just downloads upscaled tiles.

Commit `static/tiles/` afterwards — a few megabytes is worth never thinking
about it again. **Settings** then reports exactly how far coverage extends, and
the farm map draws a dashed boundary at its edge so a flight track running past
it is obvious rather than looking like bare ground.

Missing tiles serve a plain sand-coloured image, never a 404 — Leaflet would
otherwise cover the map in broken-image icons, which reads as a broken dashboard.

> The earlier version of this file suggested copying Mission Planner's prefetched
> cache. **Don't** — it stores tiles in a proprietary `gmapcache/TileDBv3`
> database, not a `{z}/{x}/{y}` tree. The built-in downloader is simpler.

## 2. Camera triggering belongs in the mission

Put `DO_SET_CAM_TRIGG_DIST` in the Mission Planner mission, or set the
`CAM_TRIGG_DIST` parameter. The flight controller then emits `CAMERA_TRIGGER` /
`CAMERA_FEEDBACK` over MAVLink and the Pi captures on each message, with the
controller's own GPS position attached to every photo — which is what places it
correctly on the map.

**The dashboard never arms the aircraft.** Mission Planner flies it; the Pi runs
the camera and the processing. `telemetry.py` only ever reads and requests data
streams; keep it that way.

## 3. Wi-Fi access point

Bookworm uses NetworkManager, so the hotspot is `nmcli`, not `hostapd`:

```bash
sudo nmcli con add type wifi ifname wlan0 mode ap con-name hylocropter \
     ssid Hylocropter autoconnect yes
sudo nmcli con modify hylocropter 802-11-wireless.mode ap \
     802-11-wireless.band bg ipv4.method shared
sudo nmcli con modify hylocropter wifi-sec.key-mgmt wpa-psk
sudo nmcli con modify hylocropter wifi-sec.psk "your-password-here"
sudo nmcli con up hylocropter
```

NetworkManager handles DHCP itself. Set the hostname so `hylocropter.local`
resolves over mDNS:

```bash
sudo hostnamectl set-hostname hylocropter
sudo apt install -y avahi-daemon
```

The dashboard is then at `http://hylocropter.local:5000/` from any phone on the
hotspot.

> Deliberately **not** exposed in the UI: changing the Pi's network config from a
> web page served over that same network is a good way to lock yourself out in a
> field.

## 4. The two privileged actions

"Shut down the Pi" and "Copy all flights to a USB stick" need more than the app
user has. Rather than running the whole dashboard as root — it serves a web page
to a field Wi-Fi network — grant just those:

```bash
sudo visudo -f /etc/sudoers.d/hylocropter
```

```
pi ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff
```

Without this, "Shut down the Pi" reports that it isn't allowed rather than
failing silently. USB copy works without sudo as long as the stick is
auto-mounted under `/media`.

## 5. Run it as a service

```bash
sudo tee /etc/systemd/system/hylocropter.service >/dev/null <<'EOF'
[Unit]
Description=Hylocropter BNDVI dashboard
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/Hylocropter
ExecStart=/usr/bin/python3 hylocropter/app.py --host 0.0.0.0 --port 5000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now hylocropter
```

Logs go to `hylocropter/hylocropter_data/hylocropter.log` (rotating, 5 × 1 MB)
as well as journald — and to the **Debug** page's log viewer, which is the point:
you shouldn't need `journalctl` to find out what happened.

## Bench checklist

Everything below could **not** be tested during development — there was no
camera, no flight controller, and no tile access. Work through it before the
demo.

### Camera

- [ ] `python bndvi.py` succeeds and writes to `hylocropter_data/ground/`
- [ ] The raw JPEG shows **pink/magenta vegetation**. If it looks like a normal
      photo, the gel has fallen out or AWB is still on.
- [ ] No red bleed at the frame corners (gel too small)
- [ ] Open **Debug** — all seven canvases paint, and the Red channel is clearly
      brighter than Blue over plants
- [ ] **Verify the AWB/AE lock actually holds.** Point at a fixed scene, then
      shade the subject with your hand. The histogram should shift bodily
      brighter/darker while the **BNDVI mean barely moves**. If the raw
      brightness doesn't change either, the lock failed silently. The dashboard
      also checks every frame against the camera's own metadata and will say so —
      see RESEARCH-GAPS.md §4.
- [ ] **Confirm the ISP is neutralised.** Take a capture and check its detail page
      says "ISP neutralised: yes" with no control-mismatch warning. This matters
      more than it sounds: with the pipeline live, the colour-correction matrix
      mixes green into both red and blue and produces a misclassification band
      right at the 0.3 threshold — and a white card cannot detect it. This code
      path has never run against a camera, so treat it as unverified until you've
      checked. RESEARCH-GAPS.md §4.
- [ ] Put a white card in frame, drag a box over it in Debug, hit **Solve k**.
      Confirm the card then reads BNDVI ≈ 0.
- [ ] A known-healthy plant reads mean BNDVI **+0.3 to +0.7**. A negative value
      means the channel mapping got reversed.

### Flight controller

Test against **ArduPilot SITL over UDP first** — no hardware, no risk:

- [ ] Run SITL on a laptop, set **Settings → MAVLink connection** to
      `udp:127.0.0.1:14550`
- [ ] Header chip goes green; the pre-flight checklist's MAVLink and GPS rows pass
- [ ] Flight mode, waypoint, altitude and GPS populate on the New flight page
- [ ] Arming opens a flight; disarming closes it and lands on Processing
- [ ] `CAM_TRIGG_DIST` fires captures, and each one has a GPS position on its
      detail page

Then the real Pixhawk on `/dev/ttyAMA0` at 57600. Enable the UART and free the
serial console first:

```bash
sudo raspi-config    # Interface Options -> Serial Port -> login shell NO, hardware YES
```

Expect one round of fixes here — message field names and integer scaling
(lat/lon are 1e7-scaled, altitudes are millimetres) are the usual culprits, and
none of this decoding has run against a real stream.

### Offline

- [ ] Download the basemap with internet, then **disconnect it**
      (`sudo ip link set wlan0 down` after connecting a laptop to the hotspot) and
      reload every page
- [ ] Map renders, fonts are correct, no broken images, no blank panels
- [ ] Devtools → Network shows nothing leaving the Pi

Anything that silently degrades is a finding for the defense, so fix it now
rather than during.

### Development checks

**The test suite runs anywhere, including on the Pi:**

```bash
pip install -r hylocropter/requirements-dev.txt
pytest
```

152 tests covering the index maths, the photo→footprint→grid mapping chain, the
JSON store, the settings, and every route rendering with no camera and no drone.
`.github/workflows/verify.yml` runs the same suite on every push, in one job,
plus the standalone-`bndvi.py` check, the app boot, and greps for a CDN reference
or a reintroduced `(B − R)/(B + R)`.

Worth being explicit: **CI does not test the camera or MAVLink.** Nothing about a
green tick means those work — the checklists above are still the only evidence.

**Playwright** verification lives outside the repo (it needs a browser the Pi
doesn't have). It asserts, for all seven views at desktop and phone widths: the
page renders, Leaflet initialises from the vendored file, the BNDVI overlay
draws, all seven debug canvases actually paint, moving `k` repaints with **zero**
network requests, the threshold sliders move the band percentages, the
white-card solver returns a coefficient, history search hides rows, the battery
chip stays hidden with no telemetry, a blocked tile download reports a clear
reason, and **no request leaves the device**. 42 checks, all passing at the time
of writing.
