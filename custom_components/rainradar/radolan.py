from __future__ import annotations

import asyncio
import bz2
import io
import math
import struct
from datetime import datetime, timezone

import aiohttp
import logging

from .const import DWD_RADOLAN_BASE

_LOGGER = logging.getLogger(__name__)

RADOLAN_GRID_SIZE = 900
RADOLAN_GRID_CENTER_X = 449
RADOLAN_GRID_CENTER_Y = 449
RADOLAN_GRID_KM_PER_PX = 1.0

RADOLAN_PROJ_LAT0 = 90.0
RADOLAN_PROJ_LAT_TS = 60.0
RADOLAN_PROJ_LON0 = 10.0
RADOLAN_PROJ_RADIUS = 6370040.0


def _radolan_grid_coords(lat: float, lon: float) -> tuple[int, int]:
    """Convert WGS84 lat/lon to RADOLAN grid x,y indices."""
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)
    lat0 = math.radians(RADOLAN_PROJ_LAT0)
    lat_ts = math.radians(RADOLAN_PROJ_LAT_TS)
    lon0 = math.radians(RADOLAN_PROJ_LON0)
    R = RADOLAN_PROJ_RADIUS

    F = (1 + math.sin(lat_ts)) / 2
    F = F ** (2.0 / (1 - math.sin(lat_ts)))
    F = F * math.tan(math.pi / 4 - lat_r / 2)

    x = R * F * math.sin(lon_r - lon0)
    y = -R * F * math.cos(lon_r - lon0)

    x_km = x / 1000.0
    y_km = y / 1000.0

    gx = int(RADOLAN_GRID_CENTER_X + x_km)
    gy = int(RADOLAN_GRID_CENTER_Y + y_km)

    return gx, gy


def _parse_radolan_binary(data: bytes) -> list[list[int]]:
    """Parse a RADOLAN binary (DWD format) file into a 2D grid."""
    if len(data) < 16:
        return []

    header_len = struct.unpack(">H", data[0:2])[0]
    if header_len < 16 or header_len >= len(data):
        return []

    payload = data[header_len:]
    grid: list[list[int]] = []
    for y in range(RADOLAN_GRID_SIZE):
        row: list[int] = []
        start = y * RADOLAN_GRID_SIZE
        for x in range(RADOLAN_GRID_SIZE):
            idx = start + x
            if idx + 2 <= len(payload):
                val = struct.unpack(">H", payload[idx:idx + 2])[0]
                if val in (0, 0xFFFF):
                    row.append(-1)
                else:
                    row.append(val)
            else:
                row.append(-1)
        grid.append(row)
    return grid



async def _fetch_radolan_file(
    session: aiohttp.ClientSession,
    url: str,
    timeout_s: int = 30,
) -> bytes | None:
    """Fetch and decompress a RADOLAN bz2 file."""
    try:
        async with asyncio.timeout(timeout_s):
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                compressed = await resp.read()
        return bz2.decompress(compressed)
    except Exception:
        return None


async def fetch_radolan_grid(
    session: aiohttp.ClientSession,
) -> list[list[int]] | None:
    """Fetch the latest RADOLAN RW (hourly) grid."""
    try:
        now = datetime.now(timezone.utc)
        urls_to_try = []
        for offset in range(0, 3):
            t = now.replace(
                minute=0, second=0, microsecond=0
            ).timestamp() - offset * 3600
            ts = datetime.fromtimestamp(t, tz=timezone.utc)
            ts_str = ts.strftime("%Y%m%d%H%M")
            url = f"{DWD_RADOLAN_BASE}/rw/raa01-rw_10000-{ts_str}-dwd---bin"
            urls_to_try.append(url)

        for url in urls_to_try:
            data = await _fetch_radolan_file(session, url)
            if data is not None:
                grid = _parse_radolan_binary(data)
                if grid:
                    return grid

        _LOGGER.warning("RADOLAN RW fetch failed for all attempts")
        return None

    except Exception as exc:
        _LOGGER.warning("RADOLAN fetch error: %s", exc)
        return None


def get_radolan_value(
    grid: list[list[int]] | None,
    lat: float,
    lon: float,
) -> float | None:
    """Extract the RADOLAN precipitation value at a location.

    Returns mm/h or None if unavailable.
    """
    if not grid:
        return None
    gx, gy = _radolan_grid_coords(lat, lon)
    if 0 <= gx < RADOLAN_GRID_SIZE and 0 <= gy < RADOLAN_GRID_SIZE:
        val = grid[gy][gx]
        if val < 0:
            return 0.0
        return round(val / 100.0, 1)
    return None


