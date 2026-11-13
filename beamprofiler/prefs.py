"""Small per-user preferences kept between runs (quick-save folder, next file name, options).

Stored as JSON in ``<local_dir>/prefs.json`` (``%LOCALAPPDATA%\\BeamProfiler`` on
Windows). Measurement settings are not stored here: they start from the
defaults and can be saved to / loaded from a settings file in the page.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from .appdirs import local_dir

_LOCK = threading.Lock()


def path() -> Path:
    return local_dir() / "prefs.json"


def load() -> dict:
    with _LOCK:
        try:
            data = json.loads(path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}


def update(section: str, patch: dict) -> dict:
    """Merge ``patch`` into ``prefs[section]``, write the file, and return the new section."""
    with _LOCK:
        try:
            data = json.loads(path().read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        sec = dict(data.get(section) or {})
        sec.update(patch)
        data[section] = sec
        p = path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".prefs-", dir=p.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1, ensure_ascii=False)
            os.replace(tmp, p)
        except OSError as e:
            print(f"Could not save preferences to {p}: {e}")
        return sec
