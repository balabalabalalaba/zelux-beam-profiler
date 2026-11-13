"""Beam analysis: baseline, ISO 11146 second moments (D4-sigma), Gaussian fits, M^2.

All lengths here are in pixels and angles in radians in image coordinates
(x to the right, y downwards). Pixel i is centred on coordinate i.
Gaussian widths `w` are 1/e^2 intensity radii:  I = A * exp(-2 r^2 / w^2).
For an ideal Gaussian the D4-sigma diameter equals 2 w.
"""

from __future__ import annotations

import math

import numpy as np
from scipy.optimize import curve_fit, least_squares


# ------------------------------------------------------------------ baseline


def corner_baseline(img: np.ndarray, frac: float = 0.04, min_px: int = 4) -> tuple[float, float]:
    """Estimate offset and noise from the four corners of the image.

    Uses the median over the corners so that one corner touched by the beam
    does not spoil the estimate.
    """
    h, w = img.shape
    ch = max(1, min(max(min_px, int(h * frac)), h // 2))
    cw = max(1, min(max(min_px, int(w * frac)), w // 2))
    patches = (img[:ch, :cw], img[:ch, -cw:], img[-ch:, :cw], img[-ch:, -cw:])
    means = [float(p.mean()) for p in patches]
    stds = [float(p.std()) for p in patches]
    return float(np.median(means)), float(np.median(stds))


def block_mean(img: np.ndarray, s: int) -> np.ndarray:
    if s <= 1:
        return img
    h, w = img.shape
    h2, w2 = (h // s) * s, (w // s) * s
    if h2 == 0 or w2 == 0:
        return img
    return img[:h2, :w2].reshape(h2 // s, s, w2 // s, s).mean(axis=(1, 3))


# ------------------------------------------------------------ second moments


def _moments(img: np.ndarray, x0: int, x1: int, y0: int, y1: int):
    sub = img[y0:y1, x0:x1]
    px = sub.sum(axis=0, dtype=np.float64)
    py = sub.sum(axis=1, dtype=np.float64)
    total = float(px.sum())
    if not total > 0:
        return None
    xs = np.arange(x0, x1, dtype=np.float64)
    ys = np.arange(y0, y1, dtype=np.float64)
    cx = float((px * xs).sum() / total)
    cy = float((py * ys).sum() / total)
    dx = xs - cx
    dy = ys - cy
    sxx = float((px * dx * dx).sum() / total)
    syy = float((py * dy * dy).sum() / total)
    sxy = float(dy.astype(sub.dtype) @ (sub @ dx.astype(sub.dtype))) / total
    return cx, cy, sxx, syy, sxy, total


def _initial_window(img: np.ndarray, k: float = 3.0):
    """Rough starting window around the beam.

    Second moments over the whole frame are unusable for a small beam: the
    r^2 weighting lets a tiny baseline error far from the beam dominate (the
    variance can even come out negative). So start from the pixels above the
    1/e^2 level of a coarsely binned copy, then let the ISO iteration refine.
    """
    h, w = img.shape
    s = max(1, min(h, w) // 128)
    b = block_mean(img, s)
    peak = float(b.max())
    if not peak > 0:
        return None
    bm = np.where(b > 0.135 * peak, b, 0.0)
    m = _moments(bm, 0, bm.shape[1], 0, bm.shape[0])
    if m is None:
        return None
    cx = (m[0] + 0.5) * s - 0.5
    cy = (m[1] + 0.5) * s - 0.5
    hx = max(k * 2.0 * math.sqrt(max(m[2], 0.0)) * s, 3.0 * s, 4.0)
    hy = max(k * 2.0 * math.sqrt(max(m[3], 0.0)) * s, 3.0 * s, 4.0)
    return (
        max(0, int(math.floor(cx - hx))),
        min(w, int(math.ceil(cx + hx)) + 1),
        max(0, int(math.floor(cy - hy))),
        min(h, int(math.ceil(cy + hy)) + 1),
    )


def d4sigma(img: np.ndarray, max_iter: int = 30, k: float = 3.0) -> dict | None:
    """ISO 11146 beam widths with the iterative integration window.

    The window is repeatedly re-centred on the centroid with a size of
    `k` times the D4-sigma width until it stops changing. `img` must be
    baseline-subtracted; negative noise values must be kept (not clipped).
    Returns None if no beam is found.
    """
    h, w = img.shape
    win = _initial_window(img)
    if win is None:
        return None
    seen = {win}
    m = None
    converged = False
    for _ in range(max_iter):
        m = _moments(img, *win)
        if m is None:
            return None
        cx, cy, sxx, syy, _sxy, _tot = m
        if not (sxx > 0 and syy > 0):
            return None
        hx = k * 2.0 * math.sqrt(sxx)
        hy = k * 2.0 * math.sqrt(syy)
        new = (
            max(0, int(math.floor(cx - hx))),
            min(w, int(math.ceil(cx + hx)) + 1),
            max(0, int(math.floor(cy - hy))),
            min(h, int(math.ceil(cy + hy)) + 1),
        )
        if new[1] - new[0] < 3 or new[3] - new[2] < 3:
            return None
        if new == win:
            converged = True
            break
        if new in seen:  # two windows alternating: accept
            converged = True
            win = new
            m = _moments(img, *win)
            break
        seen.add(new)
        win = new
    if m is None:
        return None
    cx, cy, sxx, syy, sxy, total = m
    if not (sxx > 0 and syy > 0):
        return None
    t = sxx + syy
    diff = sxx - syy
    root = math.sqrt(diff * diff + 4.0 * sxy * sxy)
    dmaj = 2.0 * math.sqrt(2.0) * math.sqrt(t + root)
    dmin = 2.0 * math.sqrt(2.0) * math.sqrt(max(t - root, 0.0))
    phi = 0.5 * math.atan2(2.0 * sxy, diff)  # major axis, image coordinates
    x0, x1, y0, y1 = win
    return {
        "cx": cx,
        "cy": cy,
        "dx": 4.0 * math.sqrt(sxx),
        "dy": 4.0 * math.sqrt(syy),
        "dmaj": dmaj,
        "dmin": dmin,
        "phi": phi,
        "total": total,
        "window": (x0, y0, x1 - x0, y1 - y0),
        "clipped": bool(x0 == 0 or y0 == 0 or x1 == w or y1 == h),
        "converged": converged,
    }


# -------------------------------------------------------------- Gaussian fits


def _gauss1d(x, a, x0, w, c):
    return c + a * np.exp(-2.0 * ((x - x0) / w) ** 2)


def _gauss1d_jac(x, a, x0, w, c):
    z = (x - x0) / w
    e = np.exp(-2.0 * z * z)
    return np.stack([e, a * e * 4.0 * z / w, a * e * 4.0 * z * z / w, np.ones_like(x)], axis=1)


def fit_gauss1d(x: np.ndarray, y: np.ndarray, x0: float | None = None, w: float | None = None) -> dict | None:
    """Fit c + a*exp(-2 (x-x0)^2 / w^2). Returns None when the fit fails."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    if x.size < 6:
        return None
    c0 = float(np.percentile(y, 10))
    i_max = int(np.argmax(y))
    a0 = float(y[i_max] - c0)
    if not a0 > 0:
        return None
    step = float(abs(x[1] - x[0]))
    if x0 is None:
        x0 = float(x[i_max])
    if w is None or not w > 0:
        above = int(np.count_nonzero(y > c0 + 0.5 * a0))
        w = max(above * step / 1.1774, 2.0 * step)
    try:
        p, _ = curve_fit(_gauss1d, x, y, p0=[a0, x0, w, c0], jac=_gauss1d_jac, maxfev=200)
    except Exception:
        return None
    a, xc, wf, c = (float(v) for v in p)
    wf = abs(wf)
    span = float(x[-1] - x[0])
    if not (np.isfinite(p).all() and a > 0 and wf > 0.5 * step and wf < 20 * span):
        return None
    res = y - _gauss1d(x, *p)
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float((res**2).sum()) / ss_tot if ss_tot > 0 else 0.0
    return {"a": a, "x0": xc, "w": wf, "c": c, "r2": r2}


def fit_gauss2d(img: np.ndarray, init: dict, max_side: int = 96) -> dict | None:
    """Fit a rotated elliptical Gaussian to `img`.

    `init` needs cx, cy, dmaj, dmin, phi (from d4sigma). The image is binned
    so its longer side is at most `max_side` samples, which keeps the fit fast.
    """
    h, w = img.shape
    s = max(1, int(math.ceil(max(h, w) / max_side)))
    b = block_mean(img, s).astype(np.float64)
    hb, wb = b.shape
    if hb < 4 or wb < 4:
        return None
    X = ((np.arange(wb) + 0.5) * s - 0.5)[None, :]
    Y = ((np.arange(hb) + 0.5) * s - 0.5)[:, None]
    a0 = float(b.max())
    if not a0 > 0:
        return None
    p0 = [
        a0,
        init["cx"],
        init["cy"],
        max(init["dmaj"] / 2.0, s),
        max(init["dmin"] / 2.0, s),
        init["phi"],
        0.0,
    ]

    def resid(p):
        a, x0, y0, wu, wv, th, c = p
        ct, st = math.cos(th), math.sin(th)
        dx = X - x0
        dy = Y - y0
        u = dx * ct + dy * st
        v = -dx * st + dy * ct
        return (c + a * np.exp(-2.0 * ((u / wu) ** 2 + (v / wv) ** 2)) - b).ravel()

    try:
        sol = least_squares(resid, p0, method="lm", max_nfev=400, x_scale=[a0, 1, 1, p0[3], p0[4], 0.1, max(a0 * 0.01, 1e-6)])
    except Exception:
        return None
    a, x0, y0, wu, wv, th, c = (float(v) for v in sol.x)
    wu, wv = abs(wu), abs(wv)
    if not (np.isfinite(sol.x).all() and a > 0 and wu > 0 and wv > 0):
        return None
    if wu > 20 * max(h, w) or wv > 20 * max(h, w):
        return None
    if wv > wu:
        wu, wv = wv, wu
        th += math.pi / 2.0
    th = (th + math.pi / 2.0) % math.pi - math.pi / 2.0
    ss_tot = float(((b - b.mean()) ** 2).sum())
    r2 = 1.0 - float((sol.fun**2).sum()) / ss_tot if ss_tot > 0 else 0.0
    return {"a": a, "cx": x0, "cy": y0, "wmaj": wu, "wmin": wv, "phi": th, "c": c, "r2": r2}


# ---------------------------------------------------------------- profiles


def line_profiles(img: np.ndarray, cx: float, cy: float, mode: str = "slice", half: int = 2):
    """Return (x_profile, y_profile) along the image axes.

    mode "slice": mean of 2*half+1 lines through (cx, cy).
    mode "sum":   integrated over the whole image (marginal distribution).
    """
    h, w = img.shape
    if mode == "sum":
        return img.sum(axis=0, dtype=np.float64), img.sum(axis=1, dtype=np.float64)
    iy = int(round(min(max(cy, 0), h - 1)))
    ix = int(round(min(max(cx, 0), w - 1)))
    y0, y1 = max(0, iy - half), min(h, iy + half + 1)
    x0, x1 = max(0, ix - half), min(w, ix + half + 1)
    return (
        img[y0:y1, :].mean(axis=0, dtype=np.float64),
        img[:, x0:x1].mean(axis=1, dtype=np.float64),
    )


# ------------------------------------------------------------- M^2 / caustic


def fit_caustic(z_mm, w_um, wavelength_nm: float) -> dict | None:
    """Fit w(z) = sqrt(w0^2 + theta^2 (z - z0)^2) to beam radii along z.

    z in mm, w (1/e^2 radius or D4sigma/2) in micrometres. Returns the waist
    radius w0 (um), waist position z0 (mm), half divergence theta (mrad),
    Rayleigh range zR (mm) and M^2. Needs at least 3 points.
    """
    z = np.asarray(z_mm, dtype=np.float64)
    w = np.asarray(w_um, dtype=np.float64)
    ok = np.isfinite(z) & np.isfinite(w) & (w > 0)
    z, w = z[ok], w[ok]
    if z.size < 3 or np.ptp(z) <= 0:
        return None
    zm = float(z.mean())
    zc = z - zm
    try:
        c2, c1, c0 = np.polyfit(zc, w * w, 2)
    except Exception:
        return None
    if c2 > 0:
        z0 = -c1 / (2.0 * c2)
        w0sq = c0 - c1 * c1 / (4.0 * c2)
        theta = math.sqrt(c2)
        w0 = math.sqrt(w0sq) if w0sq > 0 else float(w.min()) * 0.5
    else:
        z0, w0, theta = float(zc[np.argmin(w)]), float(w.min()), float(np.ptp(w) / max(np.ptp(z), 1e-9))

    def model(zz, w0_, z0_, th_):
        return np.sqrt(w0_ * w0_ + th_ * th_ * (zz - z0_) ** 2)

    try:
        p, _ = curve_fit(model, zc, w, p0=[w0, z0, max(theta, 1e-6)], maxfev=2000)
        w0, z0, theta = abs(float(p[0])), float(p[1]), abs(float(p[2]))
    except Exception:
        if not c2 > 0:
            return None
    if not (w0 > 0 and theta > 0 and math.isfinite(w0) and math.isfinite(theta)):
        return None
    lam_um = wavelength_nm * 1e-3
    m2 = math.pi * w0 * (theta * 1e-3) / lam_um
    resid = w - model(zc, w0, z0, theta)
    return {
        "w0_um": w0,
        "z0_mm": z0 + zm,
        "theta_mrad": theta,
        "zr_mm": w0 / theta,
        "m2": m2,
        "rms_um": float(np.sqrt((resid**2).mean())),
        "n": int(z.size),
    }
