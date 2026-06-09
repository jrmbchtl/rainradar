from __future__ import annotations

import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

DWD_WARNINGS_URL = "https://www.dwd.de/DWD/warnungen/warnapp/json/warnings.json"


async def fetch_dwd_warnings(session: aiohttp.ClientSession) -> dict[str, list[dict]] | None:
    """Fetch all current DWD severe weather warnings.

    Returns dict[str, list[dict]] keyed by region code (AGS), or None on failure.
    """
    try:
        async with session.get(DWD_WARNINGS_URL) as resp:
            if resp.status != 200:
                _LOGGER.debug("DWD warnings fetch failed: HTTP %s", resp.status)
                return None
            data = await resp.json()
        return data.get("warnings")
    except Exception as exc:
        _LOGGER.debug("DWD warnings fetch error: %s", exc)
        return None


def resolve_warnings_for_location(
    warnings: dict[str, list[dict]] | None,
    location_name: str,
) -> list[dict]:
    """Find warnings matching a location name.

    Matches by checking if the warncell ``regionName`` contains the given
    location name (case-insensitive). Returns the list of matching warnings,
    or an empty list if none match.
    """
    if not warnings or not location_name:
        return []
    name_lower = location_name.lower()
    matching: list[dict] = []
    seen: set[int] = set()
    for cell_warnings in warnings.values():
        for w in cell_warnings:
            region = w.get("regionName", "")
            if name_lower in region.lower():
                wid = id(w)
                if wid not in seen:
                    seen.add(wid)
                    matching.append(w)
    return matching


def warning_level_from_warnings(warnings_list: list[dict]) -> int:
    """Return the highest warning level (0-4) from a list of warnings.

    0 = no warning, 1 = minor, 2 = moderate, 3 = severe, 4 = extreme.
    """
    if not warnings_list:
        return 0
    max_level = 0
    for w in warnings_list:
        level = w.get("level", 0)
        if isinstance(level, (int, float)) and level > max_level:
            max_level = int(level)
    return max_level


def warning_headline_from_warnings(warnings_list: list[dict]) -> str | None:
    """Return the headline of the highest-level warning, or None."""
    if not warnings_list:
        return None
    best = max(warnings_list, key=lambda w: w.get("level", 0) if isinstance(w.get("level"), (int, float)) else 0)
    return best.get("headline")
