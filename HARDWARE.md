# Hardware Setup

How to physically build the BNDVI rig — what parts you need, how to
mount the bundled blue filter on the Pi NoIR v2 camera, and how to
verify it's mounted correctly.

## Bill of materials

| Part | Notes |
|---|---|
| Raspberry Pi 4 Model B | Any RAM variant; project doesn't need >1 GB |
| Pi NoIR Camera v2 | Must be the **NoIR** (no IR-cut filter) version |
| Rosco #2007 blue gel | Comes bundled with the v2 NoIR — small square in the box |
| CSI ribbon cable | Comes with the camera |
| microSD card (≥ 16 GB) | Raspberry Pi OS (Bookworm recommended) |
| 5 V / 3 A USB-C power supply | Official Pi PSU is most reliable |
| Camera mount / tripod | Optional, but useful for repeatable shots |

> ⚠️ If you have the **regular Pi Camera v2** (green PCB, IR-cut filter
> installed), this project will not work — the IR-cut filter blocks the
> NIR wavelengths we depend on. You need the **NoIR** variant
> (black/dark PCB, no IR-cut filter).

## Connecting the camera

1. Power the Pi **off** before touching the ribbon cable.
2. Lift the CSI connector latch on the Pi (the black plastic clip on the
   port between HDMI and the headphone jack, marked "CAMERA").
3. Insert the ribbon cable with the **silver contacts facing the HDMI
   port** (i.e. away from the Ethernet port).
4. Push the latch back down.
5. Do the same on the camera module side; contacts face away from the
   green PCB lettering.
6. Power on and enable the camera interface:
   ```bash
   sudo raspi-config
   ```
   → Interface Options → Camera → Enable → reboot.

Verify it works **before** mounting the filter:

```bash
libcamera-still -o test.jpg
```

You should get a normal-looking colour photo. If this fails, fix the
cable / camera config before attaching the filter.

## Mounting the blue filter

The filter is a small square of Rosco #2007 ("Storaro Blue") polyester
gel — thin, flexible, slightly sticky to fingerprints. It has to sit in
the optical path with no light leaking around its edges.

> **Do not glue the gel directly to the lens.** Most glues outgas
> solvents that fog or etch the optics, and removing the gel later
> becomes nearly impossible. Glue creep also pulls the gel out of
> flatness, which shows up as optical artefacts in captures.

Pick **one** of the four methods below, in order of preference:

### Method 1 — Inside the lens cap (easiest, recommended)

This is what the [Raspberry Pi blog suggests](https://www.raspberrypi.com/news/whats-that-blue-thing-doing-here/).

1. Snap off the small clear plastic lens cap from the camera module.
2. Cut the gel slightly smaller than the inside of the cap.
3. Drop it in. The cap holds it against the lens when snapped back on.

**Pros**: zero adhesive, fully reversible, takes 30 seconds.
**Cons**: gel is loose; if the cap pops off, the gel falls out. Tape
the cap to the module if you'll be moving the rig around.

### Method 2 — Tape at the edges (most secure non-permanent)

1. Cut the gel to overhang the lens aperture by ~1 mm on each side.
2. Centre it over the lens.
3. Use two small pieces of clear or black electrical tape (kapton tape
   is even better) on **opposite edges only**. **Never run tape across
   the optical centre** — it'll show up in every photo.

**Pros**: stays put through movement and vibration.
**Cons**: tape residue if you remove it later.

### Method 3 — 3D-printed gel holder

If you have access to a 3D printer, search Thingiverse / Printables for
"Pi NoIR v2 filter holder" — several designs slot the gel into a small
ring that screws onto the lens housing or clips over the module.

**Pros**: cleanest, most repeatable, swappable filters.
**Cons**: requires a printer.

### Method 4 — Under the lens (advanced, not recommended)

The Pi NoIR v2's lens is on a screw-thread focus ring sealed with a tiny
dot of glue at the factory. Some users unscrew it slightly, drop the gel
between the lens and the sensor, and re-tighten.

**Pros**: gel is fully protected inside the assembly.
**Cons**: breaks the factory focus seal — you'll need to re-focus the
lens by hand against a known target afterwards, and risk losing focus
permanently. Skip unless the other methods genuinely don't work.

## Practical tips

- **Handle the gel by the edges** to avoid fingerprints in the optical path.
- **Cut on a flat surface with sharp scissors.** Creases stay visible
  in every capture.
- **Cover the aperture fully.** If unfiltered light leaks around the
  gel, red wavelengths reach the sensor and your BNDVI will be wrong.
  Erring on the side of "too big" is fine.
- **Either side works** optically; the gel is symmetric. If you can
  tell which side is matte vs glossy, mount with the matte side toward
  the lens.
- **One gel is all you get** in the v2 box. If you cut it badly and
  need another, you can buy Rosco #2007 sheets cheaply online (a single
  sheet has enough material for hundreds of cameras).

## Verifying the mount

Once mounted, take a daylight capture of vegetation:

```bash
cd bndvi_dashboard
python bndvi.py
```

Open the raw JPEG in `bndvi_output/`. You should see:

- **Pinkish/magenta vegetation** (healthy plants reflect NIR, which lands
  in the red channel) — this is the key sanity check
- **No bright red bleed** around the corners of the frame (would
  indicate the gel is too small / not covering the aperture)
- **No visible creases, fingerprints, or dust** in the image

If the photo looks normal-coloured, the gel has fallen out or the cap
is on without the gel inside.

If only the centre is pink and the edges are colour-correct, the gel
is too small — the filter is only covering the middle of the aperture
and red light is leaking in from the sides.

## After mounting: calibrate

Mounting the gel is step 1. The optical settings (gain, exposure, AWB
lock) still need to be tuned for your lighting. See
[CALIBRATION.md](./CALIBRATION.md) for that walkthrough.
