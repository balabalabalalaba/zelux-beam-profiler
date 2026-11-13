"""Checks quick save against the simulated beam, whose true radii and angle are known.

Run:  python -m pytest tests   (or: python tests/test_quicksave.py)
"""

import csv
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["BEAMPROFILER_HOME"] = tempfile.mkdtemp(prefix="bp-qs-home-")

from beamprofiler import quicksave  # noqa: E402
from beamprofiler.cameras import SimulatedCamera  # noqa: E402
from beamprofiler.engine import Engine  # noqa: E402

_ENGINE = None


def engine():
    """One running simulator engine for all tests, with a measured beam."""
    global _ENGINE
    if _ENGINE is None:
        e = Engine(data_dir=tempfile.mkdtemp(prefix="bp-qs-data-"), live_factory=SimulatedCamera)
        assert e.connect_live()
        e.camera.set_params({"jitter_um": 0.0, "drift_um": 0.0})
        e.update_settings({"fit2d": True, "averaging": 4})
        e.start()
        t = time.monotonic()
        while time.monotonic() - t < 20:
            last = e._last
            if last and last["header"]["res"] and last["header"]["status"]["avg_n"] >= 4 \
                    and last["header"]["res"].get("g2"):
                break
            time.sleep(0.05)
        _ENGINE = e
    return _ENGINE


def test_names():
    assert quicksave.bump("beam_009") == "beam_010"
    assert quicksave.bump("beam_999") == "beam_1000"
    assert quicksave.bump("waist") == "waist_2"
    assert quicksave.bump("z12") == "z13"
    assert quicksave.clean_name("  run 1.json ") == "run 1"
    for bad in ("", "a/b", "a\\b", "x:y", "nul", "COM1.txt", "end.", 'q"', "x" * 121):
        try:
            quicksave.clean_name(bad)
        except quicksave.QuickSaveError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_save_contents_match_simulated_beam():
    e = engine()
    folder = Path(tempfile.mkdtemp(prefix="bp-qs-")) / "new sub folder"
    out = e.quicksave(folder, "beam_001", note="z = 25 mm, µ test", extra={"view": {"unit": "um"}})
    assert out["files"] == ["beam_001.json"] and out["next_name"] == "beam_002"
    rec = json.loads((folder / "beam_001.json").read_text(encoding="utf-8"))
    assert rec["format"] == quicksave.FORMAT and rec["beam_found"]
    assert rec["note"] == "z = 25 mm, µ test" and rec["view"] == {"unit": "um"}
    s = rec["summary"]
    truth = e.camera.params  # wx 420 µm, wy 300 µm radii at 25°
    assert abs(s["d4sigma_radius_major_um"] / truth["wx_um"] - 1) < 0.03, s
    assert abs(s["d4sigma_radius_minor_um"] / truth["wy_um"] - 1) < 0.03, s
    assert abs(s["angle_deg"] - truth["angle_deg"]) < 1.5, s
    assert abs(s["gauss2d_radius_major_um"] / truth["wx_um"] - 1) < 0.03, s
    assert abs(s["centroid_x_um"] / (truth["x0_px"] * 3.45) - 1) < 0.01, s
    # full-resolution profiles, in µm, with fits
    px = rec["profiles"]["x"]
    roi = rec["roi_px"]
    assert len(px["position_um"]) == roi["w"] == len(px["signal_counts"])
    assert np.allclose(np.diff(px["position_um"]), 3.45, atol=1e-3)
    assert px["fit"] and px["fit"]["radius_um"] > 0
    assert rec["settings"]["fit2d"] is True and rec["status"]["averaged_frames"] >= 1
    # summary table
    rows = list(csv.reader(open(folder / quicksave.SUMMARY_NAME, encoding="utf-8-sig")))
    assert rows[0] == quicksave.SUMMARY_COLUMNS and len(rows) == 2
    row = dict(zip(rows[0], rows[1]))
    assert row["name"] == "beam_001" and row["note"].startswith("z = 25 mm")
    assert abs(float(row["d4sigma_radius_major_um"]) - s["d4sigma_radius_major_um"]) < 1e-6


def test_existing_name_needs_overwrite_and_raw_frame():
    e = engine()
    folder = Path(tempfile.mkdtemp(prefix="bp-qs-"))
    e.quicksave(folder, "spot")
    try:
        e.quicksave(folder, "spot")
    except quicksave.NameExists as err:
        assert err.suggestion == "spot_2"
    else:
        raise AssertionError("an existing name was overwritten silently")
    out = e.quicksave(folder, "spot", overwrite=True, raw=True)
    assert out["files"] == ["spot.json", "spot.tif"]
    from PIL import Image

    with Image.open(folder / "spot.tif") as im:
        assert im.mode == "I;16" and im.info.get("compression") == "tiff_adobe_deflate"
        frame = np.array(im)
    assert frame.shape == (1080, 1440) and frame.dtype == np.uint16
    rec = json.loads((folder / "spot.json").read_text(encoding="utf-8"))
    assert rec["raw_frame"]["file"] == "spot.tif" and rec["raw_frame"]["averaged_frames"] >= 1
    assert (folder / "spot.tif").stat().st_size < 3_000_000  # compressed: smaller than an uncompressed TIFF
    rows = list(csv.reader(open(folder / quicksave.SUMMARY_NAME, encoding="utf-8-sig")))
    assert len(rows) == 3  # header + two saves
    assert out["next_name"] == "spot_2"


def test_older_summary_with_other_columns_is_not_mixed():
    e = engine()
    folder = Path(tempfile.mkdtemp(prefix="bp-qs-"))
    (folder / quicksave.SUMMARY_NAME).write_text("a,b\n1,2\n", encoding="utf-8")
    out = e.quicksave(folder, "x1")
    assert out["summary_file"] == "quicksave_summary_2.csv"
    assert (folder / quicksave.SUMMARY_NAME).read_text(encoding="utf-8") == "a,b\n1,2\n"


def test_tiff_frame_is_exact_for_one_frame_and_rounded_for_a_mean():
    from PIL import Image

    rng = np.random.default_rng(3)
    one = rng.integers(0, 1024, size=(60, 80)).astype(np.float32)
    data, info = quicksave.frame_to_tiff_bytes(one, 1, 10)
    back = np.array(Image.open(__import__("io").BytesIO(data)))
    assert np.array_equal(back, one.astype(np.uint16)) and info["exact"]
    mean = (one + rng.integers(0, 1024, size=one.shape)) / 2.0
    data, info = quicksave.frame_to_tiff_bytes(mean, 2, 10)
    back = np.array(Image.open(__import__("io").BytesIO(data))).astype(np.float64)
    assert np.max(np.abs(back - mean)) <= 0.5 and info["averaged_frames"] == 2


def test_locked_summary_keeps_the_save_and_the_row():
    e = engine()
    folder = Path(tempfile.mkdtemp(prefix="bp-qs-"))
    e.quicksave(folder, "a_001")
    summary = folder / quicksave.SUMMARY_NAME
    summary.chmod(0o444)  # like Excel holding the file open on Windows
    try:
        out = e.quicksave(folder, "a_002")
        assert out["summary_error"] and out["summary_file"] is None, out
        assert (folder / "a_002.json").exists() and out["next_name"] == "a_003"
    finally:
        summary.chmod(0o644)
    out = e.quicksave(folder, "a_003")
    assert out["summary_error"] is None
    names = [r["name"] for r in csv.DictReader(open(summary, encoding="utf-8-sig"))]
    assert names == ["a_001", "a_002", "a_003"], names  # the waiting row was added first
    assert not (folder / quicksave.PENDING_NAME).exists()


def test_overwrite_without_raw_removes_the_old_frame():
    e = engine()
    folder = Path(tempfile.mkdtemp(prefix="bp-qs-"))
    e.quicksave(folder, "spot", raw=True)
    try:
        e.quicksave(folder, "spot")
    except quicksave.NameExists as err:
        assert err.existing == ["spot.json", "spot.tif"], err.existing
    e.quicksave(folder, "spot", overwrite=True, raw=False)
    assert (folder / "spot.json").exists() and not (folder / "spot.tif").exists()


def test_loaded_file_has_no_exposure_and_source_is_labelled():
    from beamprofiler import quicksave as q

    e = engine()
    last = dict(e._last)
    header = json.loads(json.dumps(last["header"]))
    header["cam"]["kind"], header["cam"]["name"] = "file", "old.npy"
    rec = q.build_record({**last, "header": header}, "f1", "")
    assert rec["camera"]["exposure_ms"] is None and rec["camera"]["gain_db"] is None
    row = dict(zip(q.SUMMARY_COLUMNS, q.summary_row(rec, ["f1.json"])))
    assert row["source"] == "file: old.npy" and row["exposure_ms"] == "", row
    sim_row = dict(zip(q.SUMMARY_COLUMNS, q.summary_row(q.build_record(last, "s1", ""), ["s1.json"])))
    assert sim_row["source"].startswith("sim:"), sim_row


def test_server_refuses_other_sites():
    import urllib.error
    import urllib.request

    from beamprofiler.desktop import ServerThread, free_port
    from beamprofiler.server import create_app

    e = Engine(data_dir=tempfile.mkdtemp(prefix="bp-qs-data-"), live_factory=SimulatedCamera)
    assert e.connect_live()
    port = free_port()
    srv = ServerThread(create_app(e), "127.0.0.1", port)  # its lifespan starts and stops this engine
    assert srv.start(), srv.error
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(path, headers, body=b'{"note": "x"}'):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method="POST",
                                     headers={"Content-Type": "application/json", **headers})
        try:
            with opener.open(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as err:
            return err.code

    try:
        for _ in range(20):  # a refusal must reach the client every time (it once raced on Windows)
            assert call("/api/quicksave/prefs", {"Origin": "https://evil.example"}) == 403
            assert call("/api/quicksave/prefs", {"Origin": "null"}) == 403
            assert call("/api/quicksave/prefs", {"Host": "evil.example:80"}) == 403
        assert call("/api/quicksave/prefs", {"Origin": f"http://127.0.0.1:{port}"}) == 200
        assert call("/api/quicksave/prefs", {}) == 200  # no Origin: not a browser page from elsewhere
    finally:
        srv.server.should_exit = True
        srv.thread.join(5)


if __name__ == "__main__":
    try:
        for name, fn in list(globals().items()):
            if name.startswith("test_"):
                fn()
                print("ok  ", name)
    finally:
        if _ENGINE is not None:
            _ENGINE.shutdown()
