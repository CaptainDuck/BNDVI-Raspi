"""
Device actions — the "Run the device" panel.

Everything here exists so the operator never has to open a terminal. That is the
whole point, so the actions are deliberately the ones you would otherwise SSH in
to do: restart the camera, reconnect the link, copy flights to a USB stick, free
space, check for updates, shut down.

Two rules:

* **Destructive actions return a confirmation contract, they don't act.** The
  route layer requires an explicit `confirm=true` before anything is deleted or
  the Pi powers off. The mockup already models this with a dialog; this mirrors it
  server-side so a stray POST cannot wipe a season of flights.
* **Nothing runs as root.** The app runs as the normal user. The two actions that
  genuinely need privileges (poweroff, mounting a USB stick) shell out to
  specific commands that a documented sudoers snippet allows — see DEPLOYMENT.md.
  Running the whole dashboard as root to save that config would be a bad trade on
  a device that serves a web page to a field Wi-Fi network.
"""

import datetime
import logging
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path

import applog

log = logging.getLogger("hylocropter.system")

# Where a USB stick usually appears on Raspberry Pi OS Bookworm.
USB_MOUNT_ROOTS = ("/media/pi", f"/media/{os.environ.get('USER', 'pi')}",
                   "/media", "/mnt")

ACTIONS = {
    "restart-camera": {
        "label": "Restart the camera",
        "danger": False,
    },
    "reconnect-drone": {
        "label": "Reconnect to the drone",
        "danger": False,
    },
    "copy-to-usb": {
        "label": "Copy all flights to a USB stick",
        "danger": False,
    },
    "check-update": {
        "label": "Check for a software update",
        "danger": False,
    },
    "free-space": {
        "label": "Free up space",
        "danger": True,
        "confirm": {
            "title": "Delete the oldest flights?",
            "body": ("This removes flights older than 30 days and their photos. "
                     "The numbers stay in the record."),
            "action": "Delete old photos",
        },
    },
    "shutdown": {
        "label": "Shut down the Pi",
        "danger": True,
        "confirm": {
            "title": "Shut down the device?",
            "body": ("Wait for the green light to stop blinking before "
                     "unplugging power."),
            "action": "Shut down",
        },
    },
}

PURGE_AGE_DAYS = 30


class SystemService:
    def __init__(self, store, camera, telemetry, settings, repo_root):
        self.store = store
        self.camera = camera
        self.telemetry = telemetry
        self.settings = settings
        self.repo_root = Path(repo_root)

    # ── device info ───────────────────────────────────────────────────────

    def info(self):
        cam = self.camera.probe()
        tel = self.telemetry.snapshot()
        free, total = _disk_free(self.store.data_dir)
        return [
            {"label": "Device", "value": _pi_model()},
            {"label": "Camera",
             "value": (f"Pi NoIR Camera v2 + Rosco #2007"
                       if cam.get("available")
                       else ("synthetic frames" if cam.get("synthetic")
                             else "not detected"))},
            {"label": "Airframe", "value": "F450 quadcopter"},
            {"label": "CPU temperature", "value": _cpu_temp()},
            {"label": "Storage free",
             "value": f"{_gb(free)} of {_gb(total)}"},
            {"label": "Flight data", "value": _gb(self.store.disk_usage())},
            {"label": "Connection",
             "value": (tel.get("connection") or "not connected")
             if tel.get("connected") else "not connected"},
            {"label": "Uptime", "value": _uptime()},
            {"label": "Software", "value": _version(self.repo_root)},
        ]

    def storage(self):
        free, total = _disk_free(self.store.data_dir)
        # A full-resolution capture set (raw + heatmap + false colour + thumb +
        # npz) measures around 6 MB in practice.
        per_capture = 6 * 1024 * 1024
        return {
            "free_bytes": free, "total_bytes": total,
            "free_label": _gb(free), "total_label": _gb(total),
            "photos_left": int(free / per_capture),
        }

    # ── actions ───────────────────────────────────────────────────────────

    def run(self, action, confirmed=False):
        """Execute an action. Returns (ok, message, extra)."""
        spec = ACTIONS.get(action)
        if spec is None:
            return False, f"Unknown action '{action}'.", {}
        if spec["danger"] and not confirmed:
            # Hand the dialog copy back rather than acting.
            return False, "confirmation required", {"confirm": spec["confirm"]}

        handler = getattr(self, "_" + action.replace("-", "_"))
        try:
            ok, message, extra = handler()
        except Exception as exc:
            log.exception("action %s failed", action)
            return False, f"{spec['label']} failed: {exc}", {}
        applog.activity(log, "%s — %s", spec["label"], message)
        return ok, message, extra

    def _restart_camera(self):
        probe = self.camera.restart()
        if probe.get("available"):
            return True, "Camera restarted. Test frame looks right.", {"probe": probe}
        if probe.get("synthetic"):
            return True, ("No camera found, so the dashboard is using synthetic "
                          "frames."), {"probe": probe}
        return False, f"Camera still not detected — {probe.get('detail')}", {
            "probe": probe}

    def _reconnect_drone(self):
        snap = self.telemetry.reconnect()
        if snap.get("connected"):
            return True, "Reconnected — MAVLink stream is live again.", {}
        return False, f"Still not connected — {snap.get('detail')}", {}

    def _copy_to_usb(self):
        target = _find_usb()
        if target is None:
            return False, ("No USB stick found. Plug one in and try again — it "
                           "should appear under /media."), {}
        dest = Path(target) / ("hylocropter-" +
                               datetime.datetime.now().strftime("%Y%m%d-%H%M"))
        size = self.store.disk_usage()
        free, _ = _disk_free(target)
        if size > free:
            return False, (f"The stick has {_gb(free)} free but the flight data "
                           f"is {_gb(size)}. Use a bigger stick."), {}
        shutil.copytree(self.store.data_dir, dest, dirs_exist_ok=True)
        # Flush to the stick before saying it's safe to pull out.
        os.sync()
        return True, (f"Copied {_gb(size)} of flight data to {dest.name}. "
                      f"You can remove the stick."), {"path": str(dest)}

    def _check_update(self):
        version = _version(self.repo_root)
        try:
            subprocess.run(["git", "fetch", "--quiet"], cwd=self.repo_root,
                           timeout=20, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except (subprocess.SubprocessError, OSError, FileNotFoundError):
            return True, (f"You're on {version}. Couldn't check for updates — "
                          f"this device has no internet right now, which is "
                          f"normal in the field."), {}
        behind = _run(["git", "rev-list", "--count", "HEAD..@{u}"],
                      self.repo_root)
        if behind and behind.strip().isdigit() and int(behind.strip()) > 0:
            return True, (f"An update is available ({behind.strip()} new "
                          f"commits). Run: git pull && sudo systemctl restart "
                          f"hylocropter"), {"behind": int(behind.strip())}
        return True, f"You're on the latest version ({version}).", {}

    def _free_space(self):
        cutoff = datetime.datetime.now() - datetime.timedelta(
            days=PURGE_AGE_DAYS)
        freed, purged = 0, 0
        for flight in self.store.flights():
            started = flight.get("started_at")
            if not started or flight.get("files_purged"):
                continue
            try:
                when = datetime.datetime.fromisoformat(started)
            except ValueError:
                continue
            if when >= cutoff:
                continue
            # keep_records=True: the photos go, the measurements stay. That is
            # exactly what the confirm dialog promises the user.
            got = self.store.delete_flight(flight["id"], keep_records=True)
            if got:
                freed += got
                purged += 1
        if not purged:
            return True, (f"Nothing to delete — no flights are older than "
                          f"{PURGE_AGE_DAYS} days."), {"freed": 0}
        return True, (f"Freed {_gb(freed)} — {purged} old flights had their "
                      f"photos removed. The numbers stayed."), {"freed": freed}

    def _shutdown(self):
        for cmd in (["sudo", "-n", "systemctl", "poweroff"],
                    ["systemctl", "poweroff"]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return True, ("Shutting down. You can unplug when the green "
                              "light stops blinking."), {}
            except (OSError, FileNotFoundError):
                continue
        return False, ("Couldn't shut down — the dashboard isn't allowed to run "
                       "poweroff. See the sudoers snippet in DEPLOYMENT.md."), {}


# ── small helpers ────────────────────────────────────────────────────────────

def _run(cmd, cwd=None):
    try:
        out = subprocess.run(cmd, cwd=cwd, timeout=10, check=False,
                             capture_output=True, text=True)
        return out.stdout.strip()
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        return ""


def _pi_model():
    model = Path("/proc/device-tree/model")
    if model.exists():
        try:
            return model.read_text().strip("\x00").strip()
        except OSError:
            pass
    return f"{platform.system()} {platform.machine()} (not a Raspberry Pi)"


def _cpu_temp():
    p = Path("/sys/class/thermal/thermal_zone0/temp")
    if p.exists():
        try:
            return f"{int(p.read_text().strip()) / 1000:.0f} °C"
        except (OSError, ValueError):
            pass
    return "unavailable"


def _uptime():
    p = Path("/proc/uptime")
    if not p.exists():
        return "unavailable"
    try:
        seconds = float(p.read_text().split()[0])
    except (OSError, ValueError):
        return "unavailable"
    hours, minutes = divmod(int(seconds // 60), 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _disk_free(path):
    try:
        usage = shutil.disk_usage(path)
        return usage.free, usage.total
    except OSError:
        return 0, 0


def _gb(byte_count):
    if byte_count >= 1024 ** 3:
        return f"{byte_count / 1024 ** 3:.1f} GB"
    if byte_count >= 1024 ** 2:
        return f"{byte_count / 1024 ** 2:.0f} MB"
    return f"{byte_count / 1024:.0f} KB"


def _find_usb():
    """First writable removable mount that isn't the root filesystem."""
    root_dev = None
    try:
        root_dev = os.stat("/").st_dev
    except OSError:
        pass
    for base in USB_MOUNT_ROOTS:
        b = Path(base)
        if not b.is_dir():
            continue
        for entry in sorted(b.iterdir()):
            if not entry.is_dir():
                continue
            try:
                if root_dev is not None and os.stat(entry).st_dev == root_dev:
                    continue          # not a separate mount, so not a stick
                if not os.access(entry, os.W_OK):
                    continue
            except OSError:
                continue
            return entry
    return None


def _version(repo_root):
    sha = _run(["git", "rev-parse", "--short", "HEAD"], repo_root)
    when = _run(["git", "log", "-1", "--format=%cd", "--date=short"], repo_root)
    if sha:
        return f"{sha}{f' ({when})' if when else ''}"
    return "unknown"
