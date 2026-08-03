"""
Flight and capture storage.

A flight groups many GPS-tagged captures; the farm map is a grid built by
binning each capture's mean BNDVI into the cell its position falls in.

Still JSON files, no database — this device has one user and a flat list of
flights, and a JSON index is something you can read with your eyes when
something goes wrong in the field.

Two problems in the old index are fixed here:

* The capture lock used to be released *before* the read-append-write of
  `captures.json`, so two overlapping requests could lose a record. All index
  mutation now happens under one lock.
* Writes were plain `write_text()`. On an SD card in a drone, a power cut
  mid-write truncates the file and the dashboard comes back empty. Writes are
  now temp-file + `os.replace()`, which is atomic.
"""

import datetime
import json
import logging
import math
import os
import shutil
import tempfile
import threading
from pathlib import Path

import bndvi

log = logging.getLogger("hylocropter.flights")

# Map overlay resolution. Matches the mockup's 14x9 so the map reads the same,
# and it is about right for a 10 ha plot -- ~40 m cells at 620 m across.
GRID_COLS, GRID_ROWS = 14, 9

# How far outside the outermost capture the flight bounds extend, so edge
# captures are not painted on the very border of the overlay.
BOUNDS_PAD_M = 25.0
# Fallback half-size when a flight has one capture, or none with GPS.
BOUNDS_MIN_M = 60.0


class Store:
    """Owns hylocropter_data/: the two indexes and the per-flight directories."""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.captures_file = self.data_dir / "captures.json"
        self.flights_file = self.data_dir / "flights.json"
        self.ground_dir = self.data_dir / "ground"
        self._lock = threading.RLock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.ground_dir.mkdir(parents=True, exist_ok=True)

    # ── low-level IO ──────────────────────────────────────────────────────

    @staticmethod
    def _read(path):
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError) as exc:
            log.error("could not read %s: %s", path.name, exc)
            return []

    @staticmethod
    def _write(path, records):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(records, fh, indent=2)
            os.replace(tmp, path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise

    # ── captures ──────────────────────────────────────────────────────────

    def captures(self, flight_id=None, newest_first=True):
        with self._lock:
            records = self._read(self.captures_file)
        if flight_id is not None:
            records = [r for r in records if r.get("flight_id") == flight_id]
        return sorted(records, key=lambda r: r.get("timestamp", ""),
                      reverse=newest_first)

    def capture(self, capture_id):
        return next((r for r in self.captures() if r["id"] == capture_id), None)

    def add_capture(self, record):
        with self._lock:
            records = self._read(self.captures_file)
            records.append(record)
            self._write(self.captures_file, records)
        return record

    def update_capture(self, capture_id, patch):
        with self._lock:
            records = self._read(self.captures_file)
            record = next((r for r in records if r["id"] == capture_id), None)
            if record is None:
                return None
            for key in ("label", "notes"):
                if key in patch:
                    record[key] = patch[key] or None
            self._write(self.captures_file, records)
            return record

    def delete_capture(self, capture_id):
        with self._lock:
            records = self._read(self.captures_file)
            record = next((r for r in records if r["id"] == capture_id), None)
            if record is None:
                return False
            base = self.capture_dir(record.get("flight_id"))
            for name in record.get("files", {}).values():
                (base / name).unlink(missing_ok=True)
            self._write(self.captures_file,
                        [r for r in records if r["id"] != capture_id])
            return True

    def capture_dir(self, flight_id):
        """Where a capture's artefacts live. Ground captures go in ground/."""
        if not flight_id:
            return self.ground_dir
        d = self.data_dir / flight_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def file_path(self, capture_id, key):
        """Resolve one artefact via the index -- never by globbing the dir."""
        record = self.capture(capture_id)
        if not record:
            return None
        name = record.get("files", {}).get(key)
        if not name:
            return None
        return self.capture_dir(record.get("flight_id")) / name

    # ── flights ───────────────────────────────────────────────────────────

    def flights(self, newest_first=True):
        with self._lock:
            records = self._read(self.flights_file)
        return sorted(records, key=lambda f: f.get("started_at", ""),
                      reverse=newest_first)

    def flight(self, flight_id):
        return next((f for f in self.flights() if f["id"] == flight_id), None)

    def open_flight(self, name=None, trigger="mission", mission=None,
                    thresholds=None):
        """Start a flight. Returns the new record."""
        now = datetime.datetime.now()
        flight = {
            "id": "F-" + now.strftime("%Y%m%d-%H%M"),
            "name": name or "Untitled flight",
            "started_at": now.isoformat(timespec="seconds"),
            "ended_at": None,
            "duration_s": None,
            "capture_ids": [],
            "capture_count": 0,
            "bounds": None,
            "grid": None,
            "stats": None,
            "classification": None,
            "altitude_m": None,
            "mission": mission or {},
            "trigger": trigger,
            "status": "recording",
            "thresholds": thresholds or {
                "healthy": bndvi.DEFAULT_THRESHOLD_HEALTHY,
                "moderate": bndvi.DEFAULT_THRESHOLD_MODERATE},
        }
        with self._lock:
            records = self._read(self.flights_file)
            # A second flight in the same minute would collide on id.
            existing = {f["id"] for f in records}
            if flight["id"] in existing:
                suffix = 2
                while f"{flight['id']}-{suffix}" in existing:
                    suffix += 1
                flight["id"] = f"{flight['id']}-{suffix}"
            records.append(flight)
            self._write(self.flights_file, records)
        self.capture_dir(flight["id"])
        log.info("flight %s opened (%s)", flight["id"], flight["name"])
        return flight

    def update_flight(self, flight_id, patch):
        with self._lock:
            records = self._read(self.flights_file)
            flight = next((f for f in records if f["id"] == flight_id), None)
            if flight is None:
                return None
            flight.update(patch)
            self._write(self.flights_file, records)
            return flight

    def attach_capture(self, flight_id, capture_id):
        with self._lock:
            records = self._read(self.flights_file)
            flight = next((f for f in records if f["id"] == flight_id), None)
            if flight is None:
                return None
            if capture_id not in flight["capture_ids"]:
                flight["capture_ids"].append(capture_id)
            flight["capture_count"] = len(flight["capture_ids"])
            self._write(self.flights_file, records)
            return flight

    def delete_flight(self, flight_id, keep_records=False):
        """Delete a flight. `keep_records=True` removes only the image files.

        That distinction backs the mockup's "Free up space" dialog, which
        promises: "This removes flights older than 30 days and their photos.
        The numbers stay in the record."
        """
        with self._lock:
            flights = self._read(self.flights_file)
            flight = next((f for f in flights if f["id"] == flight_id), None)
            if flight is None:
                return False
            freed = 0
            d = self.data_dir / flight_id
            if d.exists():
                for p in d.rglob("*"):
                    if p.is_file():
                        freed += p.stat().st_size
                shutil.rmtree(d, ignore_errors=True)

            captures = self._read(self.captures_file)
            if keep_records:
                for r in captures:
                    if r.get("flight_id") == flight_id:
                        r["files"] = {}
                        r["files_purged"] = True
                flight["files_purged"] = True
                flight["status"] = flight.get("status", "ok")
                self._write(self.captures_file, captures)
                self._write(self.flights_file, flights)
            else:
                self._write(self.captures_file,
                            [r for r in captures
                             if r.get("flight_id") != flight_id])
                self._write(self.flights_file,
                            [f for f in flights if f["id"] != flight_id])
            return freed

    # ── analysis: bounds, grid, summary ───────────────────────────────────

    def close_flight(self, flight_id, thresholds=None):
        """Finalise a flight: bounds, grid, aggregate stats, classification."""
        captures = self.captures(flight_id=flight_id, newest_first=False)
        flight = self.flight(flight_id)
        if flight is None:
            return None
        th = thresholds or flight.get("thresholds") or {}
        t_healthy = th.get("healthy", bndvi.DEFAULT_THRESHOLD_HEALTHY)
        t_moderate = th.get("moderate", bndvi.DEFAULT_THRESHOLD_MODERATE)

        started = flight.get("started_at")
        ended = datetime.datetime.now()
        duration = None
        if started:
            try:
                duration = int(
                    (ended - datetime.datetime.fromisoformat(started))
                    .total_seconds())
            except ValueError:
                duration = None

        patch = {
            "ended_at": ended.isoformat(timespec="seconds"),
            "duration_s": duration,
            "capture_count": len(captures),
            "capture_ids": [c["id"] for c in captures],
            "status": "ok" if captures else "failed",
            "thresholds": {"healthy": t_healthy, "moderate": t_moderate},
        }

        if captures:
            patch["stats"] = aggregate_stats(captures)
            patch["classification"] = bndvi.classify(
                patch["stats"]["mean"], t_healthy, t_moderate)
            alts = [c["geo"]["rel_alt_m"] for c in captures
                    if c.get("geo") and c["geo"].get("rel_alt_m")]
            patch["altitude_m"] = (round(sum(alts) / len(alts), 1)
                                   if alts else None)
            bounds = compute_bounds(captures)
            patch["bounds"] = bounds
            patch["grid"] = build_grid(captures, bounds, t_healthy, t_moderate)

        flight = self.update_flight(flight_id, patch)
        log.info("flight %s closed — %d captures, mean %s", flight_id,
                 len(captures),
                 f"{patch['stats']['mean']:+.3f}" if captures else "n/a")
        return flight

    def recolour_flight(self, flight_id, t_healthy, t_moderate):
        """Recompute band shares for a flight at new thresholds.

        The stored per-capture `stats` are left alone deliberately -- they record
        what was measured at capture time. This only refreshes the flight-level
        summary the map legend reads from.
        """
        flight = self.flight(flight_id)
        if not flight or not flight.get("grid"):
            return flight
        cells = [c for c in flight["grid"]["cells"] if c is not None]
        if not cells:
            return flight
        total = len(cells)
        stats = dict(flight.get("stats") or {})
        stats["healthy_pct"] = 100.0 * sum(c > t_healthy for c in cells) / total
        stats["stressed_pct"] = 100.0 * sum(c < t_moderate for c in cells) / total
        stats["moderate_pct"] = 100.0 - stats["healthy_pct"] - stats["stressed_pct"]
        return self.update_flight(flight_id, {
            "stats": stats,
            "thresholds": {"healthy": t_healthy, "moderate": t_moderate},
            "classification": bndvi.classify(stats.get("mean", 0.0),
                                             t_healthy, t_moderate),
        })

    # ── migration ─────────────────────────────────────────────────────────

    def migrate_legacy(self, legacy_dir):
        """Bring forward a pre-Hylocropter bndvi_output/ directory.

        Old records get flight_id/geo of None so they show up as ground
        captures rather than being silently dropped.
        """
        legacy_dir = Path(legacy_dir)
        legacy_index = legacy_dir / "captures.json"
        if not legacy_index.exists():
            return 0
        legacy = self._read(legacy_index)
        if not legacy:
            return 0

        with self._lock:
            current = self._read(self.captures_file)
            known = {r["id"] for r in current}
            moved = 0
            for record in legacy:
                if record.get("id") in known:
                    continue
                record.setdefault("flight_id", None)
                record.setdefault("geo", None)
                record.setdefault("trigger", "manual")
                for name in record.get("files", {}).values():
                    src = legacy_dir / name
                    if src.exists():
                        shutil.copy2(src, self.ground_dir / name)
                current.append(record)
                moved += 1
            if moved:
                self._write(self.captures_file, current)
        if moved:
            log.info("migrated %d legacy captures from %s", moved, legacy_dir)
        return moved

    # ── device stats ──────────────────────────────────────────────────────

    def disk_usage(self):
        total = 0
        for p in self.data_dir.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
        return total


# ── module-level maths (pure, easy to reason about and test) ─────────────────

def aggregate_stats(captures):
    """Flight-level statistics from its captures.

    Means are weighted equally per capture rather than per pixel. Captures are
    all the same resolution, so this is the same answer with less bookkeeping.
    """
    def mean_of(key):
        vals = [c["stats"][key] for c in captures if c.get("stats")]
        return sum(vals) / len(vals) if vals else 0.0

    means = [c["stats"]["mean"] for c in captures if c.get("stats")]
    return {
        "mean": mean_of("mean"),
        "min": min((c["stats"]["min"] for c in captures if c.get("stats")),
                   default=0.0),
        "max": max((c["stats"]["max"] for c in captures if c.get("stats")),
                   default=0.0),
        "std": (math.sqrt(sum((m - (sum(means) / len(means))) ** 2
                              for m in means) / len(means)) if means else 0.0),
        "healthy_pct": mean_of("healthy_pct"),
        "moderate_pct": mean_of("moderate_pct"),
        "stressed_pct": mean_of("stressed_pct"),
    }


def compute_bounds(captures):
    """Bounding box around the geotagged captures, padded.

    Returns None when nothing has a fix — the map then falls back to the plot
    bounds from settings, and the UI says the flight has no GPS.
    """
    pts = [(c["geo"]["lat"], c["geo"]["lon"]) for c in captures
           if c.get("geo") and c["geo"].get("lat") is not None]
    if not pts:
        return None
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    mid_lat = (min(lats) + max(lats)) / 2

    pad_lat = BOUNDS_PAD_M / 111_320.0
    pad_lon = BOUNDS_PAD_M / (111_320.0 * max(0.01,
                                              math.cos(math.radians(mid_lat))))
    south, north = min(lats) - pad_lat, max(lats) + pad_lat
    west, east = min(lons) - pad_lon, max(lons) + pad_lon

    # One capture (or several at one spot) gives a degenerate box; open it out
    # so the overlay has somewhere to draw.
    min_lat = BOUNDS_MIN_M / 111_320.0
    min_lon = BOUNDS_MIN_M / (111_320.0 * max(0.01,
                                              math.cos(math.radians(mid_lat))))
    if north - south < min_lat:
        south, north = mid_lat - min_lat, mid_lat + min_lat
    if east - west < min_lon:
        mid_lon = (west + east) / 2
        west, east = mid_lon - min_lon, mid_lon + min_lon

    return {"south": south, "west": west, "north": north, "east": east}


def build_grid(captures, bounds, t_healthy, t_moderate,
               cols=GRID_COLS, rows=GRID_ROWS):
    """Bin capture means into a cols x rows grid over `bounds`.

    Empty cells stay None. That matters: painting an unvisited cell as
    mid-range would invent healthy ground the drone never flew over, which is
    exactly the kind of thing a farmer would act on.
    """
    cells = [None] * (cols * rows)
    if not bounds:
        return {"cols": cols, "rows": rows, "cells": cells, "covered": 0}

    sums = [0.0] * (cols * rows)
    counts = [0] * (cols * rows)
    dlat = bounds["north"] - bounds["south"]
    dlon = bounds["east"] - bounds["west"]
    if dlat <= 0 or dlon <= 0:
        return {"cols": cols, "rows": rows, "cells": cells, "covered": 0}

    for c in captures:
        geo = c.get("geo")
        if not geo or geo.get("lat") is None or not c.get("stats"):
            continue
        fx = (geo["lon"] - bounds["west"]) / dlon
        fy = 1.0 - (geo["lat"] - bounds["south"]) / dlat   # row 0 is north
        if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
            continue
        col = min(cols - 1, int(fx * cols))
        row = min(rows - 1, int(fy * rows))
        idx = row * cols + col
        sums[idx] += c["stats"]["mean"]
        counts[idx] += 1

    covered = 0
    for i in range(cols * rows):
        if counts[i]:
            cells[i] = round(sums[i] / counts[i], 4)
            covered += 1
    return {"cols": cols, "rows": rows, "cells": cells, "covered": covered}


def summarise(mean, stressed_pct, has_gps=True):
    """Plain-language summary, in the mockup's voice.

    The thesis asks for exactly this: a farm-level status summary in ordinary
    words, with the numbers available underneath for technical review.
    """
    if mean > 0.38:
        headline = "The plants look healthy."
        advice = "Nothing to do today. Fly again in three days."
        plain = (f"Only about {round(stressed_pct)} out of every 100 spots came "
                 f"back weak. Nothing unusual for this block.")
    elif mean > 0.22:
        headline = "Mostly fine, a few spots to check."
        advice = "Walk the red patches in the next day or two."
        plain = (f"About {round(stressed_pct)} out of every 100 spots came back "
                 f"weak — they show up red on the map.")
    else:
        headline = "Several rows need attention."
        advice = "Check the water lines on the red rows today."
        plain = (f"About {round(stressed_pct)} out of every 100 spots came back "
                 f"weak. The red areas are where to start.")
    if not has_gps:
        plain += (" These photos had no GPS fix, so they could not be placed on "
                  "the map.")
    return {"headline": headline, "plain": plain, "advice": advice}
