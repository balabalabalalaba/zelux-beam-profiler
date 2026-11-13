"""Find and load the Thorlabs scientific camera SDK at run time.

The SDK is not on PyPI and may not be redistributed with this program, so it
is taken from the user's own ThorCam installation:

- the 64-bit native DLLs (``thorlabs_tsi_camera_sdk.dll`` and friends), which
  ThorCam installs under ``C:\\Program Files\\Thorlabs\\Scientific Imaging``;
- the Python wrapper ``thorlabs_tsi_sdk`` (pure Python + ctypes), which ships
  as ``thorlabs_tsi_camera_python_sdk_package.zip`` inside ThorCam's
  ``Scientific_Camera_Interfaces.zip``.

If the wrapper cannot be imported, its ``.py`` files are extracted once into
``%LOCALAPPDATA%\\BeamProfiler\\thorlabs\\python`` and that folder is added to
``sys.path``. This works both from source and in the packaged ``.exe`` (which
cannot ``pip install``). DLLs found only inside the zip are extracted to
``%LOCALAPPDATA%\\BeamProfiler\\thorlabs\\dlls``.
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from .appdirs import app_dir, local_dir

DLL_NAME = "thorlabs_tsi_camera_sdk.dll"
PKG_ZIP = "thorlabs_tsi_camera_python_sdk_package.zip"
PKG_NAME = "thorlabs_tsi_sdk"

_MAX_DEPTH = 7


class SDKNotFound(RuntimeError):
    pass


def sdk_home() -> Path:
    return local_dir() / "thorlabs"


def python_dir() -> Path:
    return sdk_home() / "python"


def dll_cache_dir() -> Path:
    return sdk_home() / "dlls"


# ----------------------------------------------------------------- searching


def thorlabs_roots() -> list[Path]:
    """Folders where ThorCam installs its files."""
    roots = []
    for var in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(var)
        if base:
            roots.append(Path(base) / "Thorlabs" / "Scientific Imaging")
    return _unique_dirs(roots)


def search_roots(extra: list[str | Path] | None = None) -> list[Path]:
    """Folders searched for the SDK zip files: user-given first, then ThorCam, then local copies."""
    roots = [Path(p) for p in (extra or [])]
    roots += thorlabs_roots()
    roots += [app_dir(), app_dir() / "thorlabs", Path.home() / "Downloads"]
    return _unique_dirs(roots)


def _unique_dirs(paths) -> list[Path]:
    out, seen = [], set()
    for p in paths:
        try:
            key = str(p.resolve()).lower()
        except OSError:
            continue
        if key not in seen and p.is_dir():
            seen.add(key)
            out.append(p)
    return out


def _walk_files(roots: list[Path], predicate, max_depth: int = _MAX_DEPTH) -> list[Path]:
    hits = []
    for root in roots:
        base_depth = len(root.parts)
        for dirpath, dirs, files in os.walk(root, onerror=lambda _e: None):
            if len(Path(dirpath).parts) - base_depth >= max_depth:
                dirs[:] = []
            for f in files:
                if predicate(f):
                    hits.append(Path(dirpath) / f)
    return hits


def _is_64bit_dir(path: str) -> bool:
    low = path.lower().replace("\\", "/")
    return not ("32_lib" in low or "/x86" in low or "win32" in low)


def is_amd64_dll(path: str | Path) -> bool:
    """True if the file is a 64-bit (x64) Windows DLL. A 32-bit ThorCam's DLLs cannot load in this program."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
        off = int.from_bytes(head[0x3C:0x40], "little")
        return head[:2] == b"MZ" and head[off:off + 4] == b"PE\0\0" and \
            int.from_bytes(head[off + 4:off + 6], "little") == 0x8664
    except OSError:
        return False


def find_dll_dir(extra: str | Path | None = None) -> str | None:
    """Folder holding the 64-bit Thorlabs native DLLs, or None."""
    candidates: list[Path] = []
    if extra:
        candidates.append(Path(extra))
    env = os.environ.get("THORLABS_DLL_DIR")
    if env:
        candidates.append(Path(env))
    candidates += [
        dll_cache_dir(),
        app_dir() / "thorlabs_dlls",
        Path.cwd() / "thorlabs_dlls",
        Path.cwd(),
    ]
    # Off Windows (tests) any file with the right name counts; on Windows it must be a 64-bit DLL.
    ok = is_amd64_dll if sys.platform == "win32" else (lambda f: Path(f).is_file())
    for c in candidates:
        if ok(c / DLL_NAME):
            return str(c)
    if sys.platform != "win32":
        return None
    best = None
    for root in thorlabs_roots():
        for dirpath, _dirs, files in os.walk(root, onerror=lambda _e: None):
            if DLL_NAME not in files or not _is_64bit_dir(dirpath) or not ok(Path(dirpath) / DLL_NAME):
                continue
            if "64" in dirpath:
                return dirpath
            # ThorCam's own program folder holds the 64-bit DLLs too.
            best = best or dirpath
    return best


def prepare_dll_path(dll_dir: str | Path | None = None) -> str | None:
    """Make the native DLLs loadable (PATH and os.add_dll_directory). Returns the folder used."""
    found = find_dll_dir(dll_dir)
    if found:
        os.environ["PATH"] = found + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            try:
                handle = os.add_dll_directory(found)
                _DLL_HANDLES.append(handle)  # keep alive: closing the handle removes the directory
            except OSError:
                pass
    return found


_DLL_HANDLES: list = []


# --------------------------------------------------------------- extracting


def _safe_rel(name: str) -> PurePosixPath | None:
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or not p.parts:
        return None
    return p


def _extract_package(pkg_zip: zipfile.ZipFile, target: Path) -> bool:
    """Copy the thorlabs_tsi_sdk/*.py files out of the Python SDK package zip."""
    files = []
    for name in pkg_zip.namelist():
        rel = _safe_rel(name)
        if rel is None or PKG_NAME not in rel.parts or name.endswith("/"):
            continue
        i = rel.parts.index(PKG_NAME)
        sub = PurePosixPath(*rel.parts[i:])
        if sub.suffix.lower() not in (".py", ".pyd", ".dll", ".txt", ".typed"):
            continue
        files.append((name, sub))
    if not any(sub.name == "__init__.py" and len(sub.parts) == 2 for _n, sub in files):
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="sdk-", dir=target.parent))
    try:
        for name, sub in files:
            out = tmp / Path(*sub.parts)
            out.parent.mkdir(parents=True, exist_ok=True)
            with pkg_zip.open(name) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        os.replace(tmp / PKG_NAME, target / PKG_NAME)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return True


def _extract_dlls(zf: zipfile.ZipFile, target: Path) -> int:
    names = [n for n in zf.namelist() if n.lower().endswith(".dll") and _is_64bit_dir(n)]
    names = [n for n in names if "64" in n] or names
    native = [n for n in names if "native" in n.lower()] or names
    if not any(PurePosixPath(n.replace("\\", "/")).name.lower() == DLL_NAME for n in native):
        return 0
    target.mkdir(parents=True, exist_ok=True)
    for n in native:
        with zf.open(n) as src, open(target / PurePosixPath(n.replace("\\", "/")).name, "wb") as dst:
            shutil.copyfileobj(src, dst)
    return len(native)


def install_from_zip(zpath: Path, need_pkg: bool = True, need_dlls: bool = True, log=print) -> tuple[bool, bool]:
    """Take the Python package and/or DLLs from a ThorCam SDK zip.

    ``zpath`` may be ``Scientific_Camera_Interfaces.zip`` (which contains the
    package zip) or the package zip itself. Returns (got_package, got_dlls).
    """
    got_pkg = got_dlls = False
    with zipfile.ZipFile(zpath) as z:
        if need_pkg:
            if _extract_package(z, python_dir()):
                got_pkg = True
            else:
                member = next((n for n in z.namelist() if n.replace("\\", "/").endswith(PKG_ZIP)), None)
                if member:
                    with z.open(member) as f:
                        inner = zipfile.ZipFile(io.BytesIO(f.read()))
                    with inner:
                        got_pkg = _extract_package(inner, python_dir())
            if got_pkg:
                log(f"Thorlabs Python SDK taken from {zpath}")
        if need_dlls:
            n = _extract_dlls(z, dll_cache_dir())
            if n:
                got_dlls = True
                log(f"Copied {n} Thorlabs DLLs from {zpath} to {dll_cache_dir()}")
    return got_pkg, got_dlls


def candidate_zips(extra: list[str | Path] | None = None) -> list[Path]:
    """SDK zips on this computer: loose Python package zips first, then the full SDK zips."""
    roots = search_roots(extra)
    loose = _walk_files(roots, lambda f: f.lower() == PKG_ZIP)
    full = _walk_files(roots, lambda f: f.lower().endswith(".zip") and "camera" in f.lower() and "interface" in f.lower())
    return loose + [z for z in full if z not in loose]


def _add_python_dir() -> None:
    p = str(python_dir())
    if (python_dir() / PKG_NAME / "__init__.py").is_file() and p not in sys.path:
        sys.path.insert(0, p)


def have_python_sdk() -> bool:
    _add_python_dir()
    try:
        import thorlabs_tsi_sdk  # noqa: F401

        return True
    except ImportError:
        return False


def ensure(zip_path: str | Path | None = None, search: list[str | Path] | None = None, log=print) -> dict:
    """Make the Python wrapper importable and the DLLs findable, extracting from ThorCam's zip if needed.

    Returns {"python": bool, "dll_dir": str|None, "sources": [...]}. Never raises for "not found".
    """
    need_pkg = not have_python_sdk()
    need_dlls = find_dll_dir() is None
    sources: list[str] = []
    if need_pkg or need_dlls:
        zips = [Path(zip_path)] if zip_path else candidate_zips(search)
        for zp in zips:
            if not zp.is_file():
                log(f"Not found: {zp}")
                continue
            try:
                got_pkg, got_dlls = install_from_zip(zp, need_pkg, need_dlls, log=log)
            except (zipfile.BadZipFile, OSError) as e:
                log(f"Could not read {zp}: {e}")
                continue
            if got_pkg or got_dlls:
                sources.append(str(zp))
            need_pkg = need_pkg and not got_pkg
            need_dlls = need_dlls and not got_dlls
            if not (need_pkg or need_dlls):
                break
        if not need_pkg:
            have_python_sdk()
    return {"python": not need_pkg, "dll_dir": find_dll_dir(), "sources": sources}


def load(dll_dir: str | Path | None = None, log=print):
    """Return (TLCameraSDK, OPERATION_MODE, dll_folder) ready to use, or raise SDKNotFound."""
    if not have_python_sdk() or find_dll_dir(dll_dir) is None:
        ensure(log=log)
    found = prepare_dll_path(dll_dir)
    try:
        from thorlabs_tsi_sdk.tl_camera import OPERATION_MODE, TLCameraSDK
    except ImportError as e:
        raise SDKNotFound(
            "The Thorlabs camera SDK was not found. Install ThorCam from thorlabs.com "
            "(it includes the SDK), then press Retry camera. If ThorCam is installed in an unusual "
            "place, copy its Scientific_Camera_Interfaces.zip into the program's folder."
        ) from e
    return TLCameraSDK, OPERATION_MODE, found
