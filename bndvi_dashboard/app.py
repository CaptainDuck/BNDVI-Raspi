#!/usr/bin/env python3
"""
BNDVI Plant-Stress Dashboard
=============================
Flask web app that drives the Pi NoIR + blue-filter camera and renders
each capture as a BNDVI heatmap, false-colour image, and time-series.

Run on the Pi:
    python app.py                       # camera mode
    python app.py --dev                 # synthetic frames (laptop testing)
    python app.py --host 0.0.0.0        # expose on the LAN
"""

import argparse
import json
import os
import sys
import threading
from pathlib import Path

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

import bndvi

BASE_DIR = Path(__file__).parent.resolve()
OUTPUT_DIR = BASE_DIR / "bndvi_output"
INDEX_FILE = OUTPUT_DIR / "captures.json"

OUTPUT_DIR.mkdir(exist_ok=True)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["DEV_MODE"] = False

# Serialise capture access — picamera2 doesn't like concurrent callers
_capture_lock = threading.Lock()


# ── index file ───────────────────────────────────────────────────────────────

def load_index():
    if not INDEX_FILE.exists():
        return []
    try:
        return json.loads(INDEX_FILE.read_text())
    except json.JSONDecodeError:
        return []


def save_index(records):
    INDEX_FILE.write_text(json.dumps(records, indent=2))


def find_record(records, capture_id):
    return next((r for r in records if r["id"] == capture_id), None)


# ── routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    records = sorted(load_index(), key=lambda r: r["timestamp"], reverse=True)
    chart_points = [
        {"id": r["id"], "timestamp": r["timestamp"], "mean": r["stats"]["mean"]}
        for r in sorted(records, key=lambda r: r["timestamp"])
    ]
    return render_template(
        "index.html",
        records=records,
        chart_points=chart_points,
        dev_mode=app.config["DEV_MODE"],
    )


@app.route("/capture/<capture_id>")
def detail(capture_id):
    record = find_record(load_index(), capture_id)
    if record is None:
        abort(404)
    return render_template("detail.html", r=record)


@app.route("/captures/<path:filename>")
def captures_file(filename):
    return send_from_directory(OUTPUT_DIR, filename)


# ── API ──────────────────────────────────────────────────────────────────────

@app.route("/api/captures", methods=["GET"])
def api_list():
    return jsonify(sorted(load_index(), key=lambda r: r["timestamp"], reverse=True))


@app.route("/api/captures", methods=["POST"])
def api_capture():
    payload = request.get_json(silent=True) or {}
    label = payload.get("label") or request.form.get("label")
    notes = payload.get("notes") or request.form.get("notes")
    correct_nir = bool(payload.get("correct_nir_leakage", False))
    try:
        k = float(payload.get("nir_leak_coef", bndvi.DEFAULT_NIR_LEAK_COEF))
    except (TypeError, ValueError):
        return jsonify({"error": "nir_leak_coef must be numeric"}), 400

    if not _capture_lock.acquire(blocking=False):
        return jsonify({"error": "another capture is in progress"}), 409
    try:
        record = bndvi.capture_and_analyse(
            OUTPUT_DIR,
            label=label,
            notes=notes,
            dev_mode=app.config["DEV_MODE"],
            correct_nir_leakage=correct_nir,
            nir_leak_coef=k,
        )
    except Exception as exc:
        app.logger.exception("capture failed")
        return jsonify({"error": str(exc)}), 500
    finally:
        _capture_lock.release()

    records = load_index()
    records.append(record)
    save_index(records)
    return jsonify(record), 201


@app.route("/api/captures/<capture_id>", methods=["GET"])
def api_get(capture_id):
    record = find_record(load_index(), capture_id)
    if record is None:
        abort(404)
    return jsonify(record)


@app.route("/api/captures/<capture_id>", methods=["PATCH"])
def api_update(capture_id):
    payload = request.get_json(silent=True) or {}
    records = load_index()
    record = find_record(records, capture_id)
    if record is None:
        abort(404)
    if "label" in payload:
        record["label"] = payload["label"] or None
    if "notes" in payload:
        record["notes"] = payload["notes"] or None
    save_index(records)
    return jsonify(record)


@app.route("/api/captures/<capture_id>", methods=["DELETE"])
def api_delete(capture_id):
    records = load_index()
    record = find_record(records, capture_id)
    if record is None:
        abort(404)
    for fname in record.get("files", {}).values():
        path = OUTPUT_DIR / fname
        if path.exists():
            path.unlink()
    records = [r for r in records if r["id"] != capture_id]
    save_index(records)
    return ("", 204)


# ── entry point ──────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--dev", action="store_true",
                   help="use synthetic frames instead of the camera")
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    app.config["DEV_MODE"] = args.dev
    if args.dev:
        print("[INFO] DEV MODE — using synthetic frames (no camera).")
    print(f"[INFO] Output dir: {OUTPUT_DIR}")
    print(f"[INFO] Open http://{args.host}:{args.port}/")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)