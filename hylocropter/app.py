#!/usr/bin/env python3
"""
Hylocropter — BNDVI plant-stress dashboard
===========================================
Flask app that drives the Pi NoIR + blue-filter camera, reads telemetry from the
Pixhawk, and serves the farm's stress map from the Pi itself with no internet.

This file is deliberately thin: routes, request parsing, and responses. The work
lives in the modules beside it (camera, telemetry, flights, tiles, system,
settings), so each piece can be read on its own.

Run on the Pi:
    python app.py                       # camera mode
    python app.py --dev                 # synthetic frames (laptop testing)
    python app.py --host 0.0.0.0        # expose on the LAN / the Pi's hotspot
"""

import argparse
import logging
import os
import threading
from pathlib import Path

import numpy as np
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

import applog
import bndvi
import camera as camera_mod
import flights as flights_mod
import settings as settings_mod
import system as system_mod
import telemetry as telemetry_mod
import tiles as tiles_mod

BASE_DIR = Path(__file__).parent.resolve()
REPO_ROOT = BASE_DIR.parent
# File-relative by default, so `python hylocropter/app.py` from anywhere lands in
# the same place and never leaves a stray data folder wherever you were standing.
# HYLOCROPTER_DATA overrides it for the route tests, which must not scribble on
# real flights -- but it has to be an explicit choice, never a cwd-relative one.
DATA_DIR = Path(os.environ.get("HYLOCROPTER_DATA")
                or BASE_DIR / "hylocropter_data")
TILES_DIR = BASE_DIR / "static" / "tiles"
LEGACY_DIR = REPO_ROOT / "bndvi_dashboard" / "bndvi_output"

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["DEV_MODE"] = False

log = applog.setup(DATA_DIR)

store = flights_mod.Store(DATA_DIR)
config = settings_mod.Settings(DATA_DIR / "settings.json")
cam = camera_mod.CameraService(config, dev_mode=False)
downloader = tiles_mod.TileDownloader(TILES_DIR)

# Progress for the post-landing processing view.
_processing = {"flight_id": None, "percent": 0.0, "stage": 0, "total": 0,
               "done": 0, "running": False, "message": "idle"}


# ── capture-on-trigger, driven by MAVLink ────────────────────────────────────

def _recording_flight():
    return next((f for f in store.flights() if f.get("status") == "recording"),
                None)


def _on_camera_trigger(geo):
    """Fired for every CAMERA_TRIGGER / CAMERA_FEEDBACK from the flight
    controller. Photo triggering belongs in the mission, not here — the Pi just
    obeys, and every photo carries the controller's own position."""
    flight = _recording_flight()
    if flight is None:
        log.debug("camera trigger with no flight recording — ignored")
        return
    ok, record = _do_capture(flight_id=flight["id"], geo=geo,
                             trigger=config.get("trigger_mode", "distance"))
    if ok:
        store.attach_capture(flight["id"], record["id"])


def _on_arm_change(armed, snapshot):
    """Open a flight when the aircraft arms, close it when it disarms.

    This is what makes the flight boundary automatic — the mockup's session card
    promises the page "closes the flight by itself when the drone disarms".
    """
    if armed:
        if _recording_flight() is None:
            mission = snapshot.get("mission") or {}
            flight = store.open_flight(
                name=(config.block_names() or ["Whole farm"])[0],
                trigger=("mission" if config.get("trigger_source") == "mission"
                         else config.get("trigger_mode", "distance")),
                mission={"waypoints": mission.get("count"),
                         "altitude_m": mission.get("altitude_m"),
                         "line_spacing_m": mission.get("line_spacing_m")},
                thresholds=config.thresholds())
            applog.activity(log, "Mission started — recording as %s",
                            flight["id"])
    else:
        flight = _recording_flight()
        if flight is not None:
            applog.activity(log, "Aircraft disarmed — closing %s", flight["id"])
            _start_processing(flight["id"])


tel = telemetry_mod.TelemetryService(config, on_trigger=_on_camera_trigger,
                                    on_arm_change=_on_arm_change)
device = system_mod.SystemService(store, cam, tel, config, REPO_ROOT)


# ── shared helpers ───────────────────────────────────────────────────────────

def _json_error(message, status):
    return jsonify({"error": message}), status


@app.errorhandler(404)
def _handle_404(exc=None):
    # The old app used bare abort(404), which returns Flask's HTML error page
    # even on /api/... paths — so any client calling res.json() threw.
    if request.path.startswith("/api/"):
        return _json_error("not found", 404)
    return render_template("error.html", code=404,
                           message="That page doesn't exist.",
                           **_shell()), 404


@app.errorhandler(500)
def _handle_500(exc=None):
    if request.path.startswith("/api/"):
        return _json_error("internal error", 500)
    return render_template("error.html", code=500,
                           message="Something went wrong on the device.",
                           **_shell()), 500


def _shell():
    """Context every page needs: the header chips, banner and nav."""
    probe = cam.probe()
    snap = tel.snapshot()
    cov = tiles_mod.coverage(TILES_DIR)
    banner = None
    if snap["status"] == "stale":
        banner = {"title": "Lost the link to the drone",
                  "body": ("Showing the last data saved on this device. Move "
                           "closer to the drone's Wi-Fi and it will reconnect "
                           "on its own.")}
    elif not probe.get("available") and not probe.get("synthetic"):
        banner = {"title": "Camera not detected",
                  "body": ("A new flight can't be started until the camera is "
                           "plugged in properly.")}
    elif not cov["has_tiles"]:
        banner = {"title": "No offline map downloaded yet",
                  "body": ("The farm map shows blank ground until you download "
                           "satellite imagery for the plot in Settings.")}
    return {
        "camera": probe,
        "telemetry": snap,
        "cfg": config.as_dict(),
        "coverage": cov,
        "banner": banner,
        "dev_mode": app.config["DEV_MODE"],
        "recording": _recording_flight(),
        # The drawn blocks, with their real dimensions worked out, plus just the
        # names for the All flights filter. Both derived rather than stored, so
        # renaming a block on the map updates the filter with nothing to sync.
        "blocks": _blocks_with_dims(),
        "block_names": config.block_names(),
    }


def _blocks_with_dims():
    out = []
    for block in config.get("survey_blocks") or []:
        entry = dict(block)
        entry.update(flights_mod.block_dimensions(block))
        out.append(entry)
    return out


# ── pages ────────────────────────────────────────────────────────────────────

@app.route("/")
def page_map():
    all_flights = store.flights()
    finished = [f for f in all_flights if f.get("status") != "recording"]
    requested = request.args.get("flight")
    flight = store.flight(requested) if requested else (
        finished[0] if finished else None)

    captures, summary, previous = [], None, None
    if flight:
        captures = store.captures(flight_id=flight["id"], newest_first=False)
        if flight.get("stats"):
            summary = flights_mod.summarise(
                flight["stats"]["mean"], flight["stats"]["stressed_pct"],
                has_gps=bool(flight.get("bounds")))
        idx = next((i for i, f in enumerate(all_flights)
                    if f["id"] == flight["id"]), None)
        if idx is not None:
            previous = next((f for f in all_flights[idx + 1:]
                             if f.get("stats")), None)

    # Compact, map-ready view of the captures: position, footprint and the
    # false-colour thumbnail, which is what the photo mosaic is built from.
    pins = []
    for c in captures:
        geo = c.get("geo") or {}
        if geo.get("lat") is None:
            continue
        fp = flights_mod.footprint(geo, config.get("fov_h_deg"),
                                  config.get("fov_v_deg"))
        pins.append({
            "id": c["id"],
            "lat": geo["lat"], "lon": geo["lon"],
            "heading": geo.get("heading_deg") or 0.0,
            "alt": geo.get("rel_alt_m"),
            "mean": (c.get("stats") or {}).get("mean"),
            "classification": c.get("classification"),
            "time": c["timestamp"][11:16],
            "thumb": (url_for("captures_file", filename=c["files"]["thumb"])
                      if c.get("files", {}).get("thumb") else None),
            "footprint": fp,
            "gsd_cm": flights_mod.ground_sampling_distance_cm(
                geo, (c.get("settings") or {}).get("resolution"),
                config.get("fov_h_deg")),
        })

    return render_template(
        "map.html", view="map", flight=flight, flights=all_flights,
        captures=captures, pins=pins, summary=summary, previous=previous,
        trend=[f for f in finished if f.get("stats")][:6][::-1],
        ground_captures=store.ground_captures(),
        **_shell())


@app.route("/new-flight")
def page_new_flight():
    snap = tel.snapshot()
    storage = device.storage()
    probe = cam.probe()
    gps = snap["gps"]
    checks = [
        {"ok": bool(probe.get("available")),
         "label": ("Camera is working" if probe.get("available")
                   else "Camera not detected"),
         "detail": ("Pi NoIR v2 with the blue filter, test frame looks right"
                    if probe.get("available")
                    else f"{probe.get('detail')} — open Debug to retest")},
        {"ok": storage["photos_left"] > 50,
         "label": ("Storage has room" if storage["photos_left"] > 50
                   else "Storage is nearly full"),
         "detail": (f"{storage['free_label']} free — about "
                    f"{storage['photos_left']} more photos")},
        {"ok": bool(snap["connected"]),
         "label": ("Connected to the drone" if snap["connected"]
                   else "Not connected to the drone"),
         "detail": (f"MAVLink on {snap['connection']}" if snap["connected"]
                    else snap["detail"])},
        {"ok": gps["fix_type"] >= 3,
         "label": ("GPS fix is good" if gps["fix_type"] >= 3
                   else "No GPS fix yet"),
         "detail": (f"{gps['fix_label']}, {gps['satellites']} satellites"
                    + (f", ±{gps['hdop']} m" if gps["hdop"] else ""))},
        {"ok": bool(snap["mission"]["loaded"]),
         "label": ("Mission is loaded" if snap["mission"]["loaded"]
                   else "No mission loaded"),
         "detail": (f"{snap['mission']['count']} waypoints from Mission Planner"
                    if snap["mission"]["loaded"]
                    else "Upload a mission in Mission Planner first")},
    ]
    # Rough photo count for the mission, from its length and the trigger spacing.
    est_photos = None
    mission = snap["mission"]
    if mission["count"] and mission["line_spacing_m"]:
        legs = max(1, mission["count"] - 1)
        est_photos = int(legs * mission["line_spacing_m"]
                         / max(1, config.get("trigger_distance_m")))
    # The planner scales everything to one drawn block, not to the whole
    # downloaded vicinity -- that is tens of hectares of imagery to search in, and
    # planning a mission over all of it would suggest an hour in the air for
    # ground the drone was never going to cover.
    blocks = _blocks_with_dims()
    first = blocks[0] if blocks else None
    plan = flights_mod.mission_plan(
        altitude_m=snap["mission"].get("altitude_m") or 12,
        fov_h_deg=config.get("fov_h_deg"), fov_v_deg=config.get("fov_v_deg"),
        plot_w_m=first["width_m"] if first else None,
        plot_h_m=first["height_m"] if first else None,
        resolution=tuple(config.get("resolution")))
    return render_template("newflight.html", view="newflight", checks=checks,
                           storage=storage, est_photos=est_photos, plan=plan,
                           selected_block=first,
                           test_areas=flights_mod.TEST_AREAS, **_shell())


@app.route("/plan")
def page_plan():
    """The mission planner on its own, for working numbers out before the farm.

    Deliberately independent of the camera, the flight controller and the map --
    it is trigonometry, so it has to work at a desk with the rig switched off.
    """
    blocks = _blocks_with_dims()
    # Default to a football field rather than a block: someone opening this page
    # with nothing drawn is almost certainly planning a rehearsal.
    default = blocks[0] if blocks else flights_mod.test_area_by_id("t-pitch")
    plan = flights_mod.mission_plan(
        altitude_m=12,
        fov_h_deg=config.get("fov_h_deg"), fov_v_deg=config.get("fov_v_deg"),
        plot_w_m=default.get("width_m", default.get("w")),
        plot_h_m=default.get("height_m", default.get("h")),
        resolution=tuple(config.get("resolution")))
    return render_template("plan.html", view="plan", plan=plan,
                           test_areas=flights_mod.TEST_AREAS,
                           usable_minutes=flights_mod.USABLE_FLIGHT_MINUTES,
                           **_shell())


@app.route("/processing")
def page_processing():
    return render_template("processing.html", view="processing",
                           progress=_processing, **_shell())


@app.route("/history")
def page_history():
    return render_template("history.html", view="history",
                           flights=store.flights(),
                           ground_captures=store.ground_captures(),
                           **_shell())


@app.route("/capture/<capture_id>")
def page_capture(capture_id):
    record = store.capture(capture_id)
    if record is None:
        return _handle_404()
    stats = record.get("stats") or {}
    summary = flights_mod.summarise(stats.get("mean", 0.0),
                                    stats.get("stressed_pct", 0.0),
                                    has_gps=bool(record.get("geo")))
    return render_template("capture.html", view="capture", r=record,
                           summary=summary,
                           flight=store.flight(record.get("flight_id")),
                           **_shell())


@app.route("/debug")
def page_debug():
    return render_template("debug.html", view="debug", scenes=bndvi.SCENES,
                           resolutions=settings_mod.RESOLUTIONS, **_shell())


@app.route("/setup")
def page_setup():
    return render_template("setup.html", view="setup", scenes=bndvi.SCENES,
                           resolutions=settings_mod.RESOLUTIONS,
                           tile_plan=_tile_plan(), **_shell())


@app.route("/settings")
def page_settings():
    return render_template("settings.html", view="settings",
                           actions=system_mod.ACTIONS,
                           device_info=device.info(),
                           resolutions=settings_mod.RESOLUTIONS,
                           tile_plan=_tile_plan(), **_shell())


@app.route("/captures/<path:filename>")
def captures_file(filename):
    """Serve a capture artefact.

    Kept at its original path for compatibility. Files now live in per-flight
    directories, so search those as well as ground/.
    """
    safe = Path(filename).name
    folders = [store.ground_dir] + [store.data_dir / f["id"]
                                    for f in store.flights()]
    for folder in folders:
        if (folder / safe).exists():
            return send_from_directory(folder, safe)
    return _handle_404()


# ── API: captures ────────────────────────────────────────────────────────────

@app.route("/api/captures", methods=["GET"])
def api_captures_list():
    wanted = request.args.get("flight")
    # No ?flight= at all means every capture; an explicit empty value
    # means the ground captures.
    return jsonify(store.captures(
        flight_id=flights_mod.ALL if wanted is None
        else (wanted or None)))


@app.route("/api/captures", methods=["POST"])
def api_capture_create():
    payload = request.get_json(silent=True) or {}
    label = payload.get("label") or request.form.get("label")
    notes = payload.get("notes") or request.form.get("notes")

    overrides = {}
    if "correct_nir_leakage" in payload:
        overrides["correct_nir_leakage"] = bool(payload["correct_nir_leakage"])
    if "nir_leak_coef" in payload:
        try:
            overrides["nir_leak_coef"] = float(payload["nir_leak_coef"])
        except (TypeError, ValueError):
            return _json_error("nir_leak_coef must be numeric", 400)

    flight = _recording_flight()
    ok, result = _do_capture(
        label=label, notes=notes,
        flight_id=flight["id"] if flight else None,
        geo=tel.geo_now(), trigger="manual",
        from_preview=bool(payload.get("from_preview")),
        overrides=overrides)
    if not ok:
        return _json_error(result, 409 if "in progress" in result else 500)
    if flight:
        store.attach_capture(flight["id"], result["id"])
    return jsonify(result), 201


def _do_capture(label=None, notes=None, flight_id=None, geo=None,
                trigger="manual", from_preview=False, overrides=None):
    """Take one capture. Returns (True, record) or (False, error_message)."""
    analysis = config.analysis_kwargs()
    analysis.update(overrides or {})
    out_dir = store.capture_dir(flight_id)

    def work():
        rgb = None
        if from_preview:
            # "Save as capture" on the Debug view: analyse the frame already on
            # screen rather than taking a new one, so what you saved is what you
            # were looking at.
            rgb, _meta = cam.latest_rgb()
        return bndvi.capture_and_analyse(
            out_dir, label=label, notes=notes,
            dev_mode=app.config["DEV_MODE"] or cam.using_synthetic(),
            flight_id=flight_id, geo=geo, trigger=trigger, rgb=rgb,
            **analysis, **config.camera_kwargs())

    try:
        acquired, record = cam.capture_locked(work)
    except bndvi.CameraUnavailable as exc:
        return False, str(exc)
    except Exception as exc:
        log.exception("capture failed")
        return False, str(exc)
    if not acquired:
        return False, "another capture is in progress"

    try:
        store.add_capture(record)
    except Exception as exc:
        log.exception("could not write the capture index")
        return False, f"capture taken but the index write failed: {exc}"
    applog.activity(log, "Capture %s — mean BNDVI %+.3f (%s)", record["id"],
                    record["stats"]["mean"], record["classification"])
    return True, record


@app.route("/api/captures/<capture_id>", methods=["GET"])
def api_capture_get(capture_id):
    record = store.capture(capture_id)
    if record is None:
        return _json_error("not found", 404)
    return jsonify(record)


@app.route("/api/captures/<capture_id>", methods=["PATCH"])
def api_capture_update(capture_id):
    record = store.update_capture(capture_id, request.get_json(silent=True) or {})
    if record is None:
        return _json_error("not found", 404)
    return jsonify(record)


@app.route("/api/captures/<capture_id>", methods=["DELETE"])
def api_capture_delete(capture_id):
    if not store.delete_capture(capture_id):
        return _json_error("not found", 404)
    return ("", 204)


# ── API: flights ─────────────────────────────────────────────────────────────

@app.route("/api/flights", methods=["GET"])
def api_flights_list():
    return jsonify(store.flights())


@app.route("/api/flights", methods=["POST"])
def api_flight_create():
    if _recording_flight() is not None:
        return _json_error("a flight is already recording", 409)
    payload = request.get_json(silent=True) or {}
    mission = tel.snapshot().get("mission") or {}
    flight = store.open_flight(
        name=payload.get("name") or (config.block_names()
                                     or ["Whole farm"])[0],
        trigger=payload.get("trigger") or config.get("trigger_mode"),
        mission={"waypoints": mission.get("count"),
                 "altitude_m": mission.get("altitude_m"),
                 "line_spacing_m": mission.get("line_spacing_m")},
        thresholds=config.thresholds())
    applog.activity(log, "Camera armed for %s — waiting for the mission",
                    flight["id"])
    return jsonify(flight), 201


@app.route("/api/flights/<flight_id>", methods=["GET"])
def api_flight_get(flight_id):
    flight = store.flight(flight_id)
    if flight is None:
        return _json_error("not found", 404)
    return jsonify({**flight,
                    "captures": store.captures(flight_id=flight_id,
                                               newest_first=False)})


@app.route("/api/flights/<flight_id>", methods=["PATCH"])
def api_flight_update(flight_id):
    payload = request.get_json(silent=True) or {}
    flight = store.update_flight(
        flight_id, {k: v for k, v in payload.items() if k in ("name", "status")})
    if flight is None:
        return _json_error("not found", 404)
    return jsonify(flight)


@app.route("/api/flights/<flight_id>", methods=["DELETE"])
def api_flight_delete(flight_id):
    freed = store.delete_flight(flight_id,
                                keep_records=request.args.get("keep_records")
                                == "true")
    if freed is False:
        return _json_error("not found", 404)
    return jsonify({"freed_bytes": freed})


@app.route("/api/flights/<flight_id>/cancel", methods=["POST"])
def api_flight_cancel(flight_id):
    if store.flight(flight_id) is None:
        return _json_error("not found", 404)
    if not store.captures(flight_id=flight_id):
        store.delete_flight(flight_id)
        return jsonify({"cancelled": True, "deleted": True})
    store.close_flight(flight_id, config.thresholds())
    return jsonify({"cancelled": True, "deleted": False})


@app.route("/api/flights/<flight_id>/process", methods=["POST"])
def api_flight_process(flight_id):
    if store.flight(flight_id) is None:
        return _json_error("not found", 404)
    _start_processing(flight_id)
    return jsonify(_processing)


@app.route("/api/processing")
def api_processing():
    return jsonify(_processing)


def _start_processing(flight_id):
    """Close and summarise a flight, reporting progress for the UI.

    Captures are already analysed as they are taken — a Pi 4 cannot run 8 MP
    BNDVI plus figure rendering per frame at a 5 s cadence *and* service
    MAVLink, so the per-photo work happens at capture time and this stage does
    the aggregation, the map grid, and the save. (The thesis contradicts itself
    on in-flight vs post-flight; see RESEARCH-GAPS.md section 10.)
    """
    if _processing["running"]:
        return _processing

    def work():
        captures = store.captures(flight_id=flight_id, newest_first=False)
        total = max(1, len(captures))
        _processing.update(flight_id=flight_id, running=True,
                           total=len(captures), done=0, percent=6.0, stage=0,
                           message="Copying photos off the camera")
        for i, _ in enumerate(captures, 1):
            _processing.update(done=i, stage=1,
                               percent=round(22 + 40 * i / total, 1),
                               message="Working out plant stress for each photo")
        _processing.update(stage=2, percent=75.0,
                           message="Placing them on the farm map")
        flight = store.close_flight(flight_id, config.thresholds())
        _processing.update(stage=3, percent=100.0, running=False,
                           message="Flight saved")
        if flight:
            applog.activity(log, "Flight %s saved — %d photos processed",
                            flight_id, flight.get("capture_count", 0))

    threading.Thread(target=work, name="processing", daemon=True).start()
    return _processing


# ── API: telemetry, camera, preview ──────────────────────────────────────────

@app.route("/api/telemetry")
def api_telemetry():
    snap = tel.snapshot()
    snap["recording_flight"] = (_recording_flight() or {}).get("id")
    snap["camera"] = cam.probe()
    return jsonify(snap)


@app.route("/api/camera/status")
def api_camera_status():
    return jsonify(cam.probe(force=request.args.get("force") == "true"))


@app.route("/api/camera/restart", methods=["POST"])
def api_camera_restart():
    return jsonify(cam.restart())


@app.route("/api/camera/synthetic", methods=["POST"])
def api_camera_synthetic():
    payload = request.get_json(silent=True) or {}
    cam.use_synthetic(bool(payload.get("on", True)))
    return jsonify(cam.probe(force=True))


@app.route("/api/preview/frame")
def api_preview_frame():
    """The live debug frame: raw NIR and blue planes, as binary.

    The browser derives BNDVI and paints all seven canvases itself. That is why
    the k and threshold sliders are instant — no round-trip — and why the four
    renders cannot drift out of sync: they all come from one array. The Pi only
    grabs, downsamples, and sends.
    """
    nir, green, blue, meta, seq = cam.latest()
    if nir is None:
        return _json_error(meta.get("error") or "no frame available", 503)
    # Three planes, not two. Green is unused by the index but the channel-split
    # panel claims to show a measured channel, and the setup wizard needs it to
    # tell whether the blue gel is fitted at all.
    payload = np.concatenate([
        nir.clip(0, 255).astype(np.uint8).ravel(),
        green.clip(0, 255).astype(np.uint8).ravel(),
        blue.clip(0, 255).astype(np.uint8).ravel(),
    ]).tobytes()
    resp = Response(payload, mimetype="application/octet-stream")
    resp.headers["X-Frame-Width"] = str(camera_mod.PREVIEW_W)
    resp.headers["X-Frame-Height"] = str(camera_mod.PREVIEW_H)
    resp.headers["X-Frame-Seq"] = str(seq)
    resp.headers["X-Frame-Planes"] = "3"
    resp.headers["X-Frame-Source"] = meta.get("source", "unknown")
    # Locks the camera silently ignored, so the Debug view can say so.
    if meta.get("mismatches"):
        resp.headers["X-Control-Mismatch"] = " | ".join(meta["mismatches"])[:400]
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/preview/scene", methods=["POST"])
def api_preview_scene():
    scene = (request.get_json(silent=True) or {}).get("scene")
    if scene not in bndvi.SCENES:
        return _json_error(f"unknown scene '{scene}'", 400)
    config.update({"preview_scene": scene})
    return jsonify({"scene": scene})


@app.route("/api/calibrate/solve-k", methods=["POST"])
def api_solve_k():
    """Derive the NIR-leakage coefficient from a box drawn on a white reference.

    White reflects about equally in NIR and visible, so it must read BNDVI ~ 0.
    Solving R = B - k*R gives k = B/R - 1. This replaces CALIBRATION.md's
    "adjust k in steps of ~0.1 and look at the picture" loop with one gesture.
    """
    payload = request.get_json(silent=True) or {}
    try:
        box = [float(payload[key]) for key in ("x0", "y0", "x1", "y1")]
    except (KeyError, TypeError, ValueError):
        return _json_error("need x0, y0, x1, y1 as fractions of the frame", 400)
    region = cam.region_means(*box)
    if region is None:
        return _json_error("no live frame to measure", 503)
    k, message = bndvi.solve_leak_coef(region["nir"], region["blue"])
    applied = False
    if k is not None and payload.get("apply"):
        config.update({"nir_leak_coef": k, "correct_nir_leakage": True})
        applied = True
        applog.activity(log, "Calibrated NIR-leakage k = %.3f from a white "
                             "reference", k)
    return jsonify({"k": k, "message": message, "applied": applied,
                    "region": region})


@app.route("/api/diagnose/filter", methods=["POST"])
def api_diagnose_filter():
    """Is the blue gel actually in the light path?

    The most common way to get meaningless numbers is a gel that fell out of the
    lens cap, because nothing complains — the frame still looks like a photo. The
    green channel gives it away; see bndvi.filter_sanity().
    """
    payload = request.get_json(silent=True) or {}
    box = payload.get("box")
    if box:
        try:
            region = cam.region_means(*[float(box[k]) for k in
                                       ("x0", "y0", "x1", "y1")])
        except (KeyError, TypeError, ValueError):
            return _json_error("box needs x0, y0, x1, y1", 400)
    else:
        nir, green, blue, meta, _ = cam.latest()
        if nir is None:
            return _json_error(meta.get("error") or "no frame available", 503)
        region = {"nir": float(nir.mean()), "green": float(green.mean()),
                  "blue": float(blue.mean()), "source": meta.get("source")}
    if region is None:
        return _json_error("no live frame to measure", 503)
    verdict, message = bndvi.filter_sanity(region["nir"], region["green"],
                                          region["blue"])
    return jsonify({"verdict": verdict, "message": message, "region": region})


@app.route("/api/diagnose/white-reference", methods=["POST"])
def api_diagnose_white():
    """Judge a white card's exposure against the 180-230 target."""
    payload = request.get_json(silent=True) or {}
    try:
        box = [float(payload[key]) for key in ("x0", "y0", "x1", "y1")]
    except (KeyError, TypeError, ValueError):
        return _json_error("need x0, y0, x1, y1 as fractions of the frame", 400)
    region = cam.region_means(*box)
    if region is None:
        return _json_error("no live frame to measure", 503)
    verdict, message = bndvi.white_reference_check(region["nir_max"],
                                                  region["blue_max"])
    k, k_message = bndvi.solve_leak_coef(region["nir"], region["blue"])
    return jsonify({"verdict": verdict, "message": message, "region": region,
                    "k": k, "k_message": k_message})


@app.route("/api/mission/plan")
def api_mission_plan():
    """What to type into Mission Planner for a given altitude and overlap."""
    def num(name, default):
        try:
            return float(request.args.get(name, default))
        except (TypeError, ValueError):
            return float(default)
    # A block id resolves to that block's real dimensions; explicit width and
    # height let the page recompute while the operator is still dragging, before
    # anything is saved.
    wanted = request.args.get("block")
    block = flights_mod.block_by_id(config.get("survey_blocks"), wanted)
    area = flights_mod.test_area_by_id(wanted)
    if block:
        dims = flights_mod.block_dimensions(block)
        plot_w, plot_h = dims["width_m"], dims["height_m"]
    elif area:
        plot_w, plot_h = area["w"], area["h"]
    else:
        plot_w = num("plot_w", flights_mod.PLACEHOLDER_BLOCK_M)
        plot_h = num("plot_h", plot_w)
    plan = flights_mod.mission_plan(
        altitude_m=num("altitude", 12),
        fov_h_deg=config.get("fov_h_deg"),
        fov_v_deg=config.get("fov_v_deg"),
        forward_overlap=num("forward", 0.40),
        side_overlap=num("side", 0.30),
        plot_w_m=plot_w, plot_h_m=plot_h,
        speed_ms=num("speed", flights_mod.DEFAULT_SURVEY_SPEED_MS),
        resolution=tuple(config.get("resolution")),
    )
    return jsonify(plan)


@app.route("/api/setup/state", methods=["GET", "POST"])
def api_setup_state():
    """Remember where the operator got to, so the wizard is resumable."""
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        patch = {}
        if "step" in payload:
            patch["setup_step"] = payload["step"]
        if "completed" in payload:
            patch["setup_completed"] = bool(payload["completed"])
        if "done_steps" in payload:
            patch["setup_done_steps"] = payload["done_steps"]
        config.update(patch)
        if patch.get("setup_completed"):
            applog.activity(log, "Guided setup completed")
    return jsonify({"step": config.get("setup_step"),
                    "completed": config.get("setup_completed"),
                    "done_steps": config.get("setup_done_steps")})


# ── API: settings, logs, system ──────────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify(config.as_dict())


@app.route("/api/settings", methods=["PUT", "PATCH"])
def api_settings_put():
    applied, warnings = config.update(request.get_json(silent=True) or {})
    if any(k in applied for k in ("exposure_us", "gain", "colour_gains")):
        cam.apply_controls()
    if "mavlink_connection" in applied or "mavlink_baud" in applied:
        tel.reconnect()
    if applied:
        log.info("settings updated: %s", ", ".join(sorted(applied)))
    return jsonify({"settings": config.as_dict(), "applied": applied,
                    "warnings": warnings})


@app.route("/api/logs")
def api_logs():
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    return jsonify(applog.recent(limit=limit,
                                 activity_only=request.args.get("activity")
                                 == "true",
                                 min_level=request.args.get("level")))


@app.route("/api/system/info")
def api_system_info():
    return jsonify({"info": device.info(), "storage": device.storage()})


@app.route("/api/system/actions/<action>", methods=["POST"])
def api_system_action(action):
    if action not in system_mod.ACTIONS:
        return _json_error(f"unknown action '{action}'", 404)
    payload = request.get_json(silent=True) or {}
    ok, message, extra = device.run(action,
                                    confirmed=bool(payload.get("confirm")))
    status = 200 if ok else (409 if "confirmation required" in message else 500)
    return jsonify({"ok": ok, "message": message, **extra}), status


# ── offline map tiles ────────────────────────────────────────────────────────

def _tile_plan():
    return tiles_mod.plan(config.get("plot_lat"), config.get("plot_lon"),
                          config.get("plot_box_m"),
                          config.get("tile_zoom_min"),
                          config.get("tile_zoom_max"))


@app.route("/tiles/<int:z>/<int:x>/<int:y>.jpg")
def tile(z, x, y):
    """Serve a basemap tile from disk, or plain sand if it isn't there.

    Returning a real image rather than a 404 matters: Leaflet renders
    broken-image icons across the whole map otherwise, which reads as a broken
    dashboard instead of "no imagery downloaded for this spot".
    """
    path = TILES_DIR / str(z) / str(x) / f"{y}.jpg"
    if path.exists():
        return send_from_directory(path.parent, path.name,
                                   max_age=60 * 60 * 24 * 30)
    resp = Response(tiles_mod.fallback_tile(), mimetype="image/jpeg")
    resp.headers["X-Tile-Missing"] = "1"
    return resp


@app.route("/api/tiles/coverage")
def api_tiles_coverage():
    return jsonify({"coverage": tiles_mod.coverage(TILES_DIR),
                    "plan": _tile_plan()})


@app.route("/api/tiles/plan")
def api_tiles_plan():
    def num(name, default):
        try:
            return float(request.args.get(name, default))
        except (TypeError, ValueError):
            return float(default)
    return jsonify(tiles_mod.plan(
        num("lat", config.get("plot_lat")),
        num("lon", config.get("plot_lon")),
        num("box_m", config.get("plot_box_m")),
        int(num("zoom_min", config.get("tile_zoom_min"))),
        int(num("zoom_max", config.get("tile_zoom_max")))))


@app.route("/api/tiles/download", methods=["POST"])
def api_tiles_download():
    payload = request.get_json(silent=True) or {}
    patch = {k: payload[k] for k in
             ("plot_lat", "plot_lon", "plot_box_m", "tile_zoom_min",
              "tile_zoom_max") if k in payload}
    if patch:
        config.update(patch)
    ok, message = downloader.start(
        config.get("plot_lat"), config.get("plot_lon"),
        config.get("plot_box_m"), config.get("tile_zoom_min"),
        config.get("tile_zoom_max"))
    return jsonify({"ok": ok, "message": message}), (200 if ok else 409)


@app.route("/api/tiles/progress")
def api_tiles_progress():
    return jsonify(downloader.progress())


@app.route("/api/tiles/cancel", methods=["POST"])
def api_tiles_cancel():
    downloader.cancel()
    return jsonify({"cancelled": True})


@app.route("/index")
def legacy_index():
    return redirect(url_for("page_map"))


# ── entry point ──────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Hylocropter dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--dev", action="store_true",
                   help="use synthetic frames instead of the camera")
    p.add_argument("--debug", action="store_true", help="Flask reloader")
    return p.parse_args()


def bootstrap(dev_mode=False, debug=False):
    """Wire everything up. Separate from __main__ so tests can call it."""
    app.config["DEV_MODE"] = dev_mode
    cam.dev_mode = dev_mode
    logging.getLogger().setLevel(logging.DEBUG if debug else logging.INFO)

    moved = store.migrate_legacy(LEGACY_DIR)
    if moved:
        applog.activity(log, "Brought forward %d captures from the old "
                             "bndvi_output folder", moved)

    cam.probe(force=True)
    cam.start_preview()
    tel.start()

    cov = tiles_mod.coverage(TILES_DIR)
    if cov["has_tiles"]:
        log.info("offline map: %s tiles, %s, %s", cov["tiles"],
                 cov["size_label"], cov.get("extent_label", "extent unknown"))
    else:
        log.warning("no offline map tiles yet — download them from Settings "
                    "while this machine has internet")
    return app


if __name__ == "__main__":
    args = parse_args()
    bootstrap(dev_mode=args.dev, debug=args.debug)
    if args.dev:
        log.info("DEV MODE — synthetic frames, no camera")
    log.info("data dir: %s", DATA_DIR)
    log.info("open http://%s:%s/", args.host, args.port)
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
