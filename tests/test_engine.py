"""Checks of the engine's measurement switch and auto exposure (no camera needed).

Run:  python -m pytest tests   (or: python tests/test_engine.py)
"""

import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["BEAMPROFILER_HOME"] = tempfile.mkdtemp(prefix="bp-eng-home-")

from beamprofiler.cameras import FileCamera, SimulatedCamera  # noqa: E402
from beamprofiler.engine import AE_SEARCH_MAX_MS, Engine  # noqa: E402


def _frame(beam: bool, seed=0):
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:1080, 0:1440].astype(np.float32)
    img = 6 + rng.normal(0, 1.6, x.shape).astype(np.float32)
    if beam:
        img += 700 * np.exp(-2 * ((x - 700) ** 2 + (y - 540) ** 2) / 150.0 ** 2)
    return np.clip(img, 0, 1023).astype(np.float32)


def test_measure_off_skips_analysis_but_keeps_the_image():
    e = Engine(data_dir=tempfile.mkdtemp())
    img = _frame(True)
    cam = FileCamera(np.round(img).astype(np.uint16), 10, "beam")
    s = dict(e.settings, fit2d=True)
    on, _ = e._process(cam, img, img.astype(np.uint16), 1, 1, s, live=False)
    assert on["res"] and on["res"]["g2"] and on["prof"]
    off, image = e._process(cam, img, img.astype(np.uint16), 1, 1, dict(s, measure=False), live=False)
    assert off["res"] is None and off["prof"] is None
    assert off["status"]["measure"] is False and off["status"]["msg"] == "Measurement is off"
    assert len(image) == off["img"]["w"] * off["img"]["h"] > 0
    assert abs(off["status"]["peak_pct"] - on["status"]["peak_pct"]) < 0.5  # level meter still works
    t = time.perf_counter()
    for _ in range(5):
        e._process(cam, img, img.astype(np.uint16), 1, 1, s, live=False)
    t_on = time.perf_counter() - t
    t = time.perf_counter()
    for _ in range(5):
        e._process(cam, img, img.astype(np.uint16), 1, 1, dict(s, measure=False), live=False)
    assert time.perf_counter() - t < t_on


def test_measure_is_a_validated_setting():
    e = Engine(data_dir=tempfile.mkdtemp())
    assert e.settings["measure"] is True
    assert e.update_settings({"measure": False})["measure"] is False
    assert e.update_settings({"measure": 0})["measure"] is False
    assert e.update_settings({"measure": True})["measure"] is True


class _Cam:
    kind = "thorlabs"
    exposure_range_ms = (0.04, 20000.0)

    def __init__(self, exp):
        self.exp = exp

    def set_exposure_ms(self, ms):
        self.exp = ms
        return ms

    def set_gain_db(self, db):
        return db


def _ae(exp, peak_frac, sat_px=0, beam_seen=True):
    e = Engine(data_dir=tempfile.mkdtemp())
    cam = _Cam(exp)
    e.camera = cam
    e.settings = dict(e.settings, exposure_ms=exp, auto_exposure=True)
    e._ae_wait_until = 0
    sat = 1023
    e._auto_expose(cam, e.settings, int(peak_frac * sat), sat_px, sat, 10, beam_seen)
    return e.settings["exposure_ms"]


def test_auto_exposure_does_not_chase_noise_when_the_beam_is_lost():
    assert _ae(5.0, 0.015, beam_seen=False) == AE_SEARCH_MAX_MS  # was 2000 ms
    assert _ae(200.0, 0.015, beam_seen=False) == 200.0  # never raised further without a beam
    assert _ae(5.0, 0.30, beam_seen=True) > 10.0  # a weak but real beam is still brought up


def test_auto_exposure_comes_down_fast_when_saturated():
    assert _ae(50.0, 1.0, sat_px=30000) == 12.5
    assert _ae(50.0, 1.0, sat_px=10) == 25.0


def _dark_with_hot_pixels(seed=0):
    """A real sensor in the dark: baseline ~1 count, noise ~0.25, two hot pixels."""
    rng = np.random.default_rng(seed)
    img = 1.0 + rng.normal(0, 0.25, (1080, 1440)).astype(np.float32)
    img[300, 200] = 20.0
    img[800, 1000] = 15.0
    return np.clip(np.round(img), 0, 1023).astype(np.float32)


def test_hot_pixels_are_not_a_beam():
    for measure in (True, False):
        for n_avg in (1, 4):
            e = Engine(data_dir=tempfile.mkdtemp())
            cam = _Cam(5.0)
            cam.width, cam.height, cam.bit_depth = 1440, 1080, 10
            cam.info = lambda: {"kind": "thorlabs", "name": "test", "width": 1440, "height": 1080, "bits": 10,
                                "pitch_um": 3.45, "exp_range": [0.04, 20000.0], "gain_range": [0, 0]}
            e.camera = cam
            e.settings = dict(e.settings, exposure_ms=5.0, auto_exposure=True, measure=measure)
            frames = [_dark_with_hot_pixels(i) for i in range(n_avg)]
            for fid in range(1, 25):  # many auto-exposure steps
                frames = frames[1:] + [_dark_with_hot_pixels(fid + 10)]
                avg = np.mean(frames, axis=0).astype(np.float32)
                e._ae_wait_until = 0
                h, _ = e._process(cam, avg, frames[-1].astype(np.uint16), n_avg, fid, dict(e.settings), live=True)
                assert h["res"] is None, (measure, n_avg, "hot pixels measured as a beam")
            assert e.settings["exposure_ms"] <= AE_SEARCH_MAX_MS, (measure, n_avg, e.settings["exposure_ms"])


def test_exposure_returns_to_search_level_after_a_glimpse():
    e = Engine(data_dir=tempfile.mkdtemp())
    cam = _Cam(600.0)
    e.camera = cam
    e.settings = dict(e.settings, exposure_ms=600.0, auto_exposure=True)
    for fid in range(5):
        e._ae_wait_until = 0
        e._auto_expose(cam, e.settings, 15, 0, 1023, fid, False)
    assert e.settings["exposure_ms"] == AE_SEARCH_MAX_MS
    # a weak beam that really needs a long exposure (still seen) is left alone
    e.settings = dict(e.settings, exposure_ms=600.0)
    for fid in range(5):
        e._ae_wait_until = 0
        e._auto_expose(cam, e.settings, int(0.75 * 1023), 0, 1023, fid, True)
    assert e.settings["exposure_ms"] == 600.0


def test_auto_roi_falls_back_to_full_frame_when_measurement_is_off():
    e = Engine(data_dir=tempfile.mkdtemp())
    img = _frame(True)
    cam = FileCamera(np.round(img).astype(np.uint16), 10, "beam")
    s = dict(e.settings, auto_roi=True)
    e._process(cam, img, img.astype(np.uint16), 1, 1, s, live=False)
    assert e._auto_roi is not None
    e._process(cam, img, img.astype(np.uint16), 1, 2, dict(s, measure=False), live=False)
    assert e._auto_roi is None


def test_simulated_beam_lost_and_found_with_auto_exposure():
    e = Engine(data_dir=tempfile.mkdtemp(), live_factory=SimulatedCamera)
    assert e.connect_live()
    e.start()
    try:
        time.sleep(0.8)
        e.update_settings({"auto_exposure": True})
        time.sleep(1.0)
        e.camera.set_params({"x0_px": 5000.0})  # beam leaves the sensor
        time.sleep(2.0)
        assert e.settings["exposure_ms"] <= AE_SEARCH_MAX_MS
        e.camera.set_params({"x0_px": 700.0})  # and comes back
        t = time.monotonic()
        while time.monotonic() - t < 5:
            st = e._last["header"]["status"]
            if st["valid"] and not st["sat_px"] and 40 <= st["peak_pct"] <= 95:
                break
            time.sleep(0.05)
        assert time.monotonic() - t < 3, "auto exposure took too long to recover"
    finally:
        e.shutdown()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  ", name)
