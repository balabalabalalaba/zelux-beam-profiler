"""Quick save: one click writes the current measurement to a chosen folder.

Each save writes ``<name>.json`` (results, full-resolution profiles with their
fits, all settings, camera and status) and appends a row to
``quicksave_summary.csv`` in the same folder, so many saves can be analysed
together. Lengths in the file are in micrometres and widths are 1/e² **radii**;
the raw pixel results are kept as well (``results_px``). The raw frame is saved
as ``<name>.tif`` (16-bit, lossless deflate, about 1 MB) only on request.

The folder, the next file name and the options are remembered between runs in
``prefs.json`` (see :mod:`beamprofiler.prefs`).
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np

from . import __version__

FORMAT = "beamprofiler-quicksave"
SUMMARY_NAME = "quicksave_summary.csv"
SUMMARY_COLUMNS = [
    "time", "name", "note", "source",
    "centroid_x_um", "centroid_y_um",
    "d4sigma_radius_x_um", "d4sigma_radius_y_um", "d4sigma_radius_major_um", "d4sigma_radius_minor_um",
    "angle_deg",
    "gauss_radius_x_um", "gauss_radius_y_um", "gauss2d_radius_major_um", "gauss2d_radius_minor_um",
    "peak_percent", "saturated_px", "total_counts",
    "exposure_ms", "gain_db", "averaged_frames", "um_per_px", "wavelength_nm",
    "file",
]
DEFAULT_NAME = "beam_001"
_EXTS = (".json", ".tif", ".npy")  # .npy: raw frames saved by version 0.3.0

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class QuickSaveError(ValueError):
    pass


class NameExists(QuickSaveError):
    def __init__(self, name: str, folder: Path, suggestion: str):
        self.existing = [f"{name}{ext}" for ext in _EXTS if (folder / f"{name}{ext}").exists()]
        super().__init__(f"{' and '.join(self.existing) or name} already exist{'s' if len(self.existing) < 2 else ''} in {folder}")
        self.name = name
        self.folder = folder
        self.suggestion = suggestion


# ------------------------------------------------------------------ names


def clean_name(name: str) -> str:
    """Validate a file name typed by the user (without extension). Raises QuickSaveError."""
    name = (name or "").strip()
    if name.lower().endswith(".json"):
        name = name[:-5].rstrip()
    if not name:
        raise QuickSaveError("Type a file name first.")
    if len(name) > 120:
        raise QuickSaveError("The file name is too long (120 characters at most).")
    if _BAD_CHARS.search(name):
        raise QuickSaveError('A file name cannot contain  < > : " / \\ | ? *')
    if name.endswith((".", " ")):
        raise QuickSaveError("A file name cannot end with a dot or a space.")
    if name.split(".")[0].upper() in _RESERVED:
        raise QuickSaveError(f'"{name}" is a reserved name on Windows.')
    return name


def bump(name: str) -> str:
    """beam_009 -> beam_010, waist -> waist_2, run7 -> run8."""
    m = re.search(r"(\d+)$", name)
    if not m:
        return f"{name}_2"
    digits = m.group(1)
    return name[: m.start()] + str(int(digits) + 1).zfill(len(digits))


def taken(folder: Path, name: str) -> bool:
    return any((folder / f"{name}{ext}").exists() for ext in _EXTS)


def next_free(folder: Path, name: str) -> str:
    """The first name from ``name`` on (inclusive) that is not used in the folder."""
    for _ in range(100000):
        if not taken(folder, name):
            return name
        name = bump(name)
    raise QuickSaveError("Could not find a free file name.")


def clean_folder(folder: str) -> Path:
    folder = (folder or "").strip().strip('"')
    if not folder:
        raise QuickSaveError("Choose a folder first.")
    p = Path(os.path.expandvars(os.path.expanduser(folder)))
    if not p.is_absolute():
        raise QuickSaveError("Give the full path of the folder, e.g. C:\\Data\\Beam.")
    return p


# ---------------------------------------------------------------- content


def _finite(v):
    """Replace NaN/inf by None everywhere, so the JSON is valid for every reader."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _finite(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_finite(x) for x in v]
    if isinstance(v, np.generic):
        return _finite(v.item())
    return v


def _r(v, nd=3):
    if v is None:
        return None
    v = float(v)
    return round(v, nd) if math.isfinite(v) else None


def _profile(axis: dict | None, k: float) -> dict | None:
    if not axis:
        return None
    start, vals, fit = axis["start"], np.asarray(axis["values"], dtype=np.float64), axis["fit"]
    px = start + np.arange(vals.size, dtype=np.float64)
    out = {
        "position_px": px.tolist(),
        "position_um": np.round(px * k, 3).tolist(),
        "signal_counts": np.round(vals, 3).tolist(),
        "fit": None,
    }
    if fit:
        out["fit"] = {
            "model": "a * exp(-2 (x - x0)^2 / w^2) + c",
            "radius_um": _r(fit["w"] * k), "radius_px": _r(fit["w"]),
            "center_um": _r((fit["x0"] + start) * k), "center_px": _r(fit["x0"] + start),
            "amplitude_counts": _r(fit["a"]), "offset_counts": _r(fit["c"]),
            "r2": _r(fit.get("r2"), 5),
        }
    return out


def build_record(last: dict, name: str, note: str, extra: dict | None = None) -> dict:
    """The JSON document for one save, from the engine's last processed frame."""
    header, settings = last["header"], last["settings"]
    k = float(header.get("um_per_px") or (settings["pixel_size_um"] / settings["magnification"]))
    r = header.get("res")
    st = header.get("status") or {}
    cam = dict(header.get("cam") or {})
    if cam.get("kind") == "file":
        # the exposure/gain controls were never applied to a loaded image
        cam["exposure_ms"] = cam["gain_db"] = None

    def um(v, f=1.0):
        return None if v is None else _r(v * k * f)

    summary = None
    if r:
        g2 = r.get("g2") or {}
        summary = {
            "centroid_x_um": um(r["cx"]), "centroid_y_um": um(r["cy"]),
            "d4sigma_radius_x_um": um(r["d4x"], 0.5), "d4sigma_radius_y_um": um(r["d4y"], 0.5),
            "d4sigma_radius_major_um": um(r["d4maj"], 0.5), "d4sigma_radius_minor_um": um(r["d4min"], 0.5),
            "angle_deg": None if r.get("phi") is None else _r(-math.degrees(r["phi"]), 3),
            "gauss_radius_x_um": um(r.get("fit_wx")), "gauss_radius_y_um": um(r.get("fit_wy")),
            "gauss2d_radius_major_um": um(g2.get("wmaj")), "gauss2d_radius_minor_um": um(g2.get("wmin")),
            "peak_position_x_um": um(r.get("peak_x")), "peak_position_y_um": um(r.get("peak_y")),
            "edge_ratio": r.get("edge_ratio"),
        }
    prof = last.get("profiles") or {}
    rec = {
        "format": FORMAT,
        "format_version": 1,
        "program": f"Beam Profiler {__version__}",
        "name": name,
        "note": note,
        "time": datetime.now().isoformat(timespec="milliseconds"),
        "frame_time": datetime.fromtimestamp(header["t"]).isoformat(timespec="milliseconds") if header.get("t") else None,
        "definitions": (
            "Lengths in micrometres (_um) at the object plane (pixel size / magnification), or sensor pixels (_px). "
            "All widths here are 1/e^2 intensity RADII: d4sigma_radius = D4-sigma diameter / 2 (ISO 11146), "
            "gauss = 1D Gaussian fit to the profile, gauss2d = 2D elliptical Gaussian fit (only if enabled). "
            "angle_deg = major-axis angle, counter-clockwise on screen. Positions are sensor coordinates "
            "(x right, y down, pixel i centred on i). results_px holds the same results as streamed, in pixels, "
            "with d4* as D4-sigma DIAMETERS. Profile signal is in camera counts after baseline/dark subtraction."
        ),
        "um_per_px": _r(k, 6),
        "beam_found": bool(r),
        "summary": summary,
        "profiles": {
            "mode": prof.get("mode"),
            "slice_halfwidth_px": prof.get("halfwidth"),
            "through_px": prof.get("through"),
            "x": _profile(prof.get("x"), k),
            "y": _profile(prof.get("y"), k),
        },
        "results_px": r,
        "roi_px": header.get("roi"),
        "status": {
            "peak_percent": st.get("peak_pct"), "saturated_px": st.get("sat_px"),
            "averaged_frames": st.get("avg_n"), "baseline_counts": st.get("baseline"),
            "noise_counts": st.get("noise"), "dark_frame_subtracted": st.get("dark_used"),
            "dark_frame": st.get("dark"), "frame_number": st.get("frame"), "frozen": st.get("paused"),
        },
        "camera": cam,
        "settings": settings,
    }
    if extra:
        rec.update({k_: v for k_, v in extra.items() if k_ in ("view", "m2") and v is not None})
    return _finite(rec)


def summary_row(rec: dict, files: list[str]) -> list:
    s = rec.get("summary") or {}
    st = rec.get("status") or {}
    cam = rec.get("camera") or {}
    settings = rec.get("settings") or {}
    vals = {
        "time": rec["time"], "name": rec["name"], "note": rec["note"],
        "source": f"{cam.get('kind', '')}: {cam.get('name', '')}" if cam else "",
        **{c: s.get(c) for c in SUMMARY_COLUMNS if c in s},
        "peak_percent": st.get("peak_percent"), "saturated_px": st.get("saturated_px"),
        "total_counts": (rec.get("results_px") or {}).get("total"),
        "exposure_ms": cam.get("exposure_ms"),
        "gain_db": cam.get("gain_db"),
        "averaged_frames": st.get("averaged_frames"), "um_per_px": rec.get("um_per_px"),
        "wavelength_nm": settings.get("wavelength_nm"),
        "file": " ".join(files),
    }
    return ["" if vals.get(c) is None else vals.get(c) for c in SUMMARY_COLUMNS]


# ---------------------------------------------------------------- writing


def frame_to_tiff_bytes(avg: np.ndarray, n_avg: int, bit_depth: int | None = None,
                        description: str = "") -> tuple[bytes, dict]:
    """Encode a frame in ADC counts as a 16-bit TIFF with lossless deflate compression.

    One frame (n_avg == 1) is stored exactly. The mean of several frames is rounded to whole
    counts (at most 0.5 count off, below the camera noise). Returns (bytes, info for the JSON).
    """
    from PIL import Image

    a = np.asarray(avg, dtype=np.float64)
    exact = bool(np.all(a == np.round(a)))
    u16 = np.clip(np.round(a), 0, 65535).astype(np.uint16)
    desc = description or "Beam Profiler raw frame"
    desc += f"; ADC counts; {'1 frame' if n_avg <= 1 else f'mean of {n_avg} frames, rounded to whole counts'}"
    if bit_depth:
        desc += f"; camera bit depth {bit_depth}"
    img = Image.fromarray(u16)
    buf = io.BytesIO()
    compression = "tiff_adobe_deflate"
    try:
        img.save(buf, format="TIFF", compression=compression, tiffinfo={270: desc})
    except Exception:  # Pillow built without libtiff: plain TIFF, still lossless
        buf = io.BytesIO()
        compression = "none"
        img.save(buf, format="TIFF", tiffinfo={270: desc})
    info = {
        "dtype": "uint16",
        "compression": compression,
        "content": "raw frame in ADC counts, before dark and baseline subtraction"
                   + ("" if n_avg <= 1 else f"; mean of {n_avg} frames rounded to whole counts"),
        "averaged_frames": int(n_avg),
        "exact": exact,
    }
    return buf.getvalue(), info



def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


PENDING_NAME = ".quicksave_summary_pending.csv"


def _append_summary(folder: Path, rows: list[list]) -> str:
    """Append rows to quicksave_summary.csv (or a numbered copy if an older file has other columns).

    Raises OSError if the file cannot be written (e.g. it is open in Excel, which locks it on Windows).
    """
    name, n = SUMMARY_NAME, 1
    while True:
        path = folder / name
        if not path.exists():
            new = True
            break
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                first = next(csv.reader(f), None)
        except UnicodeDecodeError:
            first = None
        if first == SUMMARY_COLUMNS:
            new = False
            break
        n += 1
        name = SUMMARY_NAME.replace(".csv", f"_{n}.csv")
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    if new:
        w.writerow(SUMMARY_COLUMNS)
    w.writerows(rows)
    # utf-8-sig on a new file so Excel shows µ and other characters correctly
    with open(path, "a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
        f.write(buf.getvalue())
    return name


def _summary_with_pending(folder: Path, row: list) -> tuple[str | None, str | None]:
    """Add the row to the summary; rows that could not be written earlier are added first.

    Returns (summary file name, error message or None). A locked summary never loses a row:
    it waits in a hidden pending file until the summary can be written again.
    """
    pending_path = folder / PENDING_NAME
    pending: list[list] = []
    if pending_path.exists():
        try:
            with open(pending_path, newline="", encoding="utf-8") as f:
                pending = [r for r in csv.reader(f) if len(r) == len(SUMMARY_COLUMNS)]
        except (OSError, UnicodeDecodeError):
            pending = []
    try:
        name = _append_summary(folder, pending + [row])
    except OSError as e:
        try:
            with open(pending_path, "a", newline="", encoding="utf-8") as f:
                csv.writer(f, lineterminator="\r\n").writerow(row)
            kept = "The row is kept and will be added at the next save after you close it."
        except OSError:
            kept = "All the data are in the .json file."
        return None, (f"{SUMMARY_NAME} could not be written ({e.strerror or e}); "
                      f"it is probably open in Excel. {kept}")
    if pending:
        try:
            pending_path.unlink()
        except OSError:
            pass
    return name, None


def write_save(folder: Path, name: str, last: dict, note: str = "", raw: bool = False,
               overwrite: bool = False, extra: dict | None = None) -> dict:
    """Write one quick save. Raises NameExists (unless overwrite) or QuickSaveError/OSError."""
    name = clean_name(name)
    note = (note or "").strip()[:500]
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise QuickSaveError(f"Cannot create the folder {folder}: {e.strerror or e}") from e
    if not folder.is_dir():
        raise QuickSaveError(f"{folder} is not a folder.")
    if not overwrite and taken(folder, name):
        raise NameExists(name, folder, next_free(folder, bump(name)))

    rec = build_record(last, name, note, extra)
    files = [f"{name}.json"]
    if raw:
        st = last["header"].get("status") or {}
        cam = last["header"].get("cam") or {}
        data, info = frame_to_tiff_bytes(last["avg"], int(st.get("avg_n") or 1), cam.get("bits"),
                                         f"Beam Profiler quick save {name}")
        _atomic_write(folder / f"{name}.tif", data)
        files.append(f"{name}.tif")
        rec["raw_frame"] = {"file": f"{name}.tif", **info}
    _atomic_write(folder / f"{name}.json", json.dumps(rec, indent=1, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    if overwrite:
        # the user chose to replace <name>: an old raw frame would no longer match the new results
        for ext in ((".npy",) if raw else (".tif", ".npy")):
            try:
                (folder / f"{name}{ext}").unlink(missing_ok=True)
            except OSError:
                pass
    summary_file, summary_error = _summary_with_pending(folder, summary_row(rec, files))
    return {
        "folder": str(folder),
        "name": name,
        "files": files,
        "summary_file": summary_file,
        "summary_error": summary_error,
        "beam_found": rec["beam_found"],
        "next_name": next_free(folder, bump(name)),
    }
