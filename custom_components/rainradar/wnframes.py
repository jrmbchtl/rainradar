"""WeatherNext global precipitation overlay frame rendering.

Converts WN3 ``experimental_tp_1hr_mean`` 0.1° grids into PNG overlay frames
using the DWD precipitation color ramp, so the card can render a
"2 days @ 1 h steps, global" layer alongside the DWD 5-min composite.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from .const import PRECIP_COLORS

_LOGGER = logging.getLogger(__name__)

# (RGB, mm/h) ramp from const.PRECIP_COLORS — split for numpy interpolation.
_RAMP_RGB = np.array([rgb for rgb, _ in PRECIP_COLORS], dtype=np.float32)
_RAMP_VAL = np.array([v for _, v in PRECIP_COLORS], dtype=np.float32)


def grid_to_rgba(
    grid: np.ndarray,
    vmax: float | None = None,
) -> np.ndarray:
    """Convert a precipitation (mm/h) grid to an RGBA uint8 image.

    Values below the ramp minimum (0.1) and NaN/missing become transparent.
    """
    if vmax is None:
        vmax = float(_RAMP_VAL[-1])
    vals = np.nan_to_num(np.asarray(grid, dtype=np.float32), nan=-1.0)
    out = np.zeros((*vals.shape, 4), dtype=np.uint8)

    # Log-scale normalization between ramp start and vmax (DWD's ramp is
    # perceptually logarithmic; same mapping family as pixel_intensity).
    mask = (vals >= _RAMP_VAL[0]) & (vals <= vmax)
    if not mask.any():
        return out

    norm = np.zeros_like(vals, dtype=np.float32)
    lo = float(_RAMP_VAL[0])
    log_lo, log_hi = np.log(lo), np.log(vmax)
    norm[mask] = (np.log(vals[mask]) - log_lo) / (log_hi - log_lo)
    norm = np.clip(norm, 0.0, 1.0)

    idx = norm[mask] * (len(_RAMP_VAL) - 1)
    i0 = np.floor(idx).astype(int)
    i1 = np.minimum(i0 + 1, len(_RAMP_VAL) - 1)
    frac = (idx - i0)[..., None]

    rgb = _RAMP_RGB[i0] * (1.0 - frac) + _RAMP_RGB[i1] * frac
    out[..., 0][mask] = rgb[..., 0].astype(np.uint8)
    out[..., 1][mask] = rgb[..., 1].astype(np.uint8)
    out[..., 2][mask] = rgb[..., 2].astype(np.uint8)
    out[..., 3][mask] = 255
    return out


def save_frame_png(path: Path, rgba: np.ndarray) -> None:
    """Write an RGBA array as PNG (atomic), applying the neutralize step."""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    img = Image.fromarray(rgba, mode="RGBA")
    img.save(tmp, "PNG", optimize=True)
    tmp.replace(path)
