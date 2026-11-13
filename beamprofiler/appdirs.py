"""Where the program lives and where it keeps its files.

Running from source the behaviour is unchanged (data goes to ./data). The
packaged Windows program (PyInstaller, ``sys.frozen``) cannot write next to
itself when installed under Program Files, so it uses:

- ``Documents\\BeamProfiler``             snapshots and logs (the user's files)
- ``%LOCALAPPDATA%\\BeamProfiler``         program state: log file, Thorlabs SDK
                                           files taken from ThorCam, browser profile
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "BeamProfiler"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Folder of the program: the .exe's folder when packaged, else the project root."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _windows_known_folder(csidl: int) -> Path | None:
    try:
        import ctypes
        from ctypes import wintypes

        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        # SHGetFolderPathW follows folder redirection (e.g. Documents moved to OneDrive).
        if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 and buf.value:
            return Path(buf.value)
    except Exception:
        pass
    return None


def local_dir() -> Path:
    """Per-user folder for program state (not the user's measurement files)."""
    override = os.environ.get("BEAMPROFILER_HOME")  # used by tests and the build smoke test
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else (_windows_known_folder(0x001C) or Path.home() / "AppData" / "Local")
        return root / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / APP_NAME.lower()


def documents_dir() -> Path:
    if sys.platform == "win32":
        found = _windows_known_folder(0x0005)  # CSIDL_PERSONAL (Documents)
        if found:
            return found
    docs = Path.home() / "Documents"
    return docs if docs.is_dir() else Path.home()


def default_data_dir() -> Path:
    """Where snapshots and logs go when --data-dir is not given."""
    if is_frozen():
        return documents_dir() / APP_NAME
    return Path("data")
