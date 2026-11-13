"""Acquisition and processing engine.

One thread pulls frames from the camera (and keeps the rolling average),
a second thread analyses the latest frame and builds a binary packet
(JSON header + 8-bit display image) that the web server streams to browsers.
"""

from __future__ import annotations

import csv
import json
import math
import struct
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np

from . import analysis
from .cameras import CameraBase, CameraError, FileCamera, SimulatedCamera, load_image_bytes

MAX_AVERAGING = 32

DEFAULTS = {
    # camera
    "exposure_ms": 5.0,
    "gain_db": 0.0,
    "auto_exposure": False,
    "ae_target": 0.75,
    "averaging": 1,
    "paused": False,
    # region of interest (sensor pixels) and tracking
    "roi": None,
    "auto_roi": False,
    # background
    "use_dark": False,
    "auto_baseline": True,
    # profiles and fits
    "profile_mode": "slice",
    "slice_halfwidth": 2,
    "profile_center": "centroid",
    "cursor": None,
    "fit2d": False,
    # display stream
    "display_scale": "linear",
    "autoscale": False,
    "stream_scale": 0,
    "max_fps": 20.0,
    # calibration
    "pixel_size_um": 3.45,
    "magnification": 1.0,
    "wavelength_nm": 780.0,
    # logging
    "log_interval_s": 1.0,
}

_FLOATS = {
    "exposure_ms": (1e-3, 1e5),
    "gain_db": (0.0, 100.0),
    "ae_target": (0.2, 0.95),
    "max_fps": (1.0, 60.0),
    "pixel_size_um": (0.01, 1000.0),
    "magnification": (1e-3, 1e3),
    "wavelength_nm": (100.0, 20000.0),
    "log_interval_s": (0.0, 3600.0),
}
_INTS = {"averaging": (1, MAX_AVERAGING), "slice_halfwidth": (0, 50)}
_BOOLS = {"auto_exposure", "paused", "auto_roi", "use_dark", "auto_baseline", "fit2d", "autoscale"}
_ENUMS = {
    "profile_mode": ("slice", "sum"),
    "profile_center": ("centroid", "peak", "cursor"),
    "display_scale": ("linear", "sqrt", "log"),
}

LOG_COLUMNS = [
    "time", "t_s", "centroid_x_um", "centroid_y_um",
    "d4sigma_x_um", "d4sigma_y_um", "d4sigma_major_um", "d4sigma_minor_um", "angle_deg",
    "gauss_wx_um", "gauss_wy_um", "gauss2d_wmajor_um", "gauss2d_wminor_um",
    "peak_percent", "total_counts", "exposure_ms", "gain_db",
]


def _num(v, nd: int = 3):
    if v is None:
        return None
    v = float(v)
    if not math.isfinite(v):
        return None
    return round(v, nd)


class Engine:
    def __init__(self, data_dir: str | Path = "data", live_factory=None):
        self.data_dir = Path(data_dir)
        self.camera: CameraBase | None = None
        self.cam_error: str | None = None
        self._live_factory = live_factory
        self.launch_mode: str | None = None  # "thorlabs" or "sim" from the command line
        self._connect_lock = threading.Lock()  # one camera (re)connect at a time
        self._save_lock = threading.Lock()  # one quick save at a time (summary CSV appends)
        self.settings = dict(DEFAULTS)
        self.rev = 0

        self._lock = threading.RLock()
        self._buf: deque = deque()
        self._sum = None
        self._raw = None
        self._frame_id = 0
        self._fps = 0.0
        self._last_frame_t = None

        self._dark = None
        self._dark_meta = None
        self._dark_req = None

        self._auto_roi = None
        self._ae_wait_until = 0
        self._frozen = None
        self._last = None  # last processed frame, for snapshots

        self._log_file = None
        self._log_writer = None
        self._log_name = None
        self._log_rows = 0
        self._log_t0 = 0.0
        self._log_last = 0.0

        self.packet: bytes | None = None
        self.packet_seq = 0

        self._new_frame = threading.Event()
        self._stop = False
        self._threads = [
            threading.Thread(target=self._acq_loop, name="acquire", daemon=True),
            threading.Thread(target=self._proc_loop, name="process", daemon=True),
        ]

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        for t in self._threads:
            t.start()

    def shutdown(self) -> None:
        self._stop = True
        self._new_frame.set()
        for t in self._threads:
            t.join(timeout=2.0)
        self.stop_log()
        with self._lock:
            cam, self.camera = self.camera, None
        if cam is not None:
            cam.close()

    def connect_live(self) -> bool:
        """(Re)connect the live camera. Returns True on success."""
        with self._connect_lock:
            return self._connect_live()

    def _connect_live(self) -> bool:
        if self._live_factory is None:
            self.cam_error = "No live camera configured."
            return False
        self._set_camera(None)
        try:
            cam = self._live_factory()
        except CameraError as e:
            self.cam_error = str(e)
            return False
        except Exception as e:  # unexpected driver failure
            self.cam_error = f"{type(e).__name__}: {e}"
            return False
        self._set_camera(cam)
        return True

    def use_simulator(self) -> None:
        self._live_factory = SimulatedCamera
        self.connect_live()

    def load_file(self, data: bytes, filename: str) -> None:
        image, bits = load_image_bytes(data, filename)
        cam = FileCamera(image, bits, filename, pixel_size_um=self.settings["pixel_size_um"])
        self._set_camera(cam)

    def _set_camera(self, cam: CameraBase | None) -> None:
        with self._lock:
            old, self.camera = self.camera, None
        if old is not None:
            old.close()
        with self._lock:
            old_shape = (old.height, old.width) if old is not None else None
            self._buf.clear()
            self._sum = None
            self._raw = None
            self._frozen = None
            self._last = None
            self._auto_roi = None
            self._dark_req = None
            self._fps = 0.0
            self._last_frame_t = None
            s = dict(self.settings)
            if cam is not None:
                self.cam_error = None
                if old_shape != (cam.height, cam.width):
                    self._dark = None
                    self._dark_meta = None
                    s["roi"] = None
                    s["cursor"] = None
                    s["use_dark"] = False
                if cam.kind != "file":
                    s["exposure_ms"] = cam.set_exposure_ms(s["exposure_ms"])
                    s["gain_db"] = cam.set_gain_db(s["gain_db"])
                    s["pixel_size_um"] = cam.pixel_size_um
                s["paused"] = False
            self.settings = s
            self.rev += 1
            self.camera = cam

    # ------------------------------------------------------------- settings

    def update_settings(self, patch: dict) -> dict:
        with self._lock:
            s = dict(self.settings)
            cam = self.camera
            reset_avg = False
            for k, v in patch.items():
                if k not in DEFAULTS:
                    continue
                try:
                    if k in _FLOATS:
                        lo, hi = _FLOATS[k]
                        v = float(v)
                        if not math.isfinite(v):
                            continue
                        s[k] = min(max(v, lo), hi)
                    elif k in _INTS:
                        lo, hi = _INTS[k]
                        s[k] = min(max(int(v), lo), hi)
                    elif k in _BOOLS:
                        s[k] = bool(v)
                    elif k in _ENUMS:
                        if v in _ENUMS[k]:
                            s[k] = v
                    elif k == "stream_scale":
                        s[k] = int(v) if int(v) in (0, 1, 2, 4) else 0
                    elif k == "roi":
                        s[k] = self._clean_roi(v, cam)
                    elif k == "cursor":
                        s[k] = None if v is None else {"x": float(v["x"]), "y": float(v["y"])}
                except (TypeError, ValueError, KeyError):
                    continue
            if cam is not None and cam.kind != "file":
                if "exposure_ms" in patch and s["exposure_ms"] != self.settings["exposure_ms"]:
                    s["exposure_ms"] = cam.set_exposure_ms(s["exposure_ms"])
                    reset_avg = True
                if "gain_db" in patch and s["gain_db"] != self.settings["gain_db"]:
                    s["gain_db"] = cam.set_gain_db(s["gain_db"])
                    reset_avg = True
            if "auto_roi" in patch and not s["auto_roi"]:
                self._auto_roi = None
            if reset_avg:
                self._reset_average()
            self.settings = s
            self.rev += 1
            return dict(s)

    @staticmethod
    def _clean_roi(v, cam):
        if v is None or cam is None:
            return None
        W, H = cam.width, cam.height
        x, y = int(round(float(v["x"]))), int(round(float(v["y"])))
        w, h = int(round(float(v["w"]))), int(round(float(v["h"])))
        x = min(max(x, 0), W - 8)
        y = min(max(y, 0), H - 8)
        w = min(max(w, 8), W - x)
        h = min(max(h, 8), H - y)
        if w >= W and h >= H:
            return None
        return {"x": x, "y": y, "w": w, "h": h}

    def _reset_average(self) -> None:
        self._buf.clear()
        self._sum = None
        self._ae_wait_until = self._frame_id + 3

    def state(self) -> dict:
        cam = self.camera
        return {
            "settings": dict(self.settings),
            "rev": self.rev,
            "camera": cam.info() if cam is not None else None,
            "error": self.cam_error,
            "sim": dict(cam.params) if isinstance(cam, SimulatedCamera) else None,
            "max_averaging": MAX_AVERAGING,
            "launch_mode": self.launch_mode,
        }

    # ----------------------------------------------------------- background

    def capture_dark(self, frames: int = 16) -> None:
        with self._lock:
            if self.camera is None:
                raise CameraError("No camera connected.")
            self._dark_req = {"n": max(1, min(int(frames), 256)), "count": 0, "sum": None}

    def clear_dark(self) -> None:
        with self._lock:
            self._dark = None
            self._dark_meta = None
            self._dark_req = None
        self.update_settings({"use_dark": False})

    # ---------------------------------------------------------- acquisition

    def _acq_loop(self) -> None:
        while not self._stop:
            cam = self.camera
            if cam is None:
                time.sleep(0.1)
                continue
            try:
                frame = cam.read(timeout=0.5)
            except Exception as e:
                self.cam_error = f"Camera read failed: {e}"
                time.sleep(0.5)
                continue
            if frame is None:
                continue
            now = time.monotonic()
            dark_done = False
            with self._lock:
                if cam is not self.camera:
                    continue
                n = self.settings["averaging"]
                if self._sum is None or self._sum.shape != frame.shape:
                    self._buf.clear()
                    self._sum = np.zeros(frame.shape, dtype=np.uint32)
                self._buf.append(frame)
                self._sum += frame
                while len(self._buf) > n:
                    self._sum -= self._buf.popleft()
                self._raw = frame
                self._frame_id += 1
                if self._last_frame_t is not None:
                    dt = now - self._last_frame_t
                    if dt > 0:
                        inst = 1.0 / dt
                        self._fps = inst if self._fps == 0 else 0.9 * self._fps + 0.1 * inst
                self._last_frame_t = now
                req = self._dark_req
                if req is not None:
                    if req["sum"] is None or req["sum"].shape != frame.shape:
                        req["sum"] = np.zeros(frame.shape, dtype=np.float64)
                        req["count"] = 0
                    req["sum"] += frame
                    req["count"] += 1
                    if req["count"] >= req["n"]:
                        self._dark = (req["sum"] / req["count"]).astype(np.float32)
                        self._dark_meta = {
                            "exposure_ms": self.settings["exposure_ms"],
                            "gain_db": self.settings["gain_db"],
                            "frames": req["count"],
                            "mean": float(self._dark.mean()),
                        }
                        self._dark_req = None
                        dark_done = True
            if dark_done:
                self.update_settings({"use_dark": True})
            self._new_frame.set()

    # ----------------------------------------------------------- processing

    def _proc_loop(self) -> None:
        last_rev = -1
        last_pub = 0.0
        last_status = 0.0
        while not self._stop:
            got = self._new_frame.wait(timeout=0.1)
            self._new_frame.clear()
            if self._stop:
                break
            s = self.settings
            rev = self.rev
            cam = self.camera
            now = time.monotonic()
            if cam is None:
                if now - last_status > 0.5 or rev != last_rev:
                    last_status = now
                    last_rev = rev
                    self._publish(self._header_base(s, None), b"")
                continue
            paused = s["paused"]
            if paused and self._frozen is not None:
                if rev == last_rev:
                    continue
                avg, raw, n_avg, fid, f_exp, f_gain = self._frozen
            else:
                if not got and rev == last_rev:
                    continue
                if got and rev == last_rev and now - last_pub < 1.0 / s["max_fps"]:
                    continue
                with self._lock:
                    if self._sum is None or not self._buf or cam is not self.camera:
                        continue
                    n_avg = len(self._buf)
                    avg = self._sum.astype(np.float32)
                    if n_avg > 1:
                        avg /= np.float32(n_avg)
                    raw = self._raw
                    fid = self._frame_id
                # remember the exposure/gain the frame was taken with, for a frozen frame re-analysed later
                self._frozen = (avg, raw, n_avg, fid, s["exposure_ms"], s["gain_db"])
            last_rev = rev
            last_pub = now
            try:
                header, image = self._process(cam, avg, raw, n_avg, fid, s, live=not paused)
                if paused and header.get("cam") and cam.kind != "file":
                    header["cam"]["exposure_ms"] = _num(f_exp, 4)
                    header["cam"]["gain_db"] = _num(f_gain, 2)
            except Exception as e:  # keep the stream alive on analysis errors
                header = self._header_base(s, cam)
                header["status"]["msg"] = f"Analysis error: {type(e).__name__}: {e}"
                image = b""
            self._publish(header, image)

    def _publish(self, header: dict, image: bytes) -> None:
        self.packet_seq += 1
        header["seq"] = self.packet_seq
        head = json.dumps(header, separators=(",", ":")).encode("utf-8")
        self.packet = struct.pack("<I", len(head)) + head + image

    def _header_base(self, s: dict, cam: CameraBase | None) -> dict:
        req = self._dark_req
        return {
            "t": time.time(),
            "rev": self.rev,
            "cam": None if cam is None else {
                **cam.info(),
                "exposure_ms": _num(s["exposure_ms"], 4),
                "gain_db": _num(s["gain_db"], 2),
                "fps": _num(self._fps, 1),
            },
            "error": self.cam_error,
            "img": {"w": 0, "h": 0, "scale": 1},
            "status": {
                "valid": False,
                "msg": "",
                "dark": self._dark_meta,
                "dark_progress": None if req is None else [req["count"], req["n"]],
                "log": None if self._log_name is None else {"name": self._log_name, "rows": self._log_rows},
            },
            "res": None,
            "prof": None,
            "roi": None,
        }

    def _effective_roi(self, s: dict, W: int, H: int) -> tuple[int, int, int, int]:
        r = self._auto_roi if s["auto_roi"] else s["roi"]
        if not r:
            return 0, 0, W, H
        x = min(max(int(r["x"]), 0), W - 8)
        y = min(max(int(r["y"]), 0), H - 8)
        w = min(max(int(r["w"]), 8), W - x)
        h = min(max(int(r["h"]), 8), H - y)
        return x, y, w, h

    def _process(self, cam, avg, raw, n_avg, fid, s, live=True):
        H, W = avg.shape
        sat = 2**cam.bit_depth - 1
        header = self._header_base(s, cam)
        st = header["status"]

        img = avg
        dark_used = False
        if s["use_dark"] and self._dark is not None and self._dark.shape == img.shape:
            img = img - self._dark
            dark_used = True

        x, y, w, h = self._effective_roi(s, W, H)
        sub = img[y:y + h, x:x + w]
        base, noise = analysis.corner_baseline(sub)
        offset = base if s["auto_baseline"] else 0.0
        if offset:
            sub = sub - np.float32(offset)

        rsub = raw[y:y + h, x:x + w]
        peak_raw = int(rsub.max())
        sat_px = int(np.count_nonzero(rsub >= sat)) if peak_raw >= sat else 0

        small = analysis.block_mean(sub, 2) if min(w, h) >= 16 else sub
        peak_sig = float(small.max())
        valid = peak_sig > max(5.0 * noise, 4.0)

        d4 = analysis.d4sigma(sub) if valid else None
        res = None
        prof = None
        full_prof = None
        if d4 is None:
            valid = False
            st["msg"] = "No beam found in the region of interest"
        else:
            cx, cy = d4["cx"], d4["cy"]
            iy, ix = np.unravel_index(int(np.argmax(small)), small.shape)
            f = 2 if small is not sub else 1
            pk_x, pk_y = ix * f + (f - 1) / 2.0, iy * f + (f - 1) / 2.0

            pc = s["profile_center"]
            if pc == "peak":
                px_, py_ = pk_x, pk_y
            elif pc == "cursor" and s["cursor"]:
                px_ = min(max(s["cursor"]["x"] - x, 0), w - 1)
                py_ = min(max(s["cursor"]["y"] - y, 0), h - 1)
            else:
                px_, py_ = cx, cy
            vx, vy = analysis.line_profiles(sub, px_, py_, s["profile_mode"], s["slice_halfwidth"])
            fx = analysis.fit_gauss1d(np.arange(w, dtype=np.float64), vx, x0=cx, w=d4["dx"] / 2.0)
            fy = analysis.fit_gauss1d(np.arange(h, dtype=np.float64), vy, x0=cy, w=d4["dy"] / 2.0)
            prof = {
                "x": self._pack_profile(vx, x, fx),
                "y": self._pack_profile(vy, y, fy),
                "cx": _num(px_ + x, 2),
                "cy": _num(py_ + y, 2),
                "mode": s["profile_mode"],
            }
            full_prof = {  # full resolution, for quick save
                "x": {"start": x, "values": vx, "fit": fx},
                "y": {"start": y, "values": vy, "fit": fy},
                "mode": s["profile_mode"],
                "halfwidth": s["slice_halfwidth"] if s["profile_mode"] == "slice" else None,
                "through": {"x": _num(px_ + x, 2), "y": _num(py_ + y, 2)},
            }

            g2 = None
            if s["fit2d"]:
                wx0, wy0, ww, wh = d4["window"]
                g = analysis.fit_gauss2d(
                    sub[wy0:wy0 + wh, wx0:wx0 + ww],
                    {"cx": cx - wx0, "cy": cy - wy0, "dmaj": d4["dmaj"], "dmin": d4["dmin"], "phi": d4["phi"]},
                )
                if g is not None:
                    g2 = {
                        "cx": _num(g["cx"] + wx0 + x), "cy": _num(g["cy"] + wy0 + y),
                        "wmaj": _num(g["wmaj"]), "wmin": _num(g["wmin"]),
                        "phi": _num(g["phi"], 5), "r2": _num(g["r2"], 4),
                    }

            res = {
                "cx": _num(cx + x), "cy": _num(cy + y),
                "d4x": _num(d4["dx"]), "d4y": _num(d4["dy"]),
                "d4maj": _num(d4["dmaj"]), "d4min": _num(d4["dmin"]),
                "phi": _num(d4["phi"], 5),
                # Distance from the centroid to the nearest ROI edge, in units of
                # the D4-sigma radius. Below ~1.5 the beam is cut off enough to
                # make D4-sigma read low by more than about 1 %.
                "edge_ratio": _num(min(
                    cx / (d4["dx"] / 2.0), (w - 1 - cx) / (d4["dx"] / 2.0),
                    cy / (d4["dy"] / 2.0), (h - 1 - cy) / (d4["dy"] / 2.0),
                ), 2),
                "peak_x": _num(pk_x + x, 1), "peak_y": _num(pk_y + y, 1),
                "peak_sig": _num(peak_sig, 2),
                "total": _num(d4["total"], 1),
                "fit_wx": _num(fx["w"]) if fx else None,
                "fit_wy": _num(fy["w"]) if fy else None,
                "fit_x0": _num(fx["x0"] + x) if fx else None,
                "fit_y0": _num(fy["x0"] + y) if fy else None,
                "fit_r2x": _num(fx["r2"], 4) if fx else None,
                "fit_r2y": _num(fy["r2"], 4) if fy else None,
                "g2": g2,
            }

            if s["auto_roi"]:
                self._update_auto_roi(cx + x, cy + y, d4["dx"], d4["dy"], W, H)
        if not valid and s["auto_roi"]:
            self._auto_roi = None

        # ---- 8-bit display image of the whole frame
        sc = s["stream_scale"] or max(1, int(math.ceil(W / 800)))
        sc = min(sc, max(1, min(W, H) // 8))
        d = analysis.block_mean(img, sc).astype(np.float32, copy=True)
        if offset:
            d -= np.float32(offset)
        hi = max(peak_sig, 1.0) if s["autoscale"] else max(sat - offset - (st["dark"]["mean"] if dark_used and st["dark"] else 0.0), 1.0)
        d *= np.float32(1.0 / hi)
        np.clip(d, 0.0, 1.0, out=d)
        if s["display_scale"] == "sqrt":
            np.sqrt(d, out=d)
        elif s["display_scale"] == "log":
            d *= np.float32(999.0)
            d += np.float32(1.0)
            np.log10(d, out=d)
            d *= np.float32(1.0 / 3.0)
        d *= np.float32(254.0)
        d8 = d.astype(np.uint8)
        if int(raw.max()) >= sat:
            hh, ww = d8.shape
            if sc > 1:
                m = raw[:hh * sc, :ww * sc].reshape(hh, sc, ww, sc).max(axis=(1, 3)) >= sat
            else:
                m = raw >= sat
            d8[m] = 255
        header["img"] = {"w": int(d8.shape[1]), "h": int(d8.shape[0]), "scale": sc}

        st.update({
            "valid": bool(valid),
            "peak_pct": _num(100.0 * peak_raw / sat, 1),
            "sat_px": sat_px,
            "avg_n": n_avg,
            "baseline": _num(base, 2),
            "noise": _num(noise, 2),
            "dark_used": dark_used,
            "frame": fid,
            "paused": bool(s["paused"]),
        })
        header["res"] = res
        header["prof"] = prof
        header["roi"] = {"x": x, "y": y, "w": w, "h": h, "full": bool(w == W and h == H), "auto": bool(s["auto_roi"])}
        header["um_per_px"] = s["pixel_size_um"] / s["magnification"]

        self._last = {"avg": avg, "header": header, "settings": s, "profiles": full_prof}
        if live and cam.kind != "file":
            if s["auto_exposure"]:
                self._auto_expose(cam, s, peak_raw, sat_px, sat, fid)
            if valid:
                self._log_row(header, s)
        return header, d8.tobytes()

    @staticmethod
    def _pack_profile(vals: np.ndarray, start: int, fit: dict | None) -> dict:
        n = vals.size
        step = max(1, int(math.ceil(n / 800)))
        if step > 1:
            n2 = n // step
            vals = vals[:n2 * step].reshape(n2, step).mean(axis=1)
        out = {
            "x0": start + (step - 1) / 2.0,
            "dx": step,
            "v": np.round(vals, 2).tolist(),
            "fit": None,
        }
        if fit:
            out["fit"] = {"a": _num(fit["a"]), "x0": _num(fit["x0"] + start), "w": _num(fit["w"]), "c": _num(fit["c"])}
        return out

    def _update_auto_roi(self, cx, cy, dx, dy, W, H) -> None:
        w = max(48.0, 3.0 * dx)
        h = max(48.0, 3.0 * dy)
        new = {"x": cx - w / 2.0, "y": cy - h / 2.0, "w": w, "h": h}
        old = self._auto_roi
        if old is not None:
            tol_x, tol_y = 0.04 * old["w"], 0.04 * old["h"]
            if (
                abs(new["x"] - old["x"]) < tol_x and abs(new["y"] - old["y"]) < tol_y
                and abs(new["w"] - old["w"]) < 2 * tol_x and abs(new["h"] - old["h"]) < 2 * tol_y
            ):
                return
        x0 = int(max(0, math.floor(new["x"])))
        y0 = int(max(0, math.floor(new["y"])))
        x1 = int(min(W, math.ceil(new["x"] + w)))
        y1 = int(min(H, math.ceil(new["y"] + h)))
        self._auto_roi = {"x": x0, "y": y0, "w": max(8, x1 - x0), "h": max(8, y1 - y0)}

    def _auto_expose(self, cam, s, peak_raw, sat_px, sat, fid) -> None:
        if fid < self._ae_wait_until:
            return
        exp = s["exposure_ms"]
        target = s["ae_target"]
        frac = peak_raw / sat
        if sat_px > 0 or frac >= 0.985:
            new = exp * 0.5
        elif frac < target * 0.88 or frac > min(target * 1.12, 0.97):
            new = exp * target / max(frac, 0.02)
        else:
            return
        lo, hi = cam.exposure_range_ms
        new = min(max(new, lo), min(hi, 2000.0))
        if abs(new - exp) / exp < 0.02:
            return
        self.update_settings({"exposure_ms": new})

    # -------------------------------------------------------------- logging

    def start_log(self) -> str:
        self.stop_log()
        folder = self.data_dir / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        name = datetime.now().strftime("beamlog_%Y%m%d_%H%M%S.csv")
        f = open(folder / name, "w", newline="", encoding="utf-8")
        writer = csv.writer(f)
        writer.writerow(LOG_COLUMNS)
        with self._lock:
            self._log_file, self._log_writer, self._log_name = f, writer, name
            self._log_rows = 0
            self._log_t0 = time.time()
            self._log_last = 0.0
        return name

    def stop_log(self) -> str | None:
        with self._lock:
            f, name = self._log_file, self._log_name
            self._log_file = self._log_writer = self._log_name = None
        if f is not None:
            f.close()
        return name

    def _log_row(self, header: dict, s: dict) -> None:
        with self._lock:
            writer = self._log_writer
            if writer is None:
                return
            now = time.time()
            if now - self._log_last < s["log_interval_s"]:
                return
            self._log_last = now
            r = header["res"]
            k = header["um_per_px"]

            def um(v):
                return "" if v is None else round(v * k, 3)

            g2 = r["g2"] or {}
            writer.writerow([
                datetime.fromtimestamp(now).isoformat(timespec="milliseconds"),
                round(now - self._log_t0, 3),
                um(r["cx"]), um(r["cy"]),
                um(r["d4x"]), um(r["d4y"]), um(r["d4maj"]), um(r["d4min"]),
                round(-math.degrees(r["phi"]), 2),
                um(r["fit_wx"]), um(r["fit_wy"]), um(g2.get("wmaj")), um(g2.get("wmin")),
                header["status"]["peak_pct"], r["total"], s["exposure_ms"], s["gain_db"],
            ])
            self._log_rows += 1
            if self._log_rows % 10 == 0:
                self._log_file.flush()

    # ----------------------------------------------------------- quick save

    def quicksave(self, folder, name: str, note: str = "", raw: bool = False,
                  overwrite: bool = False, extra: dict | None = None) -> dict:
        """Save the current (or frozen) measurement as <name>.json in folder; see quicksave.py."""
        from . import quicksave

        last = self._last
        if last is None:
            raise CameraError("No frame available yet.")
        with self._save_lock:
            return quicksave.write_save(Path(folder), name, last, note=note, raw=raw,
                                        overwrite=overwrite, extra=extra)

    # ------------------------------------------------------------- snapshot

    def snapshot(self) -> dict:
        last = self._last
        if last is None:
            raise CameraError("No frame available yet.")
        folder = self.data_dir / "snapshots"
        folder.mkdir(parents=True, exist_ok=True)
        stem = datetime.now().strftime("beam_%Y%m%d_%H%M%S_%f")[:-3]
        from .quicksave import frame_to_tiff_bytes

        avg = last["avg"]
        n_avg = int(last["header"]["status"].get("avg_n") or 1)
        cam = last["header"].get("cam") or {}
        data, frame_info = frame_to_tiff_bytes(avg, n_avg, cam.get("bits"), f"Beam Profiler snapshot {stem}")
        (folder / f"{stem}.tif").write_bytes(data)
        files = [f"{stem}.tif"]
        meta = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "note": "The .tif holds the raw frame in ADC counts (16-bit, lossless deflate; before dark/baseline "
                    "subtraction; a mean of several frames is rounded to whole counts). "
                    "Results are in pixels; multiply lengths by um_per_px. Gaussian w = 1/e^2 radius; "
                    "d4* = D4-sigma diameters; phi = major-axis angle in image coordinates (rad, y down).",
            "frame": {"file": f"{stem}.tif", **frame_info},
            "um_per_px": last["header"].get("um_per_px"),
            "camera": last["header"]["cam"],
            "roi": last["header"]["roi"],
            "status": last["header"]["status"],
            "results": last["header"]["res"],
            "settings": last["settings"],
        }
        (folder / f"{stem}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        files.append(f"{stem}.json")
        return {"folder": str(folder.resolve()), "files": files, "stem": stem}
