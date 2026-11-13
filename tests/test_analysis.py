"""Checks the analysis against synthetic beams with known parameters.

Run:  python -m pytest tests   (or: python tests/test_analysis.py)
"""

import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from beamprofiler import analysis  # noqa: E402


def make_beam(w=1440, h=1080, cx=700.0, cy=560.0, wu=120.0, wv=80.0, angle=0.0, amp=700.0, offset=6.0, noise=1.6, seed=0):
    """Elliptical Gaussian; wu, wv are 1/e^2 radii in px; angle in image coordinates (rad)."""
    rng = np.random.default_rng(seed)
    x = np.arange(w)[None, :] - cx
    y = np.arange(h)[:, None] - cy
    ct, st = math.cos(angle), math.sin(angle)
    u = x * ct + y * st
    v = -x * st + y * ct
    img = amp * np.exp(-2 * ((u / wu) ** 2 + (v / wv) ** 2)) + offset
    if noise:
        img = img + rng.normal(0, noise, img.shape)
    return img.astype(np.float32)


def analyse(img):
    base, noise = analysis.corner_baseline(img)
    sub = img - np.float32(base)
    return sub, analysis.d4sigma(sub), noise


def test_d4sigma_round_beam():
    for w_px in (29.0, 100.0, 200.0):  # 100 um, 345 um, 690 um radius on the Zelux
        sub, d4, _ = analyse(make_beam(wu=w_px, wv=w_px))
        assert d4 is not None and d4["converged"]
        assert abs(d4["cx"] - 700.0) < 0.3 and abs(d4["cy"] - 560.0) < 0.3
        assert abs(d4["dx"] / (2 * w_px) - 1) < 0.02, (w_px, d4["dx"])
        assert abs(d4["dy"] / (2 * w_px) - 1) < 0.02, (w_px, d4["dy"])


def test_d4sigma_small_beam_anywhere_on_full_frame():
    # Regression: whole-frame moments of a small beam are swamped by baseline
    # noise, so the first window must start near the beam, not at the full frame.
    for seed in range(8):
        rng = np.random.default_rng(seed)
        cx, cy = rng.uniform(200, 1240), rng.uniform(200, 880)
        w_px = rng.uniform(9, 30)  # 30-100 um radius
        sub, d4, _ = analyse(make_beam(wu=w_px, wv=w_px, cx=cx, cy=cy, seed=seed))
        assert d4 is not None, seed
        assert abs(d4["cx"] - cx) < 0.5 and abs(d4["cy"] - cy) < 0.5
        assert abs(d4["dx"] / (2 * w_px) - 1) < 0.03 and abs(d4["dy"] / (2 * w_px) - 1) < 0.03


def test_d4sigma_rotated_ellipse():
    ang = math.radians(30)
    sub, d4, _ = analyse(make_beam(wu=120, wv=80, angle=ang))
    assert abs(d4["dmaj"] / 240 - 1) < 0.02
    assert abs(d4["dmin"] / 160 - 1) < 0.02
    assert abs(d4["phi"] - ang) < math.radians(1.0)


def test_gauss1d_fit_on_slices():
    sub, d4, _ = analyse(make_beam(wu=150, wv=90))
    vx, vy = analysis.line_profiles(sub, d4["cx"], d4["cy"], "slice", 2)
    fx = analysis.fit_gauss1d(np.arange(vx.size), vx, x0=d4["cx"], w=d4["dx"] / 2)
    fy = analysis.fit_gauss1d(np.arange(vy.size), vy, x0=d4["cy"], w=d4["dy"] / 2)
    assert abs(fx["w"] / 150 - 1) < 0.01 and abs(fy["w"] / 90 - 1) < 0.01
    assert fx["r2"] > 0.99
    sx, sy = analysis.line_profiles(sub, d4["cx"], d4["cy"], "sum")
    gx = analysis.fit_gauss1d(np.arange(sx.size), sx)
    assert abs(gx["w"] / 150 - 1) < 0.01


def test_gauss2d_fit():
    ang = math.radians(-20)
    sub, d4, _ = analyse(make_beam(wu=140, wv=70, angle=ang))
    x0, y0, w, h = d4["window"]
    g = analysis.fit_gauss2d(
        sub[y0:y0 + h, x0:x0 + w],
        {"cx": d4["cx"] - x0, "cy": d4["cy"] - y0, "dmaj": d4["dmaj"], "dmin": d4["dmin"], "phi": d4["phi"]},
    )
    assert g is not None
    assert abs(g["wmaj"] / 140 - 1) < 0.01 and abs(g["wmin"] / 70 - 1) < 0.01
    assert abs(g["phi"] - ang) < math.radians(0.5)
    assert abs(g["cx"] + x0 - 700) < 0.3 and abs(g["cy"] + y0 - 560) < 0.3


def test_largest_beam_is_clipped_but_fit_holds():
    # 1000 um radius = 290 px: the sensor is only 3.7 w tall, so D4sigma reads low.
    sub, d4, _ = analyse(make_beam(wu=290, wv=290, cx=720, cy=540))
    assert d4["clipped"]
    vx, vy = analysis.line_profiles(sub, d4["cx"], d4["cy"], "slice", 2)
    fy = analysis.fit_gauss1d(np.arange(vy.size), vy)
    assert abs(fy["w"] / 290 - 1) < 0.02


def test_no_beam():
    rng = np.random.default_rng(3)
    img = (6 + rng.normal(0, 1.6, (400, 500))).astype(np.float32)
    base, noise = analysis.corner_baseline(img)
    sub = img - np.float32(base)
    assert analysis.block_mean(sub, 2).max() < max(5 * noise, 4.0)


def test_caustic_m2():
    lam_nm, w0, m2, z0 = 780.0, 60.0, 1.3, 42.0
    theta = m2 * (lam_nm * 1e-3) / (math.pi * w0) * 1e3  # mrad
    z = np.linspace(0, 90, 12)
    w = np.sqrt(w0**2 + (theta * (z - z0)) ** 2)
    fit = analysis.fit_caustic(z, w, lam_nm)
    assert abs(fit["m2"] - m2) < 1e-3
    assert abs(fit["w0_um"] - w0) < 1e-2 and abs(fit["z0_mm"] - z0) < 1e-2


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  ", name)
