"""Web server: serves the profiler page, streams frames, exposes controls."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, analysis, folderpicker, prefs, quicksave
from .appdirs import default_data_dir
from .cameras import CameraError, SimulatedCamera
from .engine import Engine

STATIC = Path(__file__).resolve().parent / "static"
MAX_UPLOAD = 80 * 1024 * 1024
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def _host_name(netloc: str) -> str:
    """'127.0.0.1:8765' -> '127.0.0.1', '[::1]:8765' -> '[::1]'."""
    if netloc.startswith("["):
        return netloc.split("]")[0] + "]"
    return netloc.rsplit(":", 1)[0] if ":" in netloc else netloc


def create_app(engine: Engine, lan: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        engine.start()
        yield
        engine.shutdown()

    app = FastAPI(title="Beam Profiler", version=__version__, lifespan=lifespan)
    # Open page connections; the desktop launcher stops the program when the last one closes.
    app.state.ws_clients = 0
    app.state.ws_ever = False
    app.state.ws_changed = time.monotonic()

    @app.middleware("http")
    async def _same_site_only(request, call_next):
        """Refuse requests that a web page from another site could make to this local server.

        A page on any website open in the lab PC's browser can send requests to 127.0.0.1
        (e.g. a form POST). Browsers mark them with an Origin header, so any state-changing
        request whose Origin is not this server is refused. Without --lan, the Host header
        must also be a loopback name (protects against DNS rebinding).
        """
        async def refuse(detail: str) -> JSONResponse:
            # Read the request body first (up to 1 MB): answering before it is read makes the
            # server close the connection, and Windows may then cut off the 403 reply.
            seen = 0
            try:
                async for chunk in request.stream():
                    seen += len(chunk)
                    if seen > 1_000_000:
                        break
            except Exception:
                pass
            return JSONResponse({"detail": detail}, status_code=403)

        host = request.headers.get("host", "")
        if not lan and _host_name(host).lower() not in _LOCAL_HOSTS:
            return await refuse("Unexpected host.")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin is not None and urlsplit(origin).netloc != host:
                return await refuse("Cross-site request refused.")
        return await call_next(request)

    @app.exception_handler(CameraError)
    async def _camera_error(_req, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/state")
    async def state(hold: int = 0):
        if hold:
            # Another launch is about to open a window to this instance: do not stop meanwhile.
            app.state.ws_changed = max(app.state.ws_changed, time.monotonic() + 20.0)
        return engine.state()

    @app.post("/api/settings")
    async def settings(req: Request):
        patch = await req.json()
        if not isinstance(patch, dict):
            raise HTTPException(400, "Expected a JSON object.")
        engine.update_settings(patch)
        return engine.state()

    @app.post("/api/dark/capture")
    async def dark_capture(req: Request):
        body = await req.json() if await req.body() else {}
        engine.capture_dark(int(body.get("frames", 16)))
        return {"ok": True}

    @app.post("/api/dark/clear")
    async def dark_clear():
        engine.clear_dark()
        return engine.state()

    @app.post("/api/snapshot")
    async def snapshot():
        return await asyncio.to_thread(engine.snapshot)

    @app.get("/api/files/{kind}/{name}")
    async def files(kind: str, name: str):
        if kind not in ("snapshots", "logs") or not _SAFE_NAME.match(name):
            raise HTTPException(404)
        path = engine.data_dir / kind / name
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, filename=name)

    def _local_only(request: Request) -> None:
        if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
            raise HTTPException(403, "Only available on the computer running the profiler.")

    @app.post("/api/open-folder/{kind}")
    async def open_folder(kind: str, request: Request):
        """Show the snapshots/logs/quick-save folder in Explorer/Finder. Only for a page on this computer."""
        if kind not in ("snapshots", "logs", "saves"):
            raise HTTPException(404)
        _local_only(request)
        folder = Path(_qs_prefs()["folder"]) if kind == "saves" else (engine.data_dir / kind).resolve()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HTTPException(400, f"Cannot open {folder}: {e.strerror or e}")
        _reveal(folder)
        return {"ok": True, "folder": str(folder)}

    # ---------------------------------------------------------- quick save

    def _qs_prefs() -> dict:
        saved = prefs.load().get("quicksave") or {}
        default_folder = str((engine.data_dir / "saves").resolve())
        out = {
            "folder": saved.get("folder") or default_folder,
            "name": saved.get("name") or quicksave.DEFAULT_NAME,
            "note": saved.get("note") or "",
            "raw": bool(saved.get("raw", False)),
            "default_folder": default_folder,
        }
        return out

    @app.get("/api/quicksave")
    async def quicksave_get(request: Request):
        q = _qs_prefs()
        q["can_choose_folder"] = request.client is not None and request.client.host in ("127.0.0.1", "::1")
        return q

    @app.post("/api/quicksave/prefs")
    async def quicksave_prefs(req: Request):
        body = await req.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object.")
        patch = {}
        if "folder" in body:
            _local_only(req)  # a page on another computer must not pick where files are written
            try:
                patch["folder"] = str(quicksave.clean_folder(str(body["folder"])))
            except quicksave.QuickSaveError as e:
                raise HTTPException(400, str(e))
        if "name" in body:
            try:
                patch["name"] = quicksave.clean_name(str(body["name"]))
            except quicksave.QuickSaveError as e:
                raise HTTPException(400, str(e))
        if "note" in body:
            patch["note"] = str(body["note"])[:500]
        if "raw" in body:
            patch["raw"] = bool(body["raw"])
        if patch:
            prefs.update("quicksave", patch)
        return await quicksave_get(req)

    @app.post("/api/quicksave/pick-folder")
    async def quicksave_pick(req: Request):
        _local_only(req)
        try:
            chosen = await asyncio.to_thread(folderpicker.pick_folder, _qs_prefs()["folder"])
        except folderpicker.PickerBusy as e:
            raise HTTPException(409, str(e))
        except folderpicker.PickerUnavailable as e:
            raise HTTPException(501, str(e))
        if chosen:
            prefs.update("quicksave", {"folder": chosen})
        out = await quicksave_get(req)
        out["cancelled"] = not chosen
        return out

    @app.post("/api/quicksave")
    async def quicksave_save(req: Request):
        body = await req.json() if await req.body() else {}
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object.")
        q = _qs_prefs()
        name = str(body.get("name", q["name"]))
        note = str(body.get("note", q["note"]))
        raw = bool(body.get("raw", q["raw"]))
        extra = {"view": body.get("view"), "m2": body.get("m2")}
        try:
            folder = quicksave.clean_folder(q["folder"])
            out = await asyncio.to_thread(engine.quicksave, folder, name, note, raw,
                                          bool(body.get("overwrite")), extra)
        except quicksave.NameExists as e:
            return JSONResponse(status_code=409, content={
                "detail": str(e), "exists": True, "name": e.name, "suggestion": e.suggestion,
                "folder": str(e.folder), "existing": e.existing})
        except quicksave.QuickSaveError as e:
            raise HTTPException(400, str(e))
        except OSError as e:
            raise HTTPException(400, f"Could not save: {e.strerror or e}"
                                     + (f" ({e.filename})" if getattr(e, "filename", None) else ""))
        prefs.update("quicksave", {"name": out["next_name"], "note": note, "raw": raw})
        return out

    @app.post("/api/log/start")
    async def log_start():
        return {"name": engine.start_log(), "folder": str((engine.data_dir / "logs").resolve())}

    @app.post("/api/log/stop")
    async def log_stop():
        return {"name": engine.stop_log(), "folder": str((engine.data_dir / "logs").resolve())}

    @app.post("/api/load")
    async def load(req: Request, name: str = "image"):
        data = await req.body()
        if not data:
            raise HTTPException(400, "Empty file.")
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "File too large.")
        await asyncio.to_thread(engine.load_file, data, name)
        return engine.state()

    @app.post("/api/source/live")
    async def source_live():
        await asyncio.to_thread(engine.connect_live)
        return engine.state()

    @app.post("/api/source/sim")
    async def source_sim():
        await asyncio.to_thread(engine.use_simulator)
        return engine.state()

    @app.post("/api/sim")
    async def sim(req: Request):
        cam = engine.camera
        if not isinstance(cam, SimulatedCamera):
            raise HTTPException(400, "The simulator is not active.")
        cam.set_params(await req.json())
        return engine.state()

    @app.post("/api/caustic/fit")
    async def caustic(req: Request):
        body = await req.json()
        pts = body.get("points", [])
        lam = float(body.get("wavelength_nm", engine.settings["wavelength_nm"]))
        z = [p.get("z") for p in pts]
        out = {}
        for axis in ("x", "y"):
            w = [p.get("w" + axis) for p in pts]
            pairs = [(a, b) for a, b in zip(z, w) if a is not None and b is not None]
            out[axis] = analysis.fit_caustic([a for a, _ in pairs], [b for _, b in pairs], lam) if len(pairs) >= 3 else None
        return out

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        origin = sock.headers.get("origin")
        host = sock.headers.get("host", "")
        if (origin is not None and urlsplit(origin).netloc != host) or \
                (not lan and _host_name(host).lower() not in _LOCAL_HOSTS):
            await sock.close(code=1008)  # the stream is only for this profiler's own page
            return
        await sock.accept()
        app.state.ws_clients += 1
        app.state.ws_ever = True
        app.state.ws_changed = time.monotonic()

        async def _until_closed():
            # The page never sends; reading is how a closed window is noticed even
            # when no frames go out (display paused, camera stalled).
            while (await sock.receive())["type"] != "websocket.disconnect":
                pass

        closed = asyncio.create_task(_until_closed())
        last = -1
        try:
            while not closed.done():
                seq = engine.packet_seq
                packet = engine.packet
                if seq != last and packet is not None:
                    last = seq
                    await sock.send_bytes(packet)
                else:
                    await asyncio.sleep(0.008)
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception:
            pass
        finally:
            closed.cancel()
            app.state.ws_clients -= 1
            app.state.ws_changed = time.monotonic()

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def _no_cache(request, call_next):
        resp = await call_next(request)
        if request.url.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    return app


def _reveal(folder: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(folder))  # noqa: S606 - opens Explorer on our own data folder
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])
    else:
        subprocess.Popen(["xdg-open", str(folder)])


def build_arg_parser(prog: str = "beamprofiler") -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=prog, description="Live beam profiler for Thorlabs scientific cameras.")
    ap.add_argument("--camera", choices=["thorlabs", "sim"], default="thorlabs",
                    help="thorlabs: the real camera (Windows). sim: a simulated beam, no hardware needed.")
    ap.add_argument("--serial", default=None, help="Serial number of the camera to open (default: first found).")
    ap.add_argument("--dll-dir", default=None, help="Folder with the Thorlabs 64-bit native DLLs.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--lan", action="store_true",
                    help="Accept connections from other computers on the network (no password; use on a trusted lab network only).")
    ap.add_argument("--data-dir", default=None,
                    help=f"Where snapshots and logs are saved (default: {default_data_dir()}).")
    ap.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically.")
    return ap


def make_engine(args) -> Engine:
    """Create the engine for the parsed command line and try to connect the camera."""
    if args.camera == "sim":
        factory = SimulatedCamera
    else:
        def factory():
            from .cameras import ThorlabsCamera

            return ThorlabsCamera(serial=args.serial, dll_dir=args.dll_dir)

    engine = Engine(data_dir=args.data_dir or default_data_dir(), live_factory=factory)
    engine.launch_mode = args.camera
    if engine.connect_live():
        print(f"Camera: {engine.camera.name}")
    else:
        print(f"WARNING: camera not connected: {engine.cam_error}")
        print("The page will still open; you can retry, load an image file, or start the simulator from there.")
    return engine


def main(argv=None) -> None:
    args = build_arg_parser().parse_args(argv)
    engine = make_engine(args)

    host = "0.0.0.0" if args.lan else "127.0.0.1"
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Beam profiler running at {url}  (Ctrl+C to stop)")
    if args.lan:
        print(f"Other computers on the network can open http://<this-computer's-IP>:{args.port}/")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    import uvicorn

    uvicorn.run(create_app(engine, lan=args.lan), host=host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
