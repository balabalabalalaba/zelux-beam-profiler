"""Draw the program icon (a Gaussian beam spot) as packaging/BeamProfiler.ico.

Run from anywhere:  python packaging/make_icon.py
"""

from pathlib import Path

import numpy as np
from PIL import Image

OUT = Path(__file__).resolve().parent / "BeamProfiler.ico"


def _jet_like(v: np.ndarray) -> np.ndarray:
    """Dark blue -> cyan -> yellow -> red -> white, like the page's default colour map."""
    stops = np.array([0.0, 0.25, 0.5, 0.75, 0.92, 1.0])
    cols = np.array([[10, 20, 70], [20, 120, 220], [40, 220, 200], [250, 220, 40], [240, 60, 30], [255, 250, 240]], float)
    out = np.empty(v.shape + (3,))
    for c in range(3):
        out[..., c] = np.interp(v, stops, cols[:, c])
    return out


def render(size: int = 256) -> Image.Image:
    n = size
    y, x = np.mgrid[0:n, 0:n] + 0.5
    c = n / 2
    # rounded-square tile
    m = n * 0.06
    half = n / 2 - m
    corner = n * 0.18
    dx = np.maximum(np.abs(x - c) - (half - corner), 0)
    dy = np.maximum(np.abs(y - c) - (half - corner), 0)
    tile = np.clip(corner - np.hypot(dx, dy) + 0.5, 0, 1)
    tile[(np.abs(x - c) > half) | (np.abs(y - c) > half)] = 0
    # elliptical Gaussian spot
    u, v = (x - c) / (n * 0.26), (y - c) / (n * 0.20)
    spot = np.exp(-2 * (u**2 + v**2))
    rgb = _jet_like(spot)
    # thin 1/e^2 contour ring
    ring = np.exp(-((np.hypot(u, v) - 1.0) * n / 6) ** 2)
    rgb = rgb * (1 - 0.8 * ring[..., None]) + 255 * 0.8 * ring[..., None]
    rgba = np.dstack([rgb, 255 * tile]).clip(0, 255).astype(np.uint8)
    return Image.fromarray(rgba, "RGBA")


def main() -> None:
    img = render(256)
    img.save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    img.save(OUT.with_suffix(".png"))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
