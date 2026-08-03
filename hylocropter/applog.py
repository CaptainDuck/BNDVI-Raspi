"""
Logging for Hylocropter.

The old dashboard had no logging setup at all -- one `app.logger.exception`
call and a scatter of `print()`. That meant the only way to find out why
something failed was to SSH in and read stdout, which is exactly what this
project is trying to avoid.

So this module does two things:

1. Configures real logging to a rotating file plus the console.
2. Keeps the last N records in memory, so the dashboard can render them.
   Settings shows the friendly ones as "Recent activity"; the Debug page shows
   everything as a log viewer. Between them there should be no reason to open a
   terminal.
"""

import logging
import logging.handlers
import threading
import time
from collections import deque
from pathlib import Path

RING_SIZE = 500
_ring = deque(maxlen=RING_SIZE)
_ring_lock = threading.Lock()
_configured = False


class RingHandler(logging.Handler):
    """Keeps recent records in memory for the UI to read back."""

    def emit(self, record):
        try:
            entry = {
                "ts": record.created,
                "time": time.strftime("%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "logger": record.name,
                "text": record.getMessage(),
                # `activity=True` marks the events a farmer would want to see
                # ("Flight saved", "Camera check passed") as opposed to debug noise.
                "activity": bool(getattr(record, "activity", False)),
            }
            if record.exc_info:
                entry["text"] += f" — {record.exc_info[1]}"
            with _ring_lock:
                _ring.append(entry)
        except Exception:  # never let logging break the caller
            pass


def setup(data_dir, debug=False):
    """Configure logging once. Safe to call repeatedly."""
    global _configured
    if _configured:
        return logging.getLogger("hylocropter")

    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file: 5 x 1 MB is plenty for a field device and cannot fill the
    # SD card. Note *.log is gitignored, so this never ends up committed.
    fh = logging.handlers.RotatingFileHandler(
        data_dir / "hylocropter.log", maxBytes=1_000_000, backupCount=5,
        encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)

    root.addHandler(RingHandler())

    # Werkzeug logs every request at INFO; that drowns the ring buffer.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    _configured = True
    log = logging.getLogger("hylocropter")
    log.info("logging started, writing to %s", data_dir / "hylocropter.log")
    return log


def activity(logger, message, *args):
    """Log something the operator should see in 'Recent activity'."""
    logger.info(message, *args, extra={"activity": True})


def recent(limit=100, activity_only=False, min_level=None):
    """Read back the ring buffer, newest last (matching how a log reads)."""
    with _ring_lock:
        items = list(_ring)
    if activity_only:
        items = [i for i in items if i["activity"]]
    if min_level:
        order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40,
                 "CRITICAL": 50}
        floor = order.get(min_level.upper(), 0)
        items = [i for i in items if order.get(i["level"], 0) >= floor]
    return items[-limit:]
