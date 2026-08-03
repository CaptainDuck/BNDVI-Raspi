"""
Offline satellite basemap tiles.

The dashboard must work with no internet — that is the argument of the thesis,
so nothing on the page may reach the network at run time. Leaflet and the fonts
are vendored into static/; the basemap is the remaining piece, and it has to be
downloaded once, ahead of time, for the farm's bounding box only.

This module does three things:

1. Estimates a download before committing to it, so the UI can say "138 tiles,
   about 2.4 MB" rather than starting an unbounded fetch.
2. Downloads the pyramid into static/tiles/{z}/{x}/{y}.jpg and writes a
   manifest describing exactly what was fetched.
3. Reports coverage back — which is what answers "how far is the map
   downloaded?" in Settings and as a dashed boundary on the farm map.

Note on Mission Planner: DEPLOYMENT.md used to suggest copying its prefetched
cache. Don't — it stores tiles in a proprietary gmapcache/TileDBv3 database, not
a {z}/{x}/{y} tree. Downloading directly is simpler and gives us the manifest.
"""

import json
import logging
import math
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("hylocropter.tiles")

# Esri World Imagery: free, no API key, and the layer Mission Planner also uses.
# Detail runs out around z19 in most of the world.
TILE_SOURCE = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
               "World_Imagery/MapServer/tile/{z}/{y}/{x}")
ATTRIBUTION = "Imagery © Esri"
SOURCE_NAME = "Esri World Imagery"
MAX_SOURCE_ZOOM = 19

# Refuse absurd jobs outright rather than hammering someone's tile server.
MAX_TILES = 4000
AVG_TILE_BYTES = 18_000          # observed average for this layer, for estimates
REQUEST_TIMEOUT_S = 20
POLITE_DELAY_S = 0.05            # ~20 req/s; well within fair use for one farm
# Consecutive failures from the very first tile that mean "there is no internet",
# so we stop rather than making the user watch hundreds of timeouts.
EARLY_ABORT_FAILURES = 6


# ── Web Mercator helpers ─────────────────────────────────────────────────────

def deg2tile(lat, lon, z):
    n = 2 ** z
    lat = max(-85.05112878, min(85.05112878, lat))
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(math.radians(lat))
                            + 1.0 / math.cos(math.radians(lat))) / math.pi)
            / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def tile2deg(x, y, z):
    """North-west corner of tile (x, y) at zoom z, as (lat, lon)."""
    n = 2 ** z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon


def metres_per_pixel(lat, z):
    return 156543.03392 * math.cos(math.radians(lat)) / (2 ** z)


def box_bounds(lat, lon, box_m):
    """Square bounding box of `box_m` metres a side, centred on lat/lon."""
    half = box_m / 2.0
    dlat = half / 111_320.0
    dlon = half / (111_320.0 * max(0.01, math.cos(math.radians(lat))))
    return {"south": lat - dlat, "west": lon - dlon,
            "north": lat + dlat, "east": lon + dlon}


def bounds_area_ha(bounds):
    mid = (bounds["south"] + bounds["north"]) / 2
    h = (bounds["north"] - bounds["south"]) * 111_320.0
    w = (bounds["east"] - bounds["west"]) * 111_320.0 * math.cos(
        math.radians(mid))
    return round(h * w / 10_000.0, 1)


def plan(lat, lon, box_m, zoom_min, zoom_max):
    """What a download would fetch, without fetching anything.

    Lets the UI show the tile count and size before you commit — the difference
    between an informed click and a surprise.
    """
    zoom_max = min(int(zoom_max), MAX_SOURCE_ZOOM)
    zoom_min = min(int(zoom_min), zoom_max)
    bounds = box_bounds(lat, lon, box_m)
    per_zoom, ranges, total = {}, {}, 0
    for z in range(zoom_min, zoom_max + 1):
        x0, y0 = deg2tile(bounds["north"], bounds["west"], z)
        x1, y1 = deg2tile(bounds["south"], bounds["east"], z)
        x0, x1 = min(x0, x1), max(x0, x1)
        y0, y1 = min(y0, y1), max(y0, y1)
        count = (x1 - x0 + 1) * (y1 - y0 + 1)
        per_zoom[str(z)] = count
        ranges[z] = (x0, x1, y0, y1)
        total += count
    # Tiles sit on a fixed grid, so any box snaps outward to whole tiles and you
    # end up with more ground than you asked for. Report that separately — the
    # difference is what the coverage inset draws, and it is real: it's imagery
    # you have and can fly over.
    top = ranges[zoom_max]
    nw_lat, nw_lon = tile2deg(top[0], top[2], zoom_max)
    se_lat, se_lon = tile2deg(top[1] + 1, top[3] + 1, zoom_max)
    tile_bounds = {"south": se_lat, "west": nw_lon, "north": nw_lat, "east": se_lon}

    return {
        "centre": [round(lat, 6), round(lon, 6)],
        "bounds": bounds,
        "tile_bounds": tile_bounds,
        "tile_area_ha": bounds_area_ha(tile_bounds),
        "box_m": int(box_m),
        "area_ha": bounds_area_ha(bounds),
        "zooms": list(range(zoom_min, zoom_max + 1)),
        "per_zoom": per_zoom,
        "tiles": total,
        "est_bytes": total * AVG_TILE_BYTES,
        "ranges": {str(k): list(v) for k, v in ranges.items()},
        "too_large": total > MAX_TILES,
        "max_tiles": MAX_TILES,
        "source": SOURCE_NAME,
        "attribution": ATTRIBUTION,
        "ground_resolution_m": round(metres_per_pixel(lat, zoom_max), 3),
    }


# ── the downloader ───────────────────────────────────────────────────────────

class TileDownloader:
    """Background tile fetch with progress the UI can poll."""

    def __init__(self, tiles_dir):
        self.tiles_dir = Path(tiles_dir)
        self._lock = threading.Lock()
        self._thread = None
        self._cancel = threading.Event()
        self._state = {"running": False, "done": 0, "total": 0, "failed": 0,
                       "bytes": 0, "message": "idle", "finished_at": None,
                       "ok": None}

    @property
    def manifest_path(self):
        return self.tiles_dir / "manifest.json"

    def progress(self):
        with self._lock:
            state = dict(self._state)
        total = state["total"] or 1
        state["percent"] = round(100.0 * state["done"] / total, 1)
        return state

    def start(self, lat, lon, box_m, zoom_min, zoom_max):
        with self._lock:
            if self._state["running"]:
                return False, "A download is already running."
        job = plan(lat, lon, box_m, zoom_min, zoom_max)
        if job["too_large"]:
            return False, (f"That area needs {job['tiles']} tiles, over the "
                           f"{MAX_TILES} limit. Reduce the area or the maximum "
                           f"zoom.")
        self._cancel.clear()
        with self._lock:
            self._state = {"running": True, "done": 0, "total": job["tiles"],
                           "failed": 0, "bytes": 0, "finished_at": None,
                           "ok": None,
                           "message": f"Downloading {job['tiles']} tiles…"}
        self._thread = threading.Thread(target=self._run, args=(job,),
                                        name="tiles", daemon=True)
        self._thread.start()
        return True, f"Downloading {job['tiles']} tiles (~{_mb(job['est_bytes'])})."

    def cancel(self):
        self._cancel.set()

    def _run(self, job):
        done = failed = 0
        total_bytes = 0
        first_error = None
        no_network = False
        try:
            for z in job["zooms"]:
                x0, x1, y0, y1 = job["ranges"][str(z)]
                for x in range(x0, x1 + 1):
                    for y in range(y0, y1 + 1):
                        if self._cancel.is_set():
                            self._finish(False, "Cancelled.", done, failed,
                                         total_bytes, job)
                            return
                        size, err = self._fetch(z, x, y)
                        if err:
                            failed += 1
                            if first_error is None:
                                first_error = err
                        else:
                            total_bytes += size
                        done += 1
                        with self._lock:
                            self._state.update(done=done, failed=failed,
                                               bytes=total_bytes)
                        # If the first handful all fail, there is no internet --
                        # grinding through the remaining hundreds of timeouts
                        # just makes the user wait to be told the obvious.
                        if failed == done and failed >= EARLY_ABORT_FAILURES:
                            no_network = True
                            break
                        time.sleep(POLITE_DELAY_S)
                    if no_network:
                        break
                if no_network:
                    break
        except Exception as exc:
            log.exception("tile download failed")
            self._finish(False, f"Download failed: {exc}", done, failed,
                         total_bytes, job)
            return

        if failed == done and done:
            # Nothing at all got through -- almost always no internet, or the
            # tile host blocked. Say that plainly, not "0 tiles downloaded".
            self._finish(False, (
                f"Could not reach {SOURCE_NAME} — this machine has no internet "
                f"right now. Connect it to a network with internet and try "
                f"again. ({first_error})"),
                done, failed, total_bytes, job)
            return
        msg = f"Downloaded {done - failed} tiles ({_mb(total_bytes)})."
        if failed:
            msg += f" {failed} tiles failed and will show as blank ground."
        self._finish(True, msg, done, failed, total_bytes, job)

    def _fetch(self, z, x, y):
        path = self.tiles_dir / str(z) / str(x) / f"{y}.jpg"
        if path.exists() and path.stat().st_size > 0:
            return path.stat().st_size, None
        url = TILE_SOURCE.format(z=z, x=x, y=y)
        req = urllib.request.Request(url, headers={
            "User-Agent": "Hylocropter/1.0 (offline farm map prefetch)"})
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                data = resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                TimeoutError) as exc:
            return 0, str(exc)
        if not data:
            return 0, "empty response"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except Exception as exc:
            Path(tmp).unlink(missing_ok=True)
            return 0, str(exc)
        return len(data), None

    def _finish(self, ok, message, done, failed, total_bytes, job):
        with self._lock:
            self._state.update(running=False, ok=ok, message=message,
                               done=done, failed=failed, bytes=total_bytes,
                               finished_at=time.time())
        if done - failed > 0:
            self.write_manifest(job, done - failed, total_bytes, failed)
        log.info("tile download finished: %s", message)

    def write_manifest(self, job, tiles, total_bytes, failed):
        manifest = {
            "centre": job["centre"],
            "bounds": job["bounds"],
            "tile_bounds": job["tile_bounds"],
            "tile_area_ha": job["tile_area_ha"],
            "box_m": job["box_m"],
            "area_ha": job["area_ha"],
            "zooms": job["zooms"],
            "per_zoom": self._count_on_disk(job["zooms"]),
            "tiles": tiles,
            "failed": failed,
            "bytes": total_bytes,
            "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": SOURCE_NAME,
            "attribution": ATTRIBUTION,
            "complete": failed == 0,
        }
        self.tiles_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(json.dumps(manifest, indent=2))
        return manifest

    def _count_on_disk(self, zooms):
        out = {}
        for z in zooms:
            zd = self.tiles_dir / str(z)
            out[str(z)] = (sum(1 for _ in zd.rglob("*.jpg"))
                           if zd.exists() else 0)
        return out


# ── coverage reporting ───────────────────────────────────────────────────────

def coverage(tiles_dir):
    """What is actually on disk, for the "how far is the map downloaded?" UI.

    Reads the manifest but verifies against the filesystem, so a manifest left
    behind by a deleted tile tree doesn't claim coverage that isn't there.
    """
    tiles_dir = Path(tiles_dir)
    manifest_path = tiles_dir / "manifest.json"

    on_disk, per_zoom, total_bytes = 0, {}, 0
    zooms = []
    if tiles_dir.exists():
        for zd in sorted(tiles_dir.iterdir()):
            if not (zd.is_dir() and zd.name.isdigit()):
                continue
            count, size = 0, 0
            for p in zd.rglob("*.jpg"):
                count += 1
                size += p.stat().st_size
            if count:
                zooms.append(int(zd.name))
                per_zoom[zd.name] = count
                on_disk += count
                total_bytes += size

    result = {
        "has_tiles": on_disk > 0,
        "tiles": on_disk,
        "bytes": total_bytes,
        "size_label": _mb(total_bytes),
        "per_zoom": per_zoom,
        "zooms": sorted(zooms),
        "bounds": None,
        "tile_bounds": None,
        "tile_area_ha": None,
        "box_m": None,
        "area_ha": None,
        "centre": None,
        "downloaded_at": None,
        "source": SOURCE_NAME,
        "attribution": ATTRIBUTION,
        "complete": False,
    }

    if manifest_path.exists():
        try:
            m = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            m = {}
        for key in ("bounds", "tile_bounds", "tile_area_ha", "box_m", "area_ha",
                    "centre", "downloaded_at", "source", "attribution",
                    "complete"):
            if key in m:
                result[key] = m[key]

    if result["has_tiles"] and result["box_m"]:
        span = result["box_m"]
        result["extent_label"] = (
            f"{span} m × {span} m · {result['area_ha']} ha")
        if result["tile_area_ha"]:
            result["actual_label"] = (
                f"{result['tile_area_ha']} ha of imagery on disk "
                f"(tiles snap outward to a whole-tile grid)")
        best = max(result["zooms"])
        lat = result["centre"][0] if result["centre"] else 0.0
        result["detail_label"] = (
            f"down to {round(metres_per_pixel(lat, best) * 100)} cm per pixel "
            f"at zoom {best}")
    return result


def _mb(byte_count):
    if byte_count < 1024:
        return f"{byte_count} B"
    if byte_count < 1024 * 1024:
        return f"{byte_count / 1024:.0f} KB"
    return f"{byte_count / (1024 * 1024):.1f} MB"


def fallback_tile():
    """A plain sand-coloured JPEG, served when a tile is missing.

    Better than a 404: Leaflet renders broken-image icons across the whole map,
    which reads as "the dashboard is broken" rather than "no imagery here".
    """
    global _FALLBACK
    if _FALLBACK is None:
        from io import BytesIO
        from PIL import Image
        buf = BytesIO()
        # matches --color-neutral-300, so missing tiles look like bare ground
        Image.new("RGB", (256, 256), (220, 211, 196)).save(
            buf, format="JPEG", quality=70)
        _FALLBACK = buf.getvalue()
    return _FALLBACK


_FALLBACK = None
