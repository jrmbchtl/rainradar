"""MOSMIX-S forecast access with a memory-bounded streaming parser.

The full MOSMIX-S KML decompresses to ~630 MB and contains ~5,600 stations x
240 timesteps x ~16 variables. Parsing it into a dict retains ~1.2 GB of
Python floats and peaks at 2-3.5 GB with the ElementTree DOM live — far too
much for a Home Assistant process.

Instead, the KMZ member is streamed through ``ET.iterparse`` and each
Placemark is cleared right after processing; only the station IDs actually
needed by the configured locations are materialized (~1 MB each, full 240h
series). The complete station-name index is still recorded (strings only,
~0.5 MB) so candidate filtering keeps working.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import io
import logging
import time
import xml.etree.ElementTree as ET
import zipfile

import aiohttp

from .const import DWD_MOSMIX_BASE

_LOGGER = logging.getLogger(__name__)

MOSMIX_UPDATE_INTERVAL = 3600
MOSMIX_CACHE_MAX_AGE = MOSMIX_UPDATE_INTERVAL * 2

# {run_key: (cached_at_ts, _MosmixRun)} — memory-bounded, see _MosmixRun.
_kml_cache: dict[str, tuple[float, _MosmixRun]] = {}

KML_PLACEMARK_TAG = "{http://www.opengis.net/kml/2.2}Placemark"
KML_NAME_TAG = "{http://www.opengis.net/kml/2.2}name"

# The Forecast/value elements live in DWD's point-forecast extension
# namespace (verified against MOSMIX_S_LATEST_240.kmz). The historically
# assumed https://dwd.de/de/XML_synop/MOSMIX-S namespace does NOT occur in
# current files — using it made every lookup miss and logged
# "Station XXXXX not found in MOSMIX-S" for all stations.
DWD_FORECAST_NS = "https://opendata.dwd.de/weather/lib/pointforecast_dwd_extension_V1_0.xsd"
DWD_ELEMENT_NAME_ATTR = f"{{{DWD_FORECAST_NS}}}elementName"
DWD_VALUE_TAG = f"{{{DWD_FORECAST_NS}}}value"
DWD_FORECAST_TAG = f"{{{DWD_FORECAST_NS}}}Forecast"

MOSMIX_ELEMENT_MAP: dict[str, tuple[str, float, float]] = {
    # element: (attr, scale, offset)  →  value * scale + offset
    # Kelvin-based elements convert via offset (K → °C); the historical
    # implementation multiplied by 273.15, producing garbage values.
    "TTT": ("temperature", 1.0, -273.15),
    "TX": ("temp_max", 1.0, -273.15),
    "TN": ("temp_min", 1.0, -273.15),
    "Td": ("dew_point", 1.0, -273.15),
    "PPPP": ("pressure", 0.01, 0.0),
    "FF": ("wind_speed", 3.6, 0.0),
    "DD": ("wind_direction", 1.0, 0.0),
    "FX1": ("wind_gust", 3.6, 0.0),
    "Neff": ("cloud_cover", 1.0, 0.0),
    "N": ("cloud_cover_fallback", 1.0, 0.0),
    "Nl": ("cloud_cover_low", 1.0, 0.0),
    "Nm": ("cloud_cover_mid", 1.0, 0.0),
    "Nh": ("cloud_cover_high", 1.0, 0.0),
    "RR1c": ("precip_rate", 1.0, 0.0),
    "RRS1c": ("precip_rate_strat", 1.0, 0.0),
    "RR3c": ("precip_rate_3h", 0.333, 0.0),
    "Rd02": ("precip_probability", 1.0, 0.0),
    "SunD1": ("sunshine_duration", 1.0, 0.0),
    "Rad1h": ("solar_radiation_raw", 0.0002778, 0.0),
    "VV": ("visibility_raw", 0.001, 0.0),
    "ww": ("weather_code", 1.0, 0.0),
    "W1W2": ("weather_code_w2", 1.0, 0.0),
}


class _MosmixRun:
    """Parsed data for one MOSMIX run — memory-bounded by design.

    Retains only the wanted stations' forecast series (~1 MB each, full 240h)
    plus a strings-only index of every station ID in the KML (~0.5 MB) so
    candidate filtering keeps working without the full 1.2 GB parse.
    """

    __slots__ = ("cached_at", "forecasts", "station_ids")

    def __init__(self, cached_at: float) -> None:
        self.cached_at = cached_at
        # Only wanted stations: {station_id: [timestep_dict, ...]}
        self.forecasts: dict[str, list[dict[str, float]]] = {}
        # Every station ID in the KML (strings only).
        self.station_ids: set[str] = set()


def _parse_placemark(elem: ET.Element) -> tuple[str, list[dict[str, float]]] | None:
    """Extract (station_id, forecast_times) from a Placemark element."""
    name_el = elem.find(KML_NAME_TAG)
    if name_el is None or not name_el.text:
        return None
    station_id = name_el.text.strip()

    forecast_times: list[dict[str, float]] = []
    for fc in elem.findall(f".//{DWD_FORECAST_TAG}"):
        element_name = fc.get(DWD_ELEMENT_NAME_ATTR, "")
        mapping = MOSMIX_ELEMENT_MAP.get(element_name)
        if mapping is None:
            continue
        attr_name, scale, offset = mapping
        value_el = fc.find(DWD_VALUE_TAG)
        if value_el is None or value_el.text is None:
            continue
        values = value_el.text.strip().split()
        for i, val_str in enumerate(values):
            if i >= len(forecast_times):
                forecast_times.append({})
            try:
                val = float(val_str)
                if val < -900:
                    continue
                forecast_times[i][attr_name] = round(val * scale + offset, 1)
            except (ValueError, TypeError):
                pass

    if forecast_times:
        return station_id, forecast_times
    return None


def _parse_mosmix_kml_stream(
    kml_stream: io.BufferedIOBase, wanted: set[str] | None
) -> _MosmixRun:
    """Stream-parse the KML, materializing only ``wanted`` stations.

    ``wanted=None`` materializes every station with forecast data (used by
    tests and by the full-index code path — avoid in production).
    """
    run = _MosmixRun(cached_at=time.time())
    # end-event fires when a Placemark's closing tag is reached; at that point
    # its children are complete and clear() frees the subtree immediately.
    for _event, elem in ET.iterparse(kml_stream, events=("end",)):
        if elem.tag != KML_PLACEMARK_TAG:
            continue
        name_el = elem.find(KML_NAME_TAG)
        if name_el is not None and name_el.text:
            station_id = name_el.text.strip()
            run.station_ids.add(station_id)
            if wanted is None or station_id in wanted:
                parsed = _parse_placemark(elem)
                if parsed is not None:
                    run.forecasts[parsed[0]] = parsed[1]
        elem.clear()
    return run


def _parse_mosmix_kml(kml_bytes: bytes) -> dict:
    """Parse MOSMIX-S KML bytes, extracting all stations.

    Kept for tests/compatibility. Production code must use the streaming
    variant — a full 240h parse retains over 1 GB.
    """
    run = _parse_mosmix_kml_stream(io.BytesIO(kml_bytes), wanted=None)
    return {sid: {"forecasts": fc} for sid, fc in run.forecasts.items()}


def _evict_stale_cache(now_ts: float) -> None:
    """Drop cached KML runs older than the max age (and any non-run keys)."""
    for run_key in list(_kml_cache.keys()):
        ts, _run = _kml_cache[run_key]
        if (now_ts - ts) >= MOSMIX_CACHE_MAX_AGE:
            _kml_cache.pop(run_key, None)


def _cache_run_key() -> str | None:
    """Return the freshest cached run key within its validity window."""
    now_ts = time.time()
    fresh = [
        (ts, run_key)
        for run_key, (ts, _run) in _kml_cache.items()
        if (now_ts - ts) < MOSMIX_UPDATE_INTERVAL
    ]
    if not fresh:
        return None
    return max(fresh)[1]


def get_mosmix_station_ids() -> set[str] | None:
    """Return the station IDs present in the cached MOSMIX-S KML, or None."""
    run_key = _cache_run_key()
    if run_key is None:
        return None
    return set(_kml_cache[run_key][1].station_ids)


def get_cached_mosmix_forecasts() -> dict[str, list[dict]] | None:
    """Return the cached per-station forecast series, or None if not cached.

    Values are raw (unconverted-key) hourly dicts without timestamps — use
    ``_finalize_forecast`` to build the public shape.
    """
    run_key = _cache_run_key()
    if run_key is None:
        return None
    return _kml_cache[run_key][1].forecasts


def get_cached_run_key() -> str | None:
    """Return the freshest cached run key (for timestamp math)."""
    return _cache_run_key()


def _candidate_urls(now: datetime) -> list[str]:
    """Return MOSMIX-S URLs to try, best first.

    DWD publishes MOSMIX-S hourly (filenames carry the issue hour) plus a
    ``MOSMIX_S_LATEST_240.kmz`` alias. Try the exact current run first so the
    cache stays keyed by run, then fall back to LATEST.
    """
    date_str = now.strftime("%Y%m%d%H")
    return [
        f"{DWD_MOSMIX_BASE}/MOSMIX_S_{date_str}_240.kmz",
        f"{DWD_MOSMIX_BASE}/MOSMIX_S_LATEST_240.kmz",
    ]


def _finalize_forecast(raw_fc: list[dict[str, float]], run_dt: datetime) -> list[dict]:
    """Convert a raw per-station series into the public forecast shape."""
    result = []
    for i, fc in enumerate(raw_fc):
        ts = run_dt.timestamp() + (i + 1) * 3600
        entry = {"ts": ts}
        entry.update(fc)
        if "precip_rate_strat" in entry and "precip_rate" in entry:
            entry["precipitation"] = entry.pop("precip_rate", 0) + entry.pop(
                "precip_rate_strat", 0
            )
        elif "precip_rate" in entry:
            entry["precipitation"] = entry.pop("precip_rate")
        elif "precip_rate_strat" in entry:
            entry["precipitation"] = entry.pop("precip_rate_strat")
        if "precip_rate_3h" in entry:
            entry["precipitation_3h"] = entry.pop("precip_rate_3h")
        if "solar_radiation_raw" in entry:
            entry["solar_radiation"] = entry.pop("solar_radiation_raw")
        if "visibility_raw" in entry:
            entry["visibility"] = entry.pop("visibility_raw")
        if "cloud_cover_fallback" in entry and "cloud_cover" not in entry:
            entry["cloud_cover"] = entry.pop("cloud_cover_fallback")
        entry.pop("cloud_cover_fallback", None)
        entry.pop("weather_code_w2", None)
        result.append(entry)
    return result


async def fetch_mosmix_forecasts(
    session: aiohttp.ClientSession,
    station_ids: set[str],
) -> dict[str, list[dict]]:
    """Fetch MOSMIX-S forecasts for a set of stations (memory-bounded).

    Streams the KML directly from the KMZ member and materializes only the
    requested stations. Returns ``{station_id: [hourly entry, ...]}`` —
    stations absent from MOSMIX are simply missing from the result.
    """
    if not station_ids:
        return {}
    try:
        now = datetime.now(UTC)
        now_ts = time.time()

        _evict_stale_cache(now_ts)

        run_key = _cache_run_key()
        if run_key is not None:
            run = _kml_cache[run_key][1]
            missing = station_ids - set(run.forecasts)
            if not missing:
                run_dt = datetime.strptime(run_key, "%Y%m%d%H").replace(tzinfo=UTC)
                return {
                    sid: _finalize_forecast(run.forecasts[sid], run_dt)
                    for sid in station_ids
                    if sid in run.forecasts
                }
            # Fresh run cached but doesn't cover all wanted stations: only
            # refetch when the missing ones could exist in a newer file.
            station_ids = set(station_ids)

        last_error: str | None = None
        for url in _candidate_urls(now):
            try:
                async with asyncio.timeout(120):
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            last_error = f"HTTP {resp.status}"
                            continue
                        data = await resp.read()
            except TimeoutError:
                last_error = "timeout"
                continue

            kmz = zipfile.ZipFile(io.BytesIO(data))
            kml_name = next(
                (n for n in kmz.namelist() if n.endswith(".kml")), None
            )
            if kml_name is None:
                last_error = "KMZ missing KML file"
                continue

            def _stream(kmz: zipfile.ZipFile = kmz, kml_name: str = kml_name) -> _MosmixRun:
                with kmz.open(kml_name) as fh:
                    return _parse_mosmix_kml_stream(fh, wanted=station_ids)

            # The stream parse is CPU-bound (~8-10 s for 630 MB); run it in a
            # thread so the event loop stays responsive.
            run = await asyncio.to_thread(_stream)
            del data, kmz

            date_prefix = url.rsplit("/", 1)[-1].split("_")[1]
            cache_key = (
                date_prefix
                if date_prefix.isdigit() and len(date_prefix) == 10
                else now.strftime("%Y%m%d%H")
            )
            _kml_cache[cache_key] = (now_ts, run)
            run_key = cache_key
            break

        if run_key is None:
            _LOGGER.warning(
                "MOSMIX-S fetch failed: %s", last_error or "no URL succeeded"
            )
            return {}

        run = _kml_cache[run_key][1]
        run_dt = datetime.strptime(run_key, "%Y%m%d%H").replace(tzinfo=UTC)
        return {
            sid: _finalize_forecast(run.forecasts[sid], run_dt)
            for sid in station_ids
            if sid in run.forecasts
        }

    except TimeoutError:
        _LOGGER.warning("MOSMIX-S fetch timeout for %s", station_ids)
        return {}
    except Exception as exc:
        _LOGGER.warning("MOSMIX-S fetch failed for %s: %s", station_ids, exc)
        return {}


async def fetch_mosmix_forecast(
    session: aiohttp.ClientSession,
    station_id: str,
) -> list[dict] | None:
    """Fetch MOSMIX-S forecast for a single station.

    Thin wrapper over :func:`fetch_mosmix_forecasts` (kept for API
    compatibility). Returns None when the station is not a MOSMIX site —
    an expected condition for many CDC observation stations.
    """
    result = await fetch_mosmix_forecasts(session, {station_id})
    return result.get(station_id)
