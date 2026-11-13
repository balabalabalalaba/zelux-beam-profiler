"""Desktop launcher: the profiler as one Windows program with its own window.

This is the entry point of the packaged ``BeamProfiler.exe``. It runs the same
server as ``python -m beamprofiler`` and shows the page in an app-style window
(Microsoft Edge or Google Chrome with ``--app``, which every Windows 10/11 PC
has), so no Python installation, console or separate browser tab is needed.

Life cycle
- If a profiler is already running on the port, just open another window to it.
- Otherwise start the server in a thread, open the window, and stop everything
  (closing the camera) shortly after the last page connection closes.
- Without Edge/Chrome the default browser is used, with the same rule.

The packaged program has no console: output goes to
``%LOCALAPPDATA%\\BeamProfiler\\beamprofiler.log`` and fatal errors are shown
in a message box.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from . import __version__
from .appdirs import is_frozen, local_dir

APP_TITLE = "Beam Profiler"
FIRST_CONNECT_TIMEOUT_S = 300.0  # quit if no page ever connects
GRACE_WINDOW_OPEN_S = 30.0  # no page connected, but our window process still runs (e.g. page reloading)
GRACE_NO_WINDOW_S = 4.0  # no page connected and the window process has ended


# ---------------------------------------------------------------- output


def log_path() -> Path:
    return local_dir() / "beamprofiler.log"


def _setup_output() -> None:
    """Send print/uvicorn output to a log file when there is no console (windowed .exe)."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > 2_000_000:
            os.replace(path, path.with_suffix(".old.log"))
        f = open(path, "a", encoding="utf-8", buffering=1, errors="replace")
    except OSError:
        f = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stdout or f
    sys.stderr = sys.stderr or f
    print(f"\n==== {APP_TITLE} {__version__} started {time.strftime('%Y-%m-%d %H:%M:%S')} ====")


def message_box(text: str, title: str = APP_TITLE, error: bool = True) -> None:
    print(f"[{title}] {text}")
    if sys.platform == "win32":
        try:
            import ctypes

            flags = 0x10 if error else 0x40  # MB_ICONERROR / MB_ICONINFORMATION
            ctypes.windll.user32.MessageBoxW(None, text, title, flags | 0x10000)  # MB_SETFOREGROUND
        except Exception:
            pass


# ------------------------------------------------------------ networking


PORT_SPAN = 10  # if the port is taken, try the next ones (fixed range keeps view settings and detection)

# Loopback requests must not go through a proxy set in Windows' Internet Options.
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def local_request(url: str, data: bytes | None = None, timeout: float = 10.0):
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json"} if data is not None else {})
    return _LOCAL.open(req, timeout=timeout)


def _probe(port: int, hold: bool = False) -> tuple[str, str | None]:
    """('ours', mode) if a beam profiler answers on the port, ('other', None) if something else
    uses it, ('free', None) otherwise. ``hold`` asks a running profiler to wait for a new window."""
    try:
        with local_request(f"http://127.0.0.1:{port}/api/state" + ("?hold=1" if hold else ""), timeout=1.5) as r:
            data = json.loads(r.read().decode("utf-8"))
            if isinstance(data, dict) and "settings" in data and "max_averaging" in data:
                return "ours", data.get("launch_mode")
            return "other", None
    except Exception:
        pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return "free", None
        except OSError:
            return "other", None


def choose_port(base: int, mode: str) -> tuple[str, int | None]:
    """('reuse', port) for a running profiler in the same mode, else ('new', first free port) or ('none', None)."""
    first_free = None
    for port in range(base, base + PORT_SPAN + 1):
        status, their_mode = _probe(port)
        if status == "ours" and their_mode in (mode, None):
            _probe(port, hold=True)
            return "reuse", port
        if status == "free" and first_free is None:
            first_free = port
    return ("new", first_free) if first_free is not None else ("none", None)


_INSTANCE_LOCK = None


def acquire_instance_lock(mode: str) -> bool:
    """True if no other Beam Profiler in this mode is starting or running (Windows named mutex)."""
    global _INSTANCE_LOCK
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, f"Local\\BeamProfiler-{mode}")
        already = kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
        _INSTANCE_LOCK = handle  # held until the process ends
        return not already
    except Exception:
        return True


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------- window


def _registry_app_path(exe: str) -> str | None:
    try:
        import winreg
    except ImportError:
        return None
    key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
    views = [(winreg.HKEY_CURRENT_USER, 0), (winreg.HKEY_LOCAL_MACHINE, 0),
             (winreg.HKEY_LOCAL_MACHINE, getattr(winreg, "KEY_WOW64_32KEY", 0))]
    for hive, view in views:
        try:
            with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as k:
                val, _ = winreg.QueryValueEx(k, None)
                if val and Path(val.strip('"')).is_file():
                    return val.strip('"')
        except OSError:
            continue
    return None


def find_app_browser() -> str | None:
    """Path of a Chromium browser that supports --app windows (Edge first, then Chrome)."""
    env = os.environ.get("BEAMPROFILER_BROWSER")
    if env and Path(env).is_file():
        return env
    if sys.platform != "win32":
        return None
    import shutil

    for exe, rel in (("msedge.exe", r"Microsoft\Edge\Application\msedge.exe"),
                     ("chrome.exe", r"Google\Chrome\Application\chrome.exe")):
        found = _registry_app_path(exe)
        if found:
            return found
        for var in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base and (Path(base) / rel).is_file():
                return str(Path(base) / rel)
        found = shutil.which(exe)
        if found:
            return found
    return None


def open_window(url: str, use_default_browser: bool = False) -> subprocess.Popen | None:
    """Open the page in an app window. Returns the window's process, or None if the default browser was used."""
    exe = None if use_default_browser else find_app_browser()
    if exe:
        profile = local_dir() / "window-profile"
        profile.mkdir(parents=True, exist_ok=True)
        cmd = [
            exe,
            f"--app={url}",
            f"--user-data-dir={profile.resolve()}",  # own profile: own process, remembered view settings
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-extensions",
            "--start-maximized",
        ]
        if Path(exe).name.lower() == "msedge.exe":
            # Edge may relaunch itself (de-elevation, compatibility layer) and ask to sign in;
            # these keep it to one quiet window. Unknown switches are ignored by Chromium.
            cmd += ["--do-not-de-elevate", "--edge-skip-compat-layer-relaunch",
                    "--disable-features=msForceBrowserSignIn"]
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            print(f"Opening window: {exe}")
            return subprocess.Popen(cmd, close_fds=True, creationflags=flags,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except OSError as e:
            print(f"Could not start {exe}: {e}")
    print(f"Opening {url} in the default browser")
    webbrowser.open(url)
    return None


# --------------------------------------------------------------- server


class ServerThread:
    def __init__(self, app, host: str, port: int):
        import uvicorn

        self.app = app
        # log_config=None: no console formatter (there is no console), messages go to the log file.
        config = uvicorn.Config(app, host=host, port=port, log_level="warning", log_config=None,
                                access_log=False, lifespan="on", timeout_graceful_shutdown=3)
        self.server = uvicorn.Server(config)
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, name="server", daemon=True)

    def _run(self) -> None:
        try:
            self.server.run()
        except BaseException as e:  # SystemExit from a failed bind, among others
            self.error = e

    def start(self, timeout: float = 30.0) -> bool:
        self.thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.server.started:
                return True
            if not self.thread.is_alive():
                return False
            time.sleep(0.05)
        return False

    def stop(self, timeout: float = 8.0) -> None:
        self.server.should_exit = True
        self.thread.join(timeout)
        if self.thread.is_alive():
            self.server.force_exit = True
            self.thread.join(2.0)


def _wait_for_last_window(srv: ServerThread, proc: subprocess.Popen | None, url: str) -> None:
    state = srv.app.state
    t0 = time.monotonic()
    fallback_done = proc is None
    while srv.thread.is_alive():
        time.sleep(0.5)
        now = time.monotonic()
        window_running = proc is not None and proc.poll() is None
        if not state.ws_ever:
            if not window_running and not fallback_done and now - t0 > 25.0:
                # The app window never connected (browser blocked or closed early): try the default browser.
                fallback_done = True
                open_window(url, use_default_browser=True)
            if now - t0 > FIRST_CONNECT_TIMEOUT_S:
                print("No page connected; stopping.")
                return
            continue
        if state.ws_clients > 0:
            continue
        idle = now - state.ws_changed
        if idle > (GRACE_WINDOW_OPEN_S if window_running else GRACE_NO_WINDOW_S):
            print("Last page closed; stopping.")
            return


def _windowed() -> bool:
    """True for the packaged program without a console (BeamProfiler.exe, not BeamProfilerConsole.exe)."""
    return is_frozen() and sys.stdin is None


def run(args) -> int:
    from .server import create_app, make_engine

    mode = args.camera
    first = acquire_instance_lock(mode)
    deadline = time.monotonic() + (0 if first else 60.0)
    while True:
        if args.lan:  # other computers are told this port: no moving to another one
            status, their_mode = _probe(args.port)
            if status == "ours" and their_mode in (mode, None):
                _probe(args.port, hold=True)
                kind, port = "reuse", args.port
            else:
                kind, port = ("new", args.port) if status == "free" else ("none", None)
        else:
            kind, port = choose_port(args.port, mode)
        if kind == "reuse":
            print(f"A beam profiler is already running on port {port}; opening a window to it.")
            if not args.no_browser:
                open_window(f"http://127.0.0.1:{port}/", args.browser)
            return 0
        if first or time.monotonic() > deadline:
            break
        time.sleep(0.5)  # another launch is still starting up: wait for its server
    if not first:
        message_box("Another Beam Profiler is starting but did not open its window. "
                    f"Wait a moment and try again, or end it in Task Manager.\n\nDetails are in {log_path()}")
        return 1
    if kind != "new":
        message_box(f"Port {args.port} " + ("is" if args.lan else f"to {args.port + PORT_SPAN} are") +
                    " in use by other programs, so the profiler cannot start.")
        return 1
    if port != args.port:
        print(f"Port {args.port} is busy; using {port}.")

    engine = make_engine(args)
    app = create_app(engine, lan=args.lan)
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    srv = ServerThread(app, host, port)
    if not srv.start():
        engine.shutdown()
        detail = f"\n\n{srv.error!r}" if srv.error else ""
        message_box(f"The profiler could not start its local server on port {port}.{detail}\n\n"
                    f"Details are in {log_path()}")
        return 1

    url = f"http://127.0.0.1:{port}/"
    print(f"Beam profiler running at {url}")
    proc = None
    try:
        if args.no_browser:
            if _windowed():
                # No console and no window: this box is the only way to stop it.
                message_box(f"Beam Profiler is running at {url}\n\nClick OK to stop it and release the camera.",
                            error=False)
            else:
                print("Running without a window (--no-browser). Press Ctrl+C to stop.")
                while srv.thread.is_alive():
                    time.sleep(0.5)
        else:
            proc = open_window(url, args.browser)
            _wait_for_last_window(srv, proc, url)
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
        engine.shutdown()  # closes the camera and log even if the server did not shut down cleanly
        if proc is not None and proc.poll() is None:
            # Edge/Chrome may keep a background process alive after its window closed.
            try:
                proc.terminate()
            except OSError:
                pass
    print("Stopped.")
    return 0


# ------------------------------------------------------------ smoke test


def smoke_test(out: str | None) -> int:
    """Self-test of the packaged program (used by the build): simulator, page, stream, snapshot, SDK loading.

    Writes a JSON report to ``out`` (if given) and returns 0 when every check passed.
    """
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="beamprofiler-smoke-"))
    os.environ["BEAMPROFILER_HOME"] = str(tmp / "home")
    checks: list[dict] = []

    def check(name, fn):
        try:
            detail = fn()
            checks.append({"name": name, "ok": True, "detail": detail})
        except Exception as e:  # noqa: BLE001
            checks.append({"name": name, "ok": False, "detail": f"{type(e).__name__}: {e}",
                           "trace": traceback.format_exc()})

    srv = None
    engine = None
    try:
        from .cameras import CameraError, SimulatedCamera
        from .engine import Engine
        from .server import create_app

        engine = Engine(data_dir=tmp / "data", live_factory=SimulatedCamera)
        assert engine.connect_live(), engine.cam_error
        app = create_app(engine)
        port = free_port()
        srv = ServerThread(app, "127.0.0.1", port)
        base = f"http://127.0.0.1:{port}"

        def server_starts():
            assert srv.start(), f"server did not start: {srv.error!r}"
            return base

        def get(path):
            with local_request(base + path, timeout=10) as r:
                return r.status, r.read()

        def post(path, body=b"{}"):
            with local_request(base + path, data=body, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))

        def page():
            code, body = get("/")
            assert code == 200 and b"Beam Profiler" in body
            for f in ("app.js", "style.css"):
                code, body = get(f"/static/{f}")
                assert code == 200 and len(body) > 1000, f
            return "index.html, app.js, style.css served"

        def state():
            _c, body = get("/api/state")
            st = json.loads(body)
            assert st["camera"]["kind"] == "sim", st["camera"]
            return st["camera"]

        def stream():
            from websockets.sync.client import connect

            with connect(f"ws://127.0.0.1:{port}/ws", open_timeout=10, max_size=None) as ws:
                for _ in range(3):  # a few frames, so the analysis has run on real data
                    pkt = ws.recv(timeout=20)
            n = int.from_bytes(pkt[:4], "little")
            hdr = json.loads(pkt[4:4 + n].decode("utf-8"))
            img = pkt[4 + n:]
            assert len(img) == hdr["img"]["w"] * hdr["img"]["h"], (len(img), hdr["img"])
            res = hdr.get("res") or {}
            return {"bytes": len(pkt), "img": hdr["img"], "result_keys": sorted(res)[:20]}

        def closed_page_noticed_while_paused():
            from websockets.sync.client import connect

            post("/api/settings", json.dumps({"paused": True}).encode())
            try:
                time.sleep(0.5)  # let the last frame go out; nothing is sent after that
                with connect(f"ws://127.0.0.1:{port}/ws", open_timeout=10, max_size=None):
                    time.sleep(0.5)
                    assert app.state.ws_clients == 1, app.state.ws_clients
                t = time.monotonic()
                while app.state.ws_clients and time.monotonic() - t < 3:
                    time.sleep(0.05)
                assert app.state.ws_clients == 0, "closed page still counted while paused"
                return f"noticed after {time.monotonic() - t:.2f} s"
            finally:
                post("/api/settings", json.dumps({"paused": False}).encode())

        def snapshot():
            r = post("/api/snapshot")
            exts = sorted(Path(f).suffix for f in r["files"])
            assert exts == [".json", ".tif"], exts
            from PIL import Image

            with Image.open(Path(r["folder"]) / f"{r['stem']}.tif") as im:  # 16-bit, compressed, readable
                assert im.mode == "I;16" and im.size == (1440, 1080), (im.mode, im.size)
                assert im.info.get("compression") == "tiff_adobe_deflate", im.info.get("compression")
            return r["files"]

        def quick_save():
            body = json.dumps({"name": "smoke_001", "note": "smoke test µ"}).encode()
            r = post("/api/quicksave", body)
            folder = Path(r["folder"])
            rec = json.loads((folder / "smoke_001.json").read_text(encoding="utf-8"))
            s = rec["summary"]
            truth = engine.camera.params
            assert abs(s["d4sigma_radius_major_um"] / truth["wx_um"] - 1) < 0.05, s
            import csv

            with open(folder / "quicksave_summary.csv", newline="", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            assert [row["name"] for row in rows] == ["smoke_001"], rows
            try:
                post("/api/quicksave", body)
                raise AssertionError("an existing file was overwritten without asking")
            except urllib.error.HTTPError as e:
                assert e.code == 409, e.code
            return {"files": r["files"], "next_name": r["next_name"],
                    "d4sigma_radius_major_um": s["d4sigma_radius_major_um"]}

        def thorlabs_without_sdk():
            from .cameras import ThorlabsCamera

            try:
                cam = ThorlabsCamera()
            except CameraError as e:
                return f"clean CameraError: {e}"
            cam.close()
            return "a Thorlabs camera was opened"

        def thorlabs_sdk_extraction():
            return _fake_sdk_roundtrip(tmp / "fake-sdk")

        check("server starts", server_starts)
        if checks[-1]["ok"]:
            check("page and static files", page)
            check("api/state", state)
            check("websocket frame stream", stream)
            check("closed page noticed while paused", closed_page_noticed_while_paused)
            check("snapshot (16-bit tif, json)", snapshot)
            check("quick save (json, summary csv, no silent overwrite)", quick_save)
        check("Thorlabs camera without SDK fails cleanly", thorlabs_without_sdk)
        check("Thorlabs SDK extraction from a ThorCam-style zip", thorlabs_sdk_extraction)
    except Exception as e:  # noqa: BLE001
        checks.append({"name": "setup", "ok": False, "detail": f"{type(e).__name__}: {e}",
                       "trace": traceback.format_exc()})
    finally:
        if srv is not None and srv.thread.is_alive():
            srv.stop()
        elif engine is not None:
            engine.shutdown()

    ok = bool(checks) and all(c["ok"] for c in checks)
    report = {"ok": ok, "version": __version__, "frozen": is_frozen(), "python": sys.version,
              "platform": sys.platform, "checks": checks}
    text = json.dumps(report, indent=2)
    print(text)
    if out:
        Path(out).write_text(text, encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


# A stand-in for thorlabs_tsi_sdk with the same imports as the real package, so the
# smoke test proves the packaged program can import the user's SDK from disk.
_FAKE_TL_CAMERA = '''
# Same imports as tl_camera.py in thorlabs_tsi_sdk 0.0.8 (ThorCam's Scientific Camera Interfaces).
from ctypes import cdll, create_string_buffer, POINTER, CFUNCTYPE, c_int, c_ushort, c_void_p, c_char_p, c_uint, \\
    c_char, c_double, c_bool, c_float, c_longlong
from typing import Callable, Any, Optional, NamedTuple, List
from traceback import format_exception
import logging
import platform
import struct
import decimal
import sys

import numpy as np

from .tl_camera_enums import OPERATION_MODE


class TLCameraSDK:
    FAKE = True
'''

_FAKE_ENUMS = '''
from enum import IntEnum


class OPERATION_MODE(IntEnum):
    SOFTWARE_TRIGGERED = 0
    HARDWARE_TRIGGERED = 1
    BULB = 2
'''


def fake_pe(machine: int) -> bytes:
    """Minimal DLL header for tests: 0x8664 = x64, 0x14C = 32-bit x86."""
    head = bytearray(0x48)
    head[:2] = b"MZ"
    head[0x3C:0x40] = (0x40).to_bytes(4, "little")
    head[0x40:0x44] = b"PE\0\0"
    head[0x44:0x46] = machine.to_bytes(2, "little")
    return bytes(head)


def _fake_sdk_roundtrip(work: Path) -> str:
    import io
    import zipfile

    from . import thorlabs_sdk

    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("thorlabs_tsi_camera_python_sdk/setup.py", "")
        z.writestr("thorlabs_tsi_camera_python_sdk/thorlabs_tsi_sdk/__init__.py", "")
        z.writestr("thorlabs_tsi_camera_python_sdk/thorlabs_tsi_sdk/tl_camera.py", _FAKE_TL_CAMERA)
        z.writestr("thorlabs_tsi_camera_python_sdk/thorlabs_tsi_sdk/tl_camera_enums.py", _FAKE_ENUMS)
    work.mkdir(parents=True, exist_ok=True)
    outer = work / "Scientific_Camera_Interfaces.zip"
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr("Scientific Camera Interfaces/SDK/Python Toolkit/" + thorlabs_sdk.PKG_ZIP, inner.getvalue())
        z.writestr("Scientific Camera Interfaces/SDK/Native Toolkit/dlls/Native_64_lib/" + thorlabs_sdk.DLL_NAME,
                   fake_pe(0x8664))
        z.writestr("Scientific Camera Interfaces/SDK/Native Toolkit/dlls/Native_32_lib/" + thorlabs_sdk.DLL_NAME,
                   fake_pe(0x14C))
    for name in [m for m in sys.modules if m == "thorlabs_tsi_sdk" or m.startswith("thorlabs_tsi_sdk.")]:
        sys.modules.pop(name, None)
    result = thorlabs_sdk.ensure(zip_path=outer, log=lambda *_a: None)
    assert result["python"], result
    assert result["dll_dir"] and Path(result["dll_dir"]) == thorlabs_sdk.dll_cache_dir(), result
    assert thorlabs_sdk.is_amd64_dll(Path(result["dll_dir"]) / thorlabs_sdk.DLL_NAME), "picked the 32-bit DLL"
    from thorlabs_tsi_sdk.tl_camera import OPERATION_MODE, TLCameraSDK

    assert TLCameraSDK.FAKE and OPERATION_MODE.SOFTWARE_TRIGGERED == 0
    import thorlabs_tsi_sdk

    loaded = Path(thorlabs_tsi_sdk.__file__).resolve()
    assert thorlabs_sdk.python_dir().resolve() in loaded.parents, loaded
    for name in [m for m in sys.modules if m == "thorlabs_tsi_sdk" or m.startswith("thorlabs_tsi_sdk.")]:
        sys.modules.pop(name, None)
    sys.path[:] = [p for p in sys.path if p != str(thorlabs_sdk.python_dir())]
    return f"package imported from {loaded.parent}"


# ------------------------------------------------------------------ main


def main(argv=None) -> int:
    _setup_output()
    from .server import build_arg_parser

    ap = build_arg_parser(prog="BeamProfiler")
    ap.add_argument("--browser", action="store_true",
                    help="Open the page in the default browser instead of an app window.")
    ap.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--smoke-out", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--version", action="version", version=f"{APP_TITLE} {__version__}")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        if e.code not in (0, None) and is_frozen() and sys.platform == "win32":
            message_box(f"Unknown command-line option.\n\n{ap.format_usage()}")
        raise
    if args.smoke_test:
        return smoke_test(args.smoke_out)
    try:
        return run(args)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        message_box(f"{APP_TITLE} stopped because of an error:\n\n{type(e).__name__}: {e}\n\n"
                    f"Details are in {log_path()}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
