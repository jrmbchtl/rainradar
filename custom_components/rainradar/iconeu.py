from __future__ import annotations

import asyncio
import io
import math
import re
from datetime import datetime, timezone

import aiohttp
import logging

from .const import DWD_ICON_EU_BASE

_LOGGER = logging.getLogger(__name__)

ICON_EU_VARS = {
    "rain_gsp": "rain_gsp",
    "rain_con": "rain_con",
    "snow_gsp": "snow_gsp",
    "snow_con": "snow_con",
    "tot_prec": "tot_prec",
    "h_snow": "h_snow",
    "freshsnw": "freshsnw",
    "t_2m": "t_2m",
    "relhum": "relhum",
    "u_10m": "u_10m",
    "v_10m": "v_10m",
    "pmsl": "pmsl",
    "clct": "clct",
}

ICONEU_GRID_SIZE = 721
ICONEU_GRID_CENTER = 360
ICONEU_LON0 = 9.0
ICONEU_DLON = 0.0625
ICONEU_LAT_MIN = 44.0
ICONEU_DLAT = 0.0625


def _iconeu_grid_coords(lat: float, lon: float) -> tuple[int, int]:
    """Convert WGS84 lat/lon to ICON-EU regular grid x,y."""
    gy = int(ICONEU_GRID_CENTER + (lat - ICONEU_LAT_MIN) / ICONEU_DLAT)
    gx = int(ICONEU_GRID_CENTER + (lon - ICONEU_LON0) / ICONEU_DLON)
    return gx, gy



def _parse_with_cfgrib(data: bytes, var_name: str) -> list[list[float]]:
    """Parse GRIB2 data using cfgrib + xarray."""
    try:
        import xarray as xr
        ds = xr.open_dataset(
            io.BytesIO(data),
            engine="cfgrib",
            backend_kwargs={
                "filter_by_keys": {"shortName": var_name},
            },
        )
        if var_name in ds:
            values = ds[var_name].values
            return values.tolist()
        for key in ds.data_vars:
            if var_name in key:
                return ds[key].values.tolist()
        return []
    except Exception as exc:
        _LOGGER.debug("cfgrib parse failed for %s: %s", var_name, exc)
        return []


async def _fetch_icon_var(
    session: aiohttp.ClientSession,
    var_name: str,
    run_hour: int,
    forecast_step: int,
) -> list[list[float]] | None:
    """Fetch a single ICON-EU variable for a given run and step."""
    now = datetime.now(timezone.utc)
    run_dt = now.replace(hour=run_hour, minute=0, second=0, microsecond=0)
    date_str = run_dt.strftime("%Y%m%d%H")
    step_str = f"{forecast_step:03d}"
    url = (
        f"{DWD_ICON_EU_BASE}/{run_hour:02d}/{var_name}/"
        f"icon-eu_europe_icosahedral_single-level_{date_str}_{step_str}_2d_{var_name}.grib2.bz2"
    )
    try:
        async with asyncio.timeout(30):
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                import bz2
                compressed = await resp.read()
                data = bz2.decompress(compressed)
        return _parse_with_cfgrib(data, var_name)
    except ImportError:
        _LOGGER.warning("cfgrib not available, cannot parse ICON-EU GRIB2")
        return None
    except Exception as exc:
        _LOGGER.debug("ICON-EU fetch failed for %s step %d: %s", var_name, forecast_step, exc)
        return None


def _extract_value(grid: list[list[float]], lat: float, lon: float) -> float | None:
    """Extract a value from a 2D grid at a lat/lon position."""
    if not grid:
        return None
    gx, gy = _iconeu_grid_coords(lat, lon)
    if 0 <= gx < len(grid[0]) and 0 <= gy < len(grid):
        val = grid[gy][gx]
        if math.isnan(val) or val < -900:
            return None
        return round(float(val), 2)
    return None


async def fetch_icon_eu_precip(
    session: aiohttp.ClientSession,
    lat: float,
    lon: float,
) -> dict | None:
    """Fetch ICON-EU phase-separated precipitation for a location.

    Returns dict with rain_rate, snow_rate, and optionally
    fresh_snow, snow_depth.
    """
    now = datetime.now(timezone.utc)
    run_hour = (now.hour // 6) * 6
    run_dt = now.replace(hour=run_hour, minute=0, second=0, microsecond=0)
    hours_since_run = (now.timestamp() - run_dt.timestamp()) / 3600
    step = max(3, (int(hours_since_run) // 3 + 1) * 3)
    step = min(step, 48)

    results = await asyncio.gather(
        _fetch_icon_var(session, "rain_gsp", run_hour, step),
        _fetch_icon_var(session, "snow_gsp", run_hour, step),
        _fetch_icon_var(session, "h_snow", run_hour, step),
        _fetch_icon_var(session, "freshsnw", run_hour, step),
        return_exceptions=True,
    )
    rain_grid = results[0] if not isinstance(results[0], BaseException) else None
    snow_grid = results[1] if not isinstance(results[1], BaseException) else None
    h_snow_grid = results[2] if not isinstance(results[2], BaseException) else None
    freshsnw_grid = results[3] if not isinstance(results[3], BaseException) else None

    result: dict = {}
    rain = _extract_value(rain_grid, lat, lon)
    snow = _extract_value(snow_grid, lat, lon)

    if rain is not None:
        result["rain_rate"] = round(rain * 3600, 1)
    if snow is not None:
        result["snow_rate"] = round(snow * 3600, 1)
    if h_snow_grid is not None:
        hsnow = _extract_value(h_snow_grid, lat, lon)
        if hsnow is not None:
            result["snow_depth"] = round(hsnow * 100, 1)
    if freshsnw_grid is not None:
        fsnow = _extract_value(freshsnw_grid, lat, lon)
        if fsnow is not None:
            result["fresh_snow"] = round(fsnow * 100, 1)

    return result if result else None
