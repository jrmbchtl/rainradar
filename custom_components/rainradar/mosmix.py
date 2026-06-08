from __future__ import annotations

import asyncio
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone

import aiohttp
import logging

from .const import DWD_MOSMIX_BASE

_LOGGER = logging.getLogger(__name__)

MOSMIX_UPDATE_INTERVAL = 3600

MOSMIX_ELEMENT_MAP = {
    "TTT": ("temperature", 273.15),
    "TX": ("temp_max", 273.15),
    "TN": ("temp_min", 273.15),
    "Td": ("dew_point", 273.15),
    "PPPP": ("pressure", 0.01),
    "FF": ("wind_speed", 3.6),
    "DD": ("wind_direction", 1.0),
    "FX1": ("wind_gust", 3.6),
    "Neff": ("cloud_cover", 1.0),
    "N": ("cloud_cover_fallback", 1.0),
    "Nl": ("cloud_cover_low", 1.0),
    "Nm": ("cloud_cover_mid", 1.0),
    "Nh": ("cloud_cover_high", 1.0),
    "RR1c": ("precip_rate", 1.0),
    "RRS1c": ("precip_rate_strat", 1.0),
    "RR3c": ("precip_rate_3h", 0.333),
    "Rd02": ("precip_probability", 1.0),
    "SunD1": ("sunshine_duration", 1.0),
    "Rad1h": ("solar_radiation_raw", 0.0002778),
    "VV": ("visibility_raw", 0.001),
    "ww": ("weather_code", 1.0),
    "W1W2": ("weather_code_w2", 1.0),
}

DWD_WW_TEXT = {
    0: "No weather phenomena",
    1: "Cloudless",
    2: "Low clouds",
    3: "Alto clouds",
    4: "Cirrus",
    5: "Alto-cumulus",
    6: "Mist",
    7: "Fog",
    8: "Drizzle",
    9: "Rain",
    10: "Snow",
    11: "Shower",
    12: "Thunderstorm",
    13: "Hail",
    14: "Sleet",
    15: "Freezing rain",
    16: "Dust",
    17: "Smoke",
    18: "Sand",
    19: "Ash",
    20: "Blowing snow",
    21: "Blowing sand",
    22: "Freezing fog",
    23: "Rime fog",
    24: "Widespread dust",
    25: "Haze",
    30: "Fog",
    31: "Fog patches",
    32: "Mist",
    33: "Thick fog",
    34: "Freezing fog",
    35: "Rime fog",
    40: "Precipitation",
    41: "Light precipitation",
    42: "Heavy precipitation",
    43: "Intermittent light precipitation",
    44: "Intermittent precipitation",
    45: "Intermittent heavy precipitation",
    46: "Rain or snow",
    47: "Light rain or snow",
    48: "Heavy rain or snow",
    49: "Freezing precipitation",
    50: "Drizzle",
    51: "Light drizzle",
    52: "Moderate drizzle",
    53: "Heavy drizzle",
    55: "Light freezing drizzle",
    56: "Moderate freezing drizzle",
    57: "Heavy freezing drizzle",
    58: "Light rain and drizzle",
    59: "Heavy rain and drizzle",
    60: "Rain",
    61: "Light rain",
    62: "Moderate rain",
    63: "Heavy rain",
    65: "Light freezing rain",
    66: "Moderate freezing rain",
    67: "Heavy freezing rain",
    68: "Light rain and snow",
    69: "Heavy rain and snow",
    70: "Snow",
    71: "Light snow",
    72: "Moderate snow",
    73: "Heavy snow",
    75: "Heavy snow drifts",
    76: "Diamond dust",
    77: "Snow grains",
    78: "Ice crystals",
    79: "Ice pellets",
    80: "Light showers",
    81: "Moderate showers",
    82: "Heavy showers",
    83: "Light rain/snow showers",
    84: "Heavy rain/snow showers",
    85: "Light snow showers",
    86: "Heavy snow showers",
    87: "Light ice showers",
    88: "Heavy ice showers",
    89: "Hail showers",
    90: "Thunderstorm",
    91: "Light thunderstorm",
    92: "Moderate thunderstorm",
    93: "Heavy thunderstorm",
    94: "Heavy hailstorm",
    95: "Thunderstorm with snow",
    96: "Heavy thunderstorm with hail",
    97: "Lightning",
    98: "Lightning and hail",
    99: "Heavy lightning and hail",
}


def _parse_mosmix_kml(kml_bytes: bytes) -> dict:
    """Parse MOSMIX-S KML and extract forecasts for all stations.

    Returns a dict keyed by station ID with forecast data.
    """
    root = ET.fromstring(kml_bytes)
    ns = {"dwd": "https://dwd.de/de/XML_synop/MOSMIX-S",
          "kml": "http://www.opengis.net/kml/2.2"}

    stations: dict[str, dict] = {}

    for pm in root.iter("{http://www.opengis.net/kml/2.2}Placemark"):
        name_el = pm.find("kml:name", ns)
        if name_el is None:
            continue
        station_id = name_el.text.strip()

        forecast_times: list[dict[str, float]] = []
        for fc in pm.findall("dwd:Forecast", ns):
            element_name = fc.get("dwd:elementName", "")
            if element_name not in MOSMIX_ELEMENT_MAP:
                continue
            attr_name, scale = MOSMIX_ELEMENT_MAP[element_name]
            value_el = fc.find("dwd:value", ns)
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
                    forecast_times[i][attr_name] = round(val * scale, 1)
                except (ValueError, TypeError):
                    pass

        if forecast_times:
            stations[station_id] = {"forecasts": forecast_times}

    return stations


async def fetch_mosmix_forecast(
    session: aiohttp.ClientSession,
    station_id: str,
) -> list[dict] | None:
    """Fetch MOSMIX-S forecast for a station.

    Returns a list of hourly forecast dicts or None on failure.
    """
    try:
        now = datetime.now(timezone.utc)
        run_hour = (now.hour // 6) * 6
        run_dt = now.replace(hour=run_hour, minute=0, second=0, microsecond=0)
        date_str = run_dt.strftime("%Y%m%d%H")
        url = f"{DWD_MOSMIX_BASE}/MOSMIX_S_{date_str}_240.kmz"

        async with asyncio.timeout(60):
            async with session.get(url) as resp:
                if resp.status != 200:
                    _LOGGER.warning("MOSMIX-S fetch failed: HTTP %s", resp.status)
                    return None
                data = await resp.read()

        kmz = zipfile.ZipFile(io.BytesIO(data))
        kml_name = next(
            (n for n in kmz.namelist() if n.endswith(".kml")), None
        )
        if kml_name is None:
            _LOGGER.warning("MOSMIX-S KMZ missing KML file")
            return None

        kml_bytes = kmz.read(kml_name)
        stations = _parse_mosmix_kml(kml_bytes)

        if station_id not in stations:
            _LOGGER.debug("Station %s not found in MOSMIX-S", station_id)
            return None

        raw_fc = stations[station_id]["forecasts"]
        result = []
        for i, fc in enumerate(raw_fc):
            ts = run_dt.timestamp() + i * 3600
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

    except asyncio.TimeoutError:
        _LOGGER.warning("MOSMIX-S fetch timeout for %s", station_id)
        return None
    except Exception as exc:
        _LOGGER.warning("MOSMIX-S fetch failed for %s: %s", station_id, exc)
        return None
