"""Camera sources: Thorlabs TSI camera, simulated beam, and a loaded image file.

Every source returns frames as 2D uint16 arrays in raw ADC counts.
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np


class CameraError(RuntimeError):
    pass


class CameraBase:
    kind = "base"
    name = "camera"
    width = 0
    height = 0
    bit_depth = 10
    pixel_size_um = 3.45
    exposure_range_ms = (0.04, 1000.0)
    gain_range_db = (0.0, 0.0)

    def read(self, timeout: float = 0.5):
        """Return the next frame (2D uint16) or None if none arrived in time."""
        raise NotImplementedError

    def set_exposure_ms(self, ms: float) -> float:
        return ms

    def get_exposure_ms(self) -> float:
        return 0.0

    def set_gain_db(self, db: float) -> float:
        return 0.0

    def get_gain_db(self) -> float:
        return 0.0

    def close(self) -> None:
        pass

    def info(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "width": self.width,
            "height": self.height,
            "bits": self.bit_depth,
            "pitch_um": self.pixel_size_um,
            "exp_range": [float(self.exposure_range_ms[0]), float(self.exposure_range_ms[1])],
            "gain_range": [float(self.gain_range_db[0]), float(self.gain_range_db[1])],
        }


# --------------------------------------------------------------------------
# Thorlabs TSI (Zelux / Kiralux / Quantalux) through the official Python SDK
# --------------------------------------------------------------------------

def find_thorlabs_dll_dir(extra: str | None = None) -> str | None:
    """Locate the folder holding the 64-bit Thorlabs native DLLs (Windows)."""
    from .thorlabs_sdk import find_dll_dir

    return find_dll_dir(extra)


class ThorlabsCamera(CameraBase):
    kind = "thorlabs"

    def __init__(self, serial: str | None = None, dll_dir: str | None = None):
        from .thorlabs_sdk import SDKNotFound, load

        try:
            TLCameraSDK, OPERATION_MODE, found = load(dll_dir)
        except SDKNotFound as e:
            raise CameraError(str(e)) from e

        self._lock = threading.RLock()
        self._sdk = None
        self._cam = None
        try:
            self._sdk = TLCameraSDK()
        except Exception as e:
            where = f" (DLL folder used: {found})" if found else " (no DLL folder found)"
            raise CameraError(f"Could not load the Thorlabs camera DLLs{where}: {e}") from e
        try:
            serials = list(self._sdk.discover_available_cameras())
            if not serials:
                raise CameraError(
                    "No Thorlabs camera found. Check the USB 3.0 cable and close ThorCam "
                    "(only one program can use the camera at a time)."
                )
            if serial and serial not in serials:
                raise CameraError(f"Camera {serial} not found. Available: {', '.join(serials)}")
            cam = self._sdk.open_camera(serial or serials[0])
            self._cam = cam
            self.name = f"{cam.model} #{cam.serial_number}"
            self.width = int(cam.image_width_pixels)
            self.height = int(cam.image_height_pixels)
            self.bit_depth = int(cam.bit_depth)
            self.pixel_size_um = float(cam.sensor_pixel_width_um)
            rng = cam.exposure_time_range_us
            self.exposure_range_ms = (rng.min / 1000.0, min(rng.max / 1000.0, 10000.0))
            self._has_gain = False
            try:
                g = cam.gain_range
                if g.max > g.min:
                    self._has_gain = True
                    self.gain_range_db = (
                        float(cam.convert_gain_to_decibels(g.min)),
                        float(cam.convert_gain_to_decibels(g.max)),
                    )
            except Exception:
                self._has_gain = False
            cam.operation_mode = OPERATION_MODE.SOFTWARE_TRIGGERED
            cam.frames_per_trigger_zero_for_unlimited = 0
            cam.image_poll_timeout_ms = 50
            cam.arm(2)
            cam.issue_software_trigger()
        except Exception as e:
            self.close()
            if isinstance(e, CameraError):
                raise
            raise CameraError(f"Could not open the Thorlabs camera: {e}") from e

    def read(self, timeout: float = 0.5):
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                if self._cam is None:
                    return None
                frame = self._cam.get_pending_frame_or_null()
                if frame is not None:
                    img = np.array(frame.image_buffer, dtype=np.uint16, copy=True)
                    if img.ndim != 2:
                        img = img.reshape(self.height, self.width)
                    return img
            if time.monotonic() >= deadline:
                return None

    def set_exposure_ms(self, ms: float) -> float:
        lo, hi = self.exposure_range_ms
        ms = float(min(max(ms, lo), hi))
        with self._lock:
            self._cam.exposure_time_us = int(round(ms * 1000.0))
        return self.get_exposure_ms()

    def get_exposure_ms(self) -> float:
        with self._lock:
            return self._cam.exposure_time_us / 1000.0

    def set_gain_db(self, db: float) -> float:
        if not self._has_gain:
            return 0.0
        lo, hi = self.gain_range_db
        db = float(min(max(db, lo), hi))
        with self._lock:
            self._cam.gain = int(self._cam.convert_decibels_to_gain(db))
        return self.get_gain_db()

    def get_gain_db(self) -> float:
        if not self._has_gain:
            return 0.0
        with self._lock:
            return float(self._cam.convert_gain_to_decibels(self._cam.gain))

    def close(self) -> None:
        with self._lock:
            cam, self._cam = self._cam, None
            if cam is not None:
                try:
                    cam.disarm()
                except Exception:
                    pass
                try:
                    cam.dispose()
                except Exception:
                    pass
            sdk, self._sdk = self._sdk, None
            if sdk is not None:
                try:
                    sdk.dispose()
                except Exception:
                    pass


# --------------------------------------------------------------------------
# Simulated camera: an elliptical Gaussian beam with noise, for trying the
# software without hardware. Geometry matches the Zelux CS165MU1.
# --------------------------------------------------------------------------


class SimulatedCamera(CameraBase):
    kind = "sim"
    name = "Simulated beam (no camera)"
    width = 1440
    height = 1080
    bit_depth = 10
    pixel_size_um = 3.45
    exposure_range_ms = (0.04, 2000.0)
    gain_range_db = (0.0, 24.0)

    def __init__(self, fps: float = 20.0):
        self._fps = fps
        self._exposure_ms = 5.0
        self._gain_db = 0.0
        self._rng = np.random.default_rng(1)
        self._next = time.monotonic()
        self._t0 = time.monotonic()
        self._x = np.arange(self.width, dtype=np.float32)[None, :]
        self._y = np.arange(self.height, dtype=np.float32)[:, None]
        # Beam description: 1/e^2 radii in micrometres, angle in degrees
        # (counter-clockwise on screen), centre in pixels.
        self.params = {
            "wx_um": 420.0,
            "wy_um": 300.0,
            "angle_deg": 25.0,
            "x0_px": 700.0,
            "y0_px": 560.0,
            "counts_per_ms": 150.0,  # peak counts per ms of exposure at 0 dB
            "offset": 6.0,
            "noise": 1.6,
            "jitter_um": 1.5,
            "drift_um": 15.0,
            "fringes": 0.0,
        }

    def set_params(self, patch: dict) -> dict:
        for k, v in patch.items():
            if k in self.params:
                self.params[k] = float(v)
        return dict(self.params)

    def read(self, timeout: float = 0.5):
        now = time.monotonic()
        wait = self._next - now
        if wait > 0:
            time.sleep(min(wait, timeout))
            if wait > timeout:
                return None
        self._next = max(self._next + 1.0 / self._fps, time.monotonic())
        p = self.params
        t = time.monotonic() - self._t0
        px = self.pixel_size_um
        jit = self._rng.normal(0.0, p["jitter_um"] / px, size=2)
        drift = p["drift_um"] / px
        cx = p["x0_px"] + jit[0] + drift * np.sin(2 * np.pi * t / 60.0)
        cy = p["y0_px"] + jit[1] + drift * 0.6 * np.sin(2 * np.pi * t / 37.0)
        th = -np.deg2rad(p["angle_deg"])  # screen y axis points down
        ct, st = np.float32(np.cos(th)), np.float32(np.sin(th))
        dx = self._x - np.float32(cx)
        dy = self._y - np.float32(cy)
        u = dx * ct + dy * st
        v = -dx * st + dy * ct
        wu = np.float32(max(p["wx_um"], 1.0) / px)
        wv = np.float32(max(p["wy_um"], 1.0) / px)
        amp = p["counts_per_ms"] * self._exposure_ms * 10 ** (self._gain_db / 20.0)
        img = np.exp(-2.0 * ((u / wu) ** 2 + (v / wv) ** 2))
        if p["fringes"] > 0:
            img *= 1.0 + np.float32(p["fringes"]) * np.sin(u * np.float32(0.35) + np.float32(t))
        img *= np.float32(amp)
        gain_lin = 10 ** (self._gain_db / 20.0)
        img += np.float32(p["offset"])
        noise = self._rng.standard_normal(img.shape, dtype=np.float32)
        shot = np.sqrt(np.maximum(img, 0.0) * np.float32(0.05 * gain_lin))
        img += noise * (np.float32(p["noise"] * gain_lin) + shot)
        np.clip(img, 0, 2**self.bit_depth - 1, out=img)
        return img.astype(np.uint16)

    def set_exposure_ms(self, ms: float) -> float:
        lo, hi = self.exposure_range_ms
        self._exposure_ms = float(min(max(ms, lo), hi))
        return self._exposure_ms

    def get_exposure_ms(self) -> float:
        return self._exposure_ms

    def set_gain_db(self, db: float) -> float:
        lo, hi = self.gain_range_db
        self._gain_db = float(min(max(db, lo), hi))
        return self._gain_db

    def get_gain_db(self) -> float:
        return self._gain_db


# --------------------------------------------------------------------------
# A saved image served as a (static) camera, for offline analysis
# --------------------------------------------------------------------------


def load_image_bytes(data: bytes, filename: str) -> tuple[np.ndarray, int]:
    """Decode an uploaded image into (uint16 2D array, bit depth)."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".npy":
        arr = np.load(io.BytesIO(data), allow_pickle=False)
    else:
        try:
            from PIL import Image
        except ImportError as e:  # pragma: no cover
            raise CameraError("Pillow is needed to open image files (pip install pillow).") from e
        try:
            with Image.open(io.BytesIO(data)) as im:
                arr = np.array(im)
        except Exception as e:
            raise CameraError(f"Could not read '{filename}' as an image: {e}") from e
    arr = np.asarray(arr)
    if arr.ndim == 3:
        arr = arr[..., :3].astype(np.float64).mean(axis=2)
    if arr.ndim != 2 or arr.shape[0] < 8 or arr.shape[1] < 8:
        raise CameraError(f"'{filename}' is not a 2D image (shape {arr.shape}).")
    was_uint8 = arr.dtype == np.uint8
    arr = np.nan_to_num(np.asarray(arr, dtype=np.float64))
    arr -= min(arr.min(), 0.0)
    peak = float(arr.max())
    if peak > 65535:
        arr *= 65535.0 / peak
        peak = 65535.0
    if was_uint8:
        bits = 8
    else:
        bits = 16
        for b in (10, 12, 14):
            if peak <= 2**b - 1:
                bits = b
                break
    return np.round(arr).astype(np.uint16), bits


class FileCamera(CameraBase):
    kind = "file"

    def __init__(self, image: np.ndarray, bits: int, filename: str, pixel_size_um: float = 3.45):
        self._image = image
        self.name = filename
        self.height, self.width = image.shape
        self.bit_depth = bits
        self.pixel_size_um = pixel_size_um
        self.exposure_range_ms = (1.0, 1.0)

    def read(self, timeout: float = 0.5):
        time.sleep(min(0.2, timeout))
        return self._image

    def get_exposure_ms(self) -> float:
        return 1.0
