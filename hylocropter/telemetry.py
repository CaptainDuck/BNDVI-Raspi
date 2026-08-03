"""
MAVLink telemetry from the Pixhawk.

The Pi is a passenger. Mission Planner flies the aircraft; this module only
*reads* — link state, flight mode, arm state, battery, GPS fix, position, the
loaded mission, and camera-trigger events. It never arms, never commands, never
uploads a mission. That boundary is deliberate (see DEPLOYMENT.md) and should
not be relaxed.

Design constraints that shaped this:

* **Nothing here may block or crash the dashboard.** pymavlink missing, no
  serial port, wrong baud, or a silent bus all resolve to the same
  "not connected" snapshot. The Debug view has to keep working on a bench with
  no drone attached.
* **One reader thread, one snapshot.** The UI polls a plain dict. No queues, no
  per-request connections.
* **Staleness is explicit.** A link that stops mid-flight looks identical to a
  live one if you only check "did we ever connect", so every snapshot carries
  the age of the last heartbeat.

⚠️ UNVERIFIED AGAINST HARDWARE. There was no flight controller and no SITL
available when this was written, so the not-connected paths are well tested but
the message decoding is not. Test against ArduPilot SITL over UDP first — set
`mavlink_connection` to `udp:127.0.0.1:14550` in Settings. See RESEARCH-GAPS.md
section 7.
"""

import logging
import threading
import time

log = logging.getLogger("hylocropter.telemetry")

# No heartbeat for this long and we call the link dead. ArduPilot sends
# HEARTBEAT at 1 Hz, so 3 s is three missed beats.
HEARTBEAT_TIMEOUT_S = 3.0
RECONNECT_DELAY_S = 5.0

# ArduPilot copter mode numbers -> names, for the modes this project sees.
# AUTO is the one that matters: a mission is running.
COPTER_MODES = {
    0: "STABILIZE", 1: "ACROBATIC", 2: "ALT_HOLD", 3: "AUTO", 4: "GUIDED",
    5: "LOITER", 6: "RTL", 7: "CIRCLE", 9: "LAND", 16: "POSHOLD",
    17: "BRAKE", 20: "GUIDED_NOGPS", 21: "SMART_RTL",
}

GPS_FIX_LABELS = {
    0: "no GPS", 1: "no fix", 2: "2D fix", 3: "3D fix",
    4: "3D DGPS", 5: "RTK float", 6: "RTK fixed",
}


def _blank_snapshot():
    return {
        "connected": False,
        "status": "disconnected",     # disconnected|connecting|connected|stale
        "detail": "not connected",
        "connection": None,
        "armed": False,
        "mode": None,
        "battery_pct": None,
        "battery_v": None,
        "gps": {"fix_type": 0, "fix_label": "no GPS", "satellites": 0,
                "hdop": None},
        "position": None,             # {lat, lon, alt_m, rel_alt_m, heading_deg}
        "mission": {"count": 0, "current": 0, "altitude_m": None,
                    "line_spacing_m": None, "loaded": False},
        "trigger_count": 0,
        "last_heartbeat_age_s": None,
        "messages_seen": 0,
    }


class TelemetryService:
    """Background MAVLink reader exposing a single snapshot dict."""

    def __init__(self, settings, on_trigger=None, on_arm_change=None):
        self.settings = settings
        self.on_trigger = on_trigger
        self.on_arm_change = on_arm_change
        self._lock = threading.Lock()
        self._snap = _blank_snapshot()
        self._thread = None
        self._stop = threading.Event()
        self._conn = None
        self._last_heartbeat = 0.0
        self._armed = False
        self._mission_items = {}
        self._unavailable_reason = None

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self):
        if not self.settings.get("mavlink_enabled", True):
            self._set(status="disabled", detail="MAVLink is switched off in Settings")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mavlink",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._close()

    def reconnect(self):
        """The 'Reconnect to the drone' action."""
        log.info("MAVLink reconnect requested")
        self._close()
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None
        self._set(**_blank_snapshot())
        self.start()
        return self.snapshot()

    # ── snapshot ──────────────────────────────────────────────────────────

    def snapshot(self):
        """Current telemetry, with staleness resolved at read time."""
        with self._lock:
            snap = dict(self._snap)
            snap["gps"] = dict(snap["gps"])
            snap["mission"] = dict(snap["mission"])
            if snap["position"]:
                snap["position"] = dict(snap["position"])

        if self._last_heartbeat:
            age = time.time() - self._last_heartbeat
            snap["last_heartbeat_age_s"] = round(age, 1)
            if age > HEARTBEAT_TIMEOUT_S and snap["status"] == "connected":
                # Held data is still shown -- the mockup's "Showing the last
                # data saved on this device" banner -- but flagged as stale.
                snap["status"] = "stale"
                snap["connected"] = False
                snap["detail"] = f"no heartbeat for {age:.0f}s"
        return snap

    def _set(self, **fields):
        with self._lock:
            self._snap.update(fields)

    # ── the reader ────────────────────────────────────────────────────────

    def _run(self):
        while not self._stop.is_set():
            if not self._connect():
                time.sleep(RECONNECT_DELAY_S)
                continue
            try:
                self._pump()
            except Exception as exc:
                log.warning("MAVLink read failed: %s", exc)
                self._set(status="disconnected", connected=False,
                          detail=f"link error: {exc}")
                self._close()
                time.sleep(RECONNECT_DELAY_S)

    def _connect(self):
        try:
            from pymavlink import mavutil
        except ImportError:
            if self._unavailable_reason != "pymavlink":
                log.warning("pymavlink is not installed — telemetry disabled")
                self._unavailable_reason = "pymavlink"
            self._set(status="unavailable", connected=False,
                      detail="pymavlink is not installed (pip install pymavlink)")
            self._stop.wait(30)
            return False

        target = str(self.settings.get("mavlink_connection"))
        baud = int(self.settings.get("mavlink_baud", 57600))
        # A bare device path means serial; anything with a scheme is passed
        # straight through, so udp:127.0.0.1:14550 works for SITL.
        device = target if ":" in target else target
        self._set(status="connecting", connected=False, connection=target,
                  detail=f"opening {target}")
        try:
            self._conn = mavutil.mavlink_connection(
                device, baud=baud, source_system=255, autoreconnect=False)
        except Exception as exc:
            self._set(status="disconnected", connected=False,
                      detail=f"cannot open {target}: {exc}")
            return False

        # Wait for a heartbeat rather than assuming an open port means a drone.
        # A serial device that exists but has nothing on it is the common case
        # on a bench, and it must not look like success.
        try:
            hb = self._conn.wait_heartbeat(timeout=6)
        except Exception as exc:
            hb = None
            log.debug("wait_heartbeat raised: %s", exc)
        if hb is None:
            self._set(status="disconnected", connected=False,
                      detail=f"opened {target} but no heartbeat — is the flight "
                             f"controller powered and the baud right?")
            self._close()
            return False

        self._last_heartbeat = time.time()
        self._set(status="connected", connected=True,
                  detail=f"connected on {target}")
        log.info("MAVLink connected on %s (system %s)", target,
                 self._conn.target_system)
        self._request_streams()
        self._request_mission()
        return True

    def _request_streams(self):
        """Ask for the data we need at a modest rate.

        Deliberately low: the Pi is also running the camera and the web app, and
        4 Hz position is far more than enough to geotag a photo.
        """
        try:
            from pymavlink import mavutil
            self._conn.mav.request_data_stream_send(
                self._conn.target_system, self._conn.target_component,
                mavutil.mavlink.MAV_DATA_STREAM_ALL, 4, 1)
        except Exception as exc:
            log.debug("could not request data streams: %s", exc)

    def _request_mission(self):
        try:
            self._conn.mav.mission_request_list_send(
                self._conn.target_system, self._conn.target_component)
        except Exception as exc:
            log.debug("could not request mission list: %s", exc)

    def _pump(self):
        seen = 0
        while not self._stop.is_set():
            msg = self._conn.recv_match(blocking=True, timeout=1.0)
            if msg is None:
                # Timeout is normal; snapshot() decides if that means stale.
                continue
            seen += 1
            self._handle(msg)
            if seen % 20 == 0:
                self._set(messages_seen=seen)

    def _handle(self, msg):
        kind = msg.get_type()

        if kind == "HEARTBEAT":
            self._last_heartbeat = time.time()
            armed = bool(msg.base_mode & 0x80)   # MAV_MODE_FLAG_SAFETY_ARMED
            mode = COPTER_MODES.get(msg.custom_mode, f"mode {msg.custom_mode}")
            self._set(status="connected", connected=True, armed=armed, mode=mode,
                      detail=f"connected — {mode}")
            if armed != self._armed:
                self._armed = armed
                log.info("aircraft %s", "ARMED" if armed else "DISARMED")
                if self.on_arm_change:
                    try:
                        self.on_arm_change(armed, self.snapshot())
                    except Exception:
                        log.exception("arm-change handler failed")

        elif kind in ("SYS_STATUS", "BATTERY_STATUS"):
            pct = getattr(msg, "battery_remaining", None)
            volts = getattr(msg, "voltage_battery", None)
            if kind == "BATTERY_STATUS":
                cells = [v for v in getattr(msg, "voltages", []) if 0 < v < 65535]
                volts = sum(cells) if cells else None
            self._set(
                battery_pct=(pct if pct not in (None, -1) else None),
                battery_v=(round(volts / 1000.0, 2)
                           if volts not in (None, 0, 65535) else None),
            )

        elif kind == "GPS_RAW_INT":
            hdop = getattr(msg, "eph", None)
            self._set(gps={
                "fix_type": msg.fix_type,
                "fix_label": GPS_FIX_LABELS.get(msg.fix_type,
                                                f"fix {msg.fix_type}"),
                "satellites": msg.satellites_visible,
                # eph is cm of horizontal dilution; report it in metres.
                "hdop": (round(hdop / 100.0, 2)
                         if hdop not in (None, 65535) else None),
            })

        elif kind == "GLOBAL_POSITION_INT":
            # lat/lon are 1e7-scaled degrees; altitudes are millimetres;
            # hdg is centidegrees.
            self._set(position={
                "lat": msg.lat / 1e7,
                "lon": msg.lon / 1e7,
                "alt_m": round(msg.alt / 1000.0, 2),
                "rel_alt_m": round(msg.relative_alt / 1000.0, 2),
                "heading_deg": (round(msg.hdg / 100.0, 1)
                                if msg.hdg != 65535 else None),
            })

        elif kind == "MISSION_COUNT":
            with self._lock:
                self._snap["mission"]["count"] = msg.count
                self._snap["mission"]["loaded"] = msg.count > 0
            self._mission_items.clear()
            # Pull the items so we can report altitude and line spacing.
            for seq in range(min(msg.count, 200)):
                try:
                    self._conn.mav.mission_request_int_send(
                        self._conn.target_system, self._conn.target_component,
                        seq)
                except Exception:
                    break

        elif kind in ("MISSION_ITEM_INT", "MISSION_ITEM"):
            scale = 1e7 if kind == "MISSION_ITEM_INT" else 1.0
            self._mission_items[msg.seq] = (msg.x / scale, msg.y / scale, msg.z)
            self._update_mission_geometry()

        elif kind == "MISSION_CURRENT":
            with self._lock:
                self._snap["mission"]["current"] = msg.seq

        elif kind in ("CAMERA_TRIGGER", "CAMERA_FEEDBACK"):
            with self._lock:
                self._snap["trigger_count"] += 1
            if self.on_trigger:
                try:
                    self.on_trigger(self._trigger_geo(msg))
                except Exception:
                    log.exception("camera-trigger handler failed")

    def _trigger_geo(self, msg):
        """Position for a triggered capture.

        CAMERA_FEEDBACK carries the position the controller recorded at shutter
        time, which is more accurate than whatever GLOBAL_POSITION_INT we
        happen to hold. Prefer it when present.
        """
        if msg.get_type() == "CAMERA_FEEDBACK":
            snap = self.snapshot()
            return {
                "lat": msg.lat / 1e7,
                "lon": msg.lng / 1e7,
                "alt_m": round(getattr(msg, "alt_msl", 0.0), 2),
                "rel_alt_m": round(getattr(msg, "alt_rel", 0.0), 2),
                "heading_deg": snap["position"]["heading_deg"] if snap["position"] else None,
                "fix_type": snap["gps"]["fix_type"],
                "satellites": snap["gps"]["satellites"],
                "hdop": snap["gps"]["hdop"],
                "source": "mavlink",
            }
        return self.geo_now()

    def geo_now(self):
        """Best-available geotag right now, or None with no fix."""
        snap = self.snapshot()
        pos = snap["position"]
        if not pos or snap["gps"]["fix_type"] < 2:
            return None
        return {
            "lat": pos["lat"], "lon": pos["lon"],
            "alt_m": pos["alt_m"], "rel_alt_m": pos["rel_alt_m"],
            "heading_deg": pos["heading_deg"],
            "fix_type": snap["gps"]["fix_type"],
            "satellites": snap["gps"]["satellites"],
            "hdop": snap["gps"]["hdop"],
            "source": "mavlink",
        }

    def _update_mission_geometry(self):
        """Derive mission altitude and line spacing from the waypoints.

        Line spacing is the median distance between consecutive parallel legs.
        It is only an estimate, but it is what the pre-flight card reports, and
        deriving it beats asking the user to type it in twice.
        """
        items = [v for _, v in sorted(self._mission_items.items())]
        pts = [(lat, lon, alt) for lat, lon, alt in items
               if lat or lon]
        if len(pts) < 2:
            return
        alts = [alt for _, _, alt in pts if alt]
        gaps = []
        for i in range(len(pts) - 1):
            (la1, lo1, _), (la2, lo2, _) = pts[i], pts[i + 1]
            gaps.append(_haversine_m(la1, lo1, la2, lo2))
        with self._lock:
            self._snap["mission"]["altitude_m"] = (
                round(sum(alts) / len(alts), 1) if alts else None)
            if gaps:
                short = sorted(g for g in gaps if g > 0.5)
                self._snap["mission"]["line_spacing_m"] = (
                    round(short[len(short) // 2], 1) if short else None)

    def _close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


def _haversine_m(lat1, lon1, lat2, lon2):
    import math
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))
