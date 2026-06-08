from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import aiohttp
import logging

from .const import OPEN_METEO_BASE

_LOGGER = logging.getLogger(__name__)


async def fetch_uv_index(
    session: aiohttp.ClientSession,
    lat: float,
    lon: float,
) -> dict | None:
    """Fetch current UV index and daily UV max from Open-Meteo.

    Returns dict with uv_index and uv_index_max.
    """
    try:
        url = (
            f"{OPEN_METEO_BASE}"
            f"?latitude={lat}&longitude={lon}"
            f"&current=uv_index"
            f"&daily=uv_index_max"
            f"&timezone=auto"
        )
        async with asyncio.timeout(15):
            async with session.get(url) as resp:
                if resp.status != 200:
                    _LOGGER.debug("Open-Meteo UV fetch failed: HTTP %s", resp.status)
                    return None
                data = await resp.json()

        result: dict = {}
        current = data.get("current", {})
        daily = data.get("daily", {})

        uv_now = current.get("uv_index")
        if uv_now is not None:
            result["uv_index"] = round(float(uv_now), 1)

        uv_max_list = daily.get("uv_index_max")
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        dates = daily.get("date", [])
        if uv_max_list and dates:
            for i, d in enumerate(dates):
                if d == today_str and i < len(uv_max_list):
                    val = uv_max_list[i]
                    if val is not None:
                        result["uv_index_max"] = round(float(val), 1)
                    break

        return result if result else None

    except asyncio.TimeoutError:
        _LOGGER.debug("Open-Meteo UV fetch timeout")
        return None
    except Exception as exc:
        _LOGGER.debug("Open-Meteo UV fetch error: %s", exc)
        return None
