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

_kml_cache: dict[str, tuple[float, dict[str, dict]]] = {}

KML_NS = {"kml": "http://www.opengis.net/kml/2.2"}

# The Forecast/value elements live in DWD's point-forecast extension
# namespace (verified against MOSMIX_S_LATEST_240.kmz). The historically
# assumed https://dwd.de/de/XML_synop/MOSMIX-S namespace does NOT occur in
# current files — using it made every lookup miss and logged
# "Station XXXXX not found in MOSMIX-S" for all stations.
DWD_FORECAST_NS = "https://opendata.dwd.de/weather/lib/pointforecast_dwd_extension_V1_0.xsd"
DWD_NS = {"dwd": DWD_FORECAST_NS}

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


def _parse_mosmix_kml(kml_bytes: bytes) -> dict:
    """Parse MOSMIX-S KML and extract forecasts for all stations.

    Returns a dict keyed by station ID with forecast data.
    """
    root = ET.fromstring(kml_bytes)

    stations: dict[str, dict] = {}

    for pm in root.iter("{http://www.opengis.net/kml/2.2}Placemark"):
        name_el = pm.find("kml:name", KML_NS)
        if name_el is None:
            continue
        station_id = name_el.text.strip()

        forecast_times: list[dict[str, float]] = []
        for fc in pm.findall(".//dwd:Forecast", DWD_NS):
            element_name = fc.get(f"{{{DWD_FORECAST_NS}}}elementName", "")
            if element_name not in MOSMIX_ELEMENT_MAP:
                continue
            attr_name, scale, offset = MOSMIX_ELEMENT_MAP[element_name]
            value_el = fc.find("dwd:value", DWD_NS)
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
            stations[station_id] = {"forecasts": forecast_times}

    return stations


def _evict_stale_cache(now_ts: float) -> None:
    """Drop cached KML runs older than the max age (and any non-run keys)."""
    for run_key in list(_kml_cache.keys()):
        ts, _stations = _kml_cache[run_key]
        if (now_ts - ts) >= MOSMIX_CACHE_MAX_AGE:
            _kml_cache.pop(run_key, None)


def _cache_run_key() -> str | None:
    """Return the freshest cached run key within its validity window."""
    now_ts = time.time()
    fresh = [
        (ts, run_key)
        for run_key, (ts, _stations) in _kml_cache.items()
        if (now_ts - ts) < MOSMIX_UPDATE_INTERVAL
    ]
    if not fresh:
        return None
    return max(fresh)[1]


def get_mosmix_station_ids() -> set[str] | None:
    """Return set of station IDs present in the cached MOSMIX-S KML, or None if not cached."""
    run_key = _cache_run_key()
    if run_key is None:
        return None
    return set(_kml_cache[run_key][1].keys())


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


async def fetch_mosmix_forecast(
    session: aiohttp.ClientSession,
    station_id: str,
) -> list[dict] | None:
    """Fetch MOSMIX-S forecast for a station.

    Returns a list of hourly forecast dicts or None on failure.

    Note: many CDC observation stations are not MOSMIX forecast sites, so a
    None result for a valid station ID is an expected condition, not an error.
    """
    try:
        now = datetime.now(UTC)
        now_ts = time.time()

        _evict_stale_cache(now_ts)

        run_key = _cache_run_key()
        if run_key is not None:
            stations = _kml_cache[run_key][1]
        else:
            stations = None
            last_error: str | None = None
            for url in _candidate_urls(now):
                try:
                    async with asyncio.timeout(60):
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

                kml_bytes = kmz.read(kml_name)
                stations = _parse_mosmix_kml(kml_bytes)
                # Cache under the exact-run key when the filename carries one,
                # otherwise under the issue hour we requested.
                date_prefix = url.rsplit("/", 1)[-1].split("_")[1]
                cache_key = (
                    date_prefix
                    if date_prefix.isdigit() and len(date_prefix) == 10
                    else now.strftime("%Y%m%d%H")
                )
                _kml_cache[cache_key] = (now_ts, stations)
                run_key = cache_key
                break

            if stations is None:
                _LOGGER.warning("MOSMIX-S fetch failed: %s", last_error or "no URL succeeded")
                return None

        if station_id not in stations:
            # Expected: most CDC observation stations are not MOSMIX sites.
            _LOGGER.debug("Station %s not found in MOSMIX-S", station_id)
            return None

        raw_fc = stations[station_id]["forecasts"]
        run_dt = datetime.strptime(run_key, "%Y%m%d%H").replace(tzinfo=UTC)
        result = []
        for i, fc in enumerate(raw_fc):
            ts = run_dt.timestamp() + (i + 1) * 3600
            entry = {"ts": ts}
            entry.update(fc)
            if "precip_rate_strat" in entry and "precip_rate" in entry:
                entry["precipitation"] = entry.pop("precip_rate", 0) + entry.pop("precip_rate_strat", 0)
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

    except TimeoutError:
        _LOGGER.warning("MOSMIX-S fetch timeout for %s", station_id)
        return None
    except Exception as exc:
        _LOGGER.warning("MOSMIX-S fetch failed for %s: %s", station_id, exc)
        return None
