from __future__ import annotations

import logging

import aiohttp

_LOGGER = logging.getLogger(__name__)

DWD_WARNINGS_URL = (
    "https://s3.eu-central-1.amazonaws.com/"
    "app-prod-static.warnwetter.de/v16/gemeinde_warnings_v2.json"
)


async def fetch_dwd_warnings(session: aiohttp.ClientSession) -> list[dict] | None:
    """Fetch all current DWD severe weather warnings.

    Returns a list of warning dicts from the DWD WarnWetter V16 API,
    each containing level, headLine, event, and regions with polygon geometry.
    Returns None on failure.
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


def _point_in_polygon(lon: float, lat: float, polygon: list[float]) -> bool:
    """Ray-casting point-in-polygon check.

    ``polygon`` is a flat list alternating lat/lon as returned by the
    DWD V16 API ``regions[].polygon`` field:
        [lat1, lon1, lat2, lon2, …, latN, lonN]
    The ring is implicitly closed (first=last).
    """
    if not polygon or len(polygon) < 4:
        return False
    inside = False
    n = len(polygon)
    j = n - 2
    for i in range(0, n, 2):
        lati, loni = polygon[i], polygon[i + 1]
        latj, lonj = polygon[j], polygon[j + 1]
        if ((lati > lat) != (latj > lat)) and (
            lon < (lonj - loni) * (lat - lati) / (latj - lati) + loni
        ):
            inside = not inside
        j = i
    return inside


def resolve_warnings_for_coordinates(
    warnings: list[dict] | None,
    lat: float,
    lon: float,
) -> list[dict]:
    """Find warnings that cover the given coordinates.

    Uses point-in-polygon matching against the GeoJSON polygons included
    in each warning's ``regions``. Returns a list of matching warnings
    (empty list if none match).
    """
    if not warnings:
        return []
    matching: list[dict] = []
    seen: set[str] = set()
    for w in warnings:
        warn_id = w.get("warnId", "")
        if warn_id and warn_id in seen:
            continue
        for region in w.get("regions", []):
            poly = region.get("polygon")
            if poly and _point_in_polygon(lon, lat, poly):
                if warn_id:
                    seen.add(warn_id)
                matching.append(w)
                break
    return matching


def warning_level_from_warnings(warnings_list: list[dict]) -> int:
    """Return the highest warning level (0-4) from a list of warnings.

    0 = no warning, 1 = minor, 2 = moderate, 3 = severe, 4 = extreme.
    Note: DWD heat warnings use levels 50 (moderate) and 51 (extreme);
    these are returned as-is.
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
    best = max(
        warnings_list,
        key=lambda w: w.get("level", 0)
        if isinstance(w.get("level"), (int, float))
        else 0,
    )
    return best.get("headLine") or best.get("headline")
