"""
Single owner of the camera.

Two jobs:

1. Serialise access. picamera2 does not tolerate concurrent callers, and a
   live preview plus a full-resolution capture are exactly that. One lock
   arbitrates both; a capture pauses the preview and resumes it afterwards.

2. Feed the Debug view. Rather than encoding four JPEG streams on the Pi, the
   preview sends the browser a small pair of NIR and blue channel planes and
   lets JS derive BNDVI and paint all seven canvases. That means the k slider,
   the threshold sliders and the correction toggle respond with no server
   round-trip at all, and the four renders cannot drift out of sync because
   they all come from one array. The Pi's only job per frame is grab →
   downsample → send.

The preview reuses the 640x480 `lores` stream that bndvi.py has always
configured and never read.
"""

import logging
import threading
import time

import numpy as np

import bndvi

log = logging.getLogger("hylocropter.camera")

# Size of the planes sent to the browser. 160x120 is what the mockup's own
# canvases used, it is plenty for judging exposure and colour cast, and it keeps
# a frame at ~38 KB before compression.
PREVIEW_W, PREVIEW_H = bndvi.SYNTH_W, bndvi.SYNTH_H

# A real capture at full resolution can take several seconds on a Pi 4. Give a
# preview frame far less patience than that, so a stuck camera shows as "no
# frame" instead of hanging the request.
FRAME_WAIT_S = 2.0


def _downsample(frame):
    """Nearest-neighbour shrink to PREVIEW_W x PREVIEW_H.

    Deliberately index-based rather than an interpolating resize: averaging
    neighbouring pixels would blend the Bayer-derived channels together, and the
    whole point of this view is to show what each channel actually holds.
    """
    h, w = frame.shape[:2]
    ys = (np.arange(PREVIEW_H) * h // PREVIEW_H).clip(0, h - 1)
    xs = (np.arange(PREVIEW_W) * w // PREVIEW_W).clip(0, w - 1)
    return frame[ys][:, xs]


class CameraService:
    """Owns the camera, the preview loop, and the capture lock."""

    def __init__(self, settings, dev_mode=False):
        self.settings = settings
        self.dev_mode = dev_mode
        self._lock = threading.Lock()          # serialises hardware access
        self._frame_lock = threading.Lock()
        self._frame = None                     # (nir, blue) float32 planes
        self._frame_meta = {}
        self._frame_seq = 0
        self._thread = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._jitter = 0.0
        self._picam = None
        self._probe = None
        self._probe_at = 0.0
        self._wanted = {}
        self._synthetic_override = dev_mode
        self._last_error = None

    # ── availability ──────────────────────────────────────────────────────

    def probe(self, force=False):
        """Camera status, cached for a few seconds.

        Enumeration touches the hardware, and the header polls status on every
        page, so caching keeps that off the CSI bus.
        """
        now = time.time()
        if self._probe is not None and not force and now - self._probe_at < 5.0:
            return self._probe
        if self.dev_mode:
            info = {"available": False, "backend": "synthetic",
                    "detail": "dev mode — synthetic frames"}
        else:
            info = bndvi.probe_camera()
        info["synthetic"] = (self.dev_mode or self._synthetic_override
                             or not info.get("available", False))
        info["last_error"] = self._last_error
        self._probe, self._probe_at = info, now
        return info

    def using_synthetic(self):
        """True when frames are generated rather than captured."""
        if self.dev_mode or self._synthetic_override:
            return True
        return not self.probe().get("available", False)

    def use_synthetic(self, on=True):
        """The Debug view's 'Use synthetic frames' button."""
        self._synthetic_override = bool(on)
        self._probe = None
        self.restart()

    def restart(self):
        """The 'Restart the camera' / 'Retry detection' action.

        Drops the picamera2 object so the next frame reopens the device. This is
        the in-process equivalent of unplugging and replugging, and it is the
        fix for the common case where libcamera got into a bad state.
        """
        with self._lock:
            self._close_locked()
        self._last_error = None
        self._probe = None
        self.probe(force=True)
        log.info("camera restarted (synthetic=%s)", self.using_synthetic())
        return self.probe(force=True)

    # ── preview loop ──────────────────────────────────────────────────────

    def start_preview(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="preview",
                                        daemon=True)
        self._thread.start()

    def stop_preview(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            fps = max(1, min(24, int(self.settings.get("preview_fps", 12))))
            if self._paused.is_set():
                time.sleep(0.1)
                continue
            try:
                nir, blue, meta = self._grab()
                with self._frame_lock:
                    self._frame = (nir, blue)
                    self._frame_meta = meta
                    self._frame_seq += 1
            except Exception as exc:
                # A failing camera must not kill the loop -- fall back to
                # synthetic so the Debug view stays usable, and say why.
                if str(exc) != self._last_error:
                    log.warning("preview frame failed: %s", exc)
                self._last_error = str(exc)
                self._probe = None
                time.sleep(0.5)
                continue
            self._jitter += 0.13
            time.sleep(1.0 / fps)

    def _grab(self):
        """One frame as (nir, blue, meta), already downsampled."""
        if self.using_synthetic():
            scene = self.settings.get("preview_scene", "mixed")
            nir, blue = bndvi.synthetic_field(scene, jitter=self._jitter)
            return nir, blue, {"source": "synthetic", "scene": scene}

        with self._lock:
            cam = self._open_locked()
            request = cam.capture_request()
            try:
                frame = request.make_array("lores")
                metadata = request.get_metadata()
            finally:
                request.release()
        small = _downsample(frame)
        # Check the locks on every frame. This is the cheap continuous version of
        # the per-capture control_check, and it is what makes a silently ignored
        # AE/AWB lock visible in the Debug view rather than invisible forever.
        mismatches = bndvi.verify_controls(getattr(self, "_wanted", {}),
                                           metadata or {})
        return (small[:, :, 0].astype(np.float32),
                small[:, :, 2].astype(np.float32),
                {"source": "camera", "mismatches": mismatches})

    def _open_locked(self):
        """Open picamera2 with the current locked controls. Caller holds _lock."""
        if self._picam is not None:
            return self._picam
        s = self.settings
        cam = bndvi.open_camera(
            neutralise_isp=bool(s.get("neutralise_isp", True)))
        # Same control set as a real capture, filtered to what this libcamera
        # advertises. Using the identical dict is the point: the debug feed must
        # show what a capture would record, not a differently-processed preview.
        self._wanted = bndvi.locked_controls(
            s.get("gain"), s.get("exposure_us"), tuple(s.get("colour_gains")),
            available=cam.camera_controls)
        config = cam.create_preview_configuration(
            main={"size": (640, 480), "format": "RGB888"},
            lores={"size": (320, 240), "format": "RGB888"},
            controls=self._wanted,
        )
        cam.configure(config)
        cam.start()
        time.sleep(0.4)
        self._picam = cam
        log.info("preview camera opened")
        return cam

    def _close_locked(self):
        if self._picam is None:
            return
        try:
            self._picam.stop()
        except Exception:
            pass
        try:
            self._picam.close()
        except Exception:
            pass
        self._picam = None

    def apply_controls(self):
        """Re-apply exposure/gain after a settings change.

        Cheaper than a full restart, and keeps the preview live while the user
        drags the exposure slider.
        """
        with self._lock:
            if self._picam is None:
                return
            s = self.settings
            try:
                self._picam.set_controls(bndvi.locked_controls(
                    s.get("gain"), s.get("exposure_us"),
                    tuple(s.get("colour_gains"))))
            except Exception as exc:
                log.warning("could not apply controls live: %s", exc)

    # ── reading frames ────────────────────────────────────────────────────

    def latest(self, wait=True):
        """Most recent (nir, blue, meta, seq). Starts the loop on demand."""
        self.start_preview()
        deadline = time.time() + (FRAME_WAIT_S if wait else 0)
        while True:
            with self._frame_lock:
                if self._frame is not None:
                    nir, blue = self._frame
                    return nir, blue, dict(self._frame_meta), self._frame_seq
            if time.time() >= deadline:
                return None, None, {"source": "none",
                                    "error": self._last_error}, self._frame_seq
            time.sleep(0.03)

    def latest_rgb(self):
        """The current preview frame as an RGB array, for 'save as capture'.

        Reconstructs green the way the gel behaves (mostly blocked) so the saved
        frame flows through the exact same analysis path as a real capture.
        """
        nir, blue, meta, _ = self.latest()
        if nir is None:
            return None, meta
        green = 0.28 * nir + 0.25 * blue
        rgb = np.stack([nir, green, blue], -1).clip(0, 255).astype(np.uint8)
        return rgb, meta

    def region_means(self, x0, y0, x1, y1):
        """Mean NIR and blue inside a normalised box (0..1) of the live frame.

        This is what makes white-reference calibration a drag-a-box gesture
        instead of a trip to a text editor.
        """
        nir, blue, meta, _ = self.latest()
        if nir is None:
            return None
        h, w = nir.shape
        cx0, cx1 = sorted((int(x0 * w), int(x1 * w)))
        cy0, cy1 = sorted((int(y0 * h), int(y1 * h)))
        cx1 = max(cx1, cx0 + 1)
        cy1 = max(cy1, cy0 + 1)
        cx0, cy0 = max(0, cx0), max(0, cy0)
        cx1, cy1 = min(w, cx1), min(h, cy1)
        return {
            "nir": float(nir[cy0:cy1, cx0:cx1].mean()),
            "blue": float(blue[cy0:cy1, cx0:cx1].mean()),
            "pixels": int((cy1 - cy0) * (cx1 - cx0)),
            "source": meta.get("source"),
        }

    # ── full captures ─────────────────────────────────────────────────────

    def capture_locked(self, fn):
        """Run `fn` with exclusive camera access, preview paused.

        Non-blocking: returns (False, None) immediately if the camera is busy,
        which the API surfaces as 409 — preserving the old behaviour.
        """
        if not self._lock.acquire(blocking=False):
            return False, None
        self._paused.set()
        try:
            self._close_locked()      # release the device for the still config
            return True, fn()
        finally:
            self._paused.clear()
            self._lock.release()
