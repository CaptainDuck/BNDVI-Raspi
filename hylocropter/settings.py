"""
Persisted settings for Hylocropter.

Previously the only way to change exposure, gain or resolution was to edit
constants in `bndvi.py` with a text editor -- CALIBRATION.md literally says so.
Everything tunable now lives here, is editable from the dashboard, survives a
restart, and is recorded onto every capture so old records stay self-describing.

Writes are atomic (temp file + os.replace) because this runs on an SD card in a
drone; a power cut mid-write must not leave a truncated JSON file that bricks
the boot.
"""

import json
import os
import tempfile
import threading
from pathlib import Path

import bndvi

# Resolutions offered in the UI. The mockup's segmented control shows three.
RESOLUTIONS = [(640, 480), (1280, 960), (3280, 2464)]

DEFAULTS = {
    # ── camera ────────────────────────────────────────────────────────────
    "exposure_us": bndvi.DEFAULT_EXPOSURE_US,
    "gain": bndvi.DEFAULT_GAIN,
    "warmup_s": bndvi.DEFAULT_WARMUP_S,
    "resolution": list(bndvi.DEFAULT_RESOLUTION),
    "colour_gains": list(bndvi.DEFAULT_COLOUR_GAINS),
    "capture_format": "rgb888",          # or "raw_dng"
    # Load a tuning override that turns off the ISP stages no control can reach:
    # the colour correction matrix (which otherwise mixes GREEN into both R and
    # B), the adaptive tone curve, and per-channel lens shading. On by default
    # because leaving them on is a first-order threat to the index -- see
    # bndvi.neutral_tuning() and RESEARCH-GAPS.md section 4.
    "neutralise_isp": True,
    "save_array": True,                  # keep the float32 BNDVI for mapping

    # ── index ─────────────────────────────────────────────────────────────
    "correct_nir_leakage": False,
    "nir_leak_coef": bndvi.DEFAULT_NIR_LEAK_COEF,
    "threshold_healthy": bndvi.DEFAULT_THRESHOLD_HEALTHY,
    "threshold_moderate": bndvi.DEFAULT_THRESHOLD_MODERATE,

    # ── debug preview ─────────────────────────────────────────────────────
    "preview_fps": 12,
    "preview_scene": "mixed",            # synthetic scene when no camera

    # ── the farm ──────────────────────────────────────────────────────────
    # Brgy. Altura Bata, Tanauan City, Batangas. Change from the map -- see
    # RESEARCH-GAPS.md section 9 about the Bilog-bilog / Altura Bata mismatch.
    "plot_lat": 14.1265,
    "plot_lon": 121.0768,
    "plot_box_m": 620,                   # ~38 ha, comfortable around a 10 ha plot
    "farm_name": "Dragon fruit farm",
    "farm_location": "Tanauan, Batangas",
    "blocks": ["North block", "South block", "East trellises", "West rows",
               "Whole farm"],

    # ── flight / telemetry ────────────────────────────────────────────────
    # Serial for the real Pixhawk; swap to udp:127.0.0.1:14550 to test against
    # ArduPilot SITL with no hardware at all (see RESEARCH-GAPS.md section 7).
    "mavlink_connection": "/dev/ttyAMA0",
    "mavlink_baud": 57600,
    "mavlink_enabled": True,
    "trigger_source": "mission",         # "mission" | "dashboard"
    "trigger_mode": "distance",          # "distance" | "waypoint" | "interval"
    "trigger_distance_m": 5,
    "trigger_interval_s": 2,

    # ── offline map ───────────────────────────────────────────────────────
    "tile_zoom_min": 16,
    "tile_zoom_max": 19,
}

# Bounds for numeric settings, matching the mockup's slider ranges. Anything
# outside is clamped rather than rejected -- a bad value in a text field
# shouldn't be able to wedge the dashboard.
_LIMITS = {
    "exposure_us": (500, 200_000),
    "gain": (1.0, 16.0),
    "warmup_s": (0.0, 10.0),
    "nir_leak_coef": (0.0, 2.0),
    "threshold_healthy": (-0.9, 0.95),
    "threshold_moderate": (-0.95, 0.9),
    "preview_fps": (1, 24),
    "plot_lat": (-90.0, 90.0),
    "plot_lon": (-180.0, 180.0),
    "plot_box_m": (100, 4000),
    "trigger_distance_m": (1, 200),
    "trigger_interval_s": (1, 120),
    "mavlink_baud": (1200, 921_600),
    "tile_zoom_min": (10, 21),
    "tile_zoom_max": (10, 21),
}

_INTS = {"exposure_us", "preview_fps", "plot_box_m", "trigger_distance_m",
         "trigger_interval_s", "mavlink_baud", "tile_zoom_min", "tile_zoom_max"}

_lock = threading.Lock()


class Settings:
    """Dict-like settings backed by a JSON file."""

    def __init__(self, path):
        self.path = Path(path)
        self._values = dict(DEFAULTS)
        self.load()

    # ── persistence ───────────────────────────────────────────────────────

    def load(self):
        if not self.path.exists():
            return self._values
        try:
            stored = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            # A corrupt settings file must not stop the dashboard booting.
            return self._values
        if isinstance(stored, dict):
            # Unknown keys are dropped; missing keys keep their default. That
            # makes adding a setting a non-event for existing installs.
            for key in DEFAULTS:
                if key in stored:
                    self._values[key] = stored[key]
        return self._values

    def save(self):
        with _lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump(self._values, fh, indent=2, sort_keys=True)
                os.replace(tmp, self.path)     # atomic
            except Exception:
                Path(tmp).unlink(missing_ok=True)
                raise

    # ── access ────────────────────────────────────────────────────────────

    def __getitem__(self, key):
        return self._values[key]

    def get(self, key, default=None):
        return self._values.get(key, default)

    def as_dict(self):
        return dict(self._values)

    def update(self, patch):
        """Validate and apply a patch. Returns (applied, warnings)."""
        applied, warnings = {}, []
        for key, raw in (patch or {}).items():
            if key not in DEFAULTS:
                warnings.append(f"ignored unknown setting '{key}'")
                continue
            try:
                value = self._coerce(key, raw)
            except (TypeError, ValueError):
                warnings.append(f"'{key}' must be like {DEFAULTS[key]!r}")
                continue
            if key in _LIMITS:
                lo, hi = _LIMITS[key]
                clamped = min(max(value, lo), hi)
                if clamped != value:
                    warnings.append(
                        f"{key} clamped to {clamped} (allowed {lo}–{hi})")
                value = clamped
            self._values[key] = value
            applied[key] = value

        # The two thresholds must not cross, or the moderate band inverts and
        # the percentages stop summing to 100.
        if self._values["threshold_moderate"] >= self._values["threshold_healthy"]:
            self._values["threshold_moderate"] = round(
                self._values["threshold_healthy"] - 0.05, 3)
            applied["threshold_moderate"] = self._values["threshold_moderate"]
            warnings.append(
                "'stressed below' must stay under 'healthy above' — adjusted it")
        if self._values["tile_zoom_min"] > self._values["tile_zoom_max"]:
            self._values["tile_zoom_min"] = self._values["tile_zoom_max"]
            applied["tile_zoom_min"] = self._values["tile_zoom_min"]
            warnings.append("minimum zoom cannot exceed maximum — adjusted it")

        if applied:
            self.save()
        return applied, warnings

    def _coerce(self, key, raw):
        default = DEFAULTS[key]
        if key == "resolution":
            if isinstance(raw, str) and "x" in raw.lower().replace("×", "x"):
                w, h = raw.lower().replace("×", "x").split("x")
                raw = [int(w), int(h)]
            value = [int(raw[0]), int(raw[1])]
            if tuple(value) not in [tuple(r) for r in RESOLUTIONS]:
                raise ValueError("unsupported resolution")
            return value
        if key == "colour_gains":
            return [float(raw[0]), float(raw[1])]
        if key == "blocks":
            return [str(b) for b in raw if str(b).strip()]
        if isinstance(default, bool):
            if isinstance(raw, str):
                return raw.strip().lower() in ("1", "true", "yes", "on")
            return bool(raw)
        if key in _INTS:
            return int(float(raw))
        if isinstance(default, (int, float)):
            return float(raw)
        return str(raw)

    # ── derived helpers ───────────────────────────────────────────────────

    def camera_kwargs(self):
        """The kwargs bndvi.capture_image / capture_and_analyse expect.

        The `**cam_kwargs` path has always existed in bndvi.py but the old
        app.py never passed anything, so every web capture silently used the
        hardcoded defaults. This is what closes that gap.
        """
        return {
            "resolution": tuple(self._values["resolution"]),
            "exposure_us": self._values["exposure_us"],
            "gain": self._values["gain"],
            "warmup_s": self._values["warmup_s"],
            "colour_gains": tuple(self._values["colour_gains"]),
        }

    def analysis_kwargs(self):
        return {
            "correct_nir_leakage": self._values["correct_nir_leakage"],
            "nir_leak_coef": self._values["nir_leak_coef"],
            "threshold_healthy": self._values["threshold_healthy"],
            "threshold_moderate": self._values["threshold_moderate"],
            "save_array": self._values["save_array"],
            "capture_format": self._values["capture_format"],
            "neutralise_isp": self._values["neutralise_isp"],
        }

    def thresholds(self):
        return {"healthy": self._values["threshold_healthy"],
                "moderate": self._values["threshold_moderate"]}
