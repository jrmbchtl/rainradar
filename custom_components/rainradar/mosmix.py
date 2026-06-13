from __future__ import annotations

import asyncio
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from typing import Any

import aiohttp
import logging

from .const import DWD_MOSMIX_BASE
from .station_mapping import DWDStation

_LOGGER = logging.getLogger(__name__)

MOSMIX_UPDATE_INTERVAL = 3600

_kml_cache: dict[str, tuple[float, dict[str, dict]]] = {}

MOSMIX_ELEMENT_MAP = {
    "TTT": ("temperature", 273.15),
    "TX": ("temp_max", 273.15),
    "TN": ("temp_min", 273.15),
    "Td": ("dew_point", 273.15),
    "PPPP": ("pressure", 0.01),
    "FF": ("wind_speed", 3.6),
    "DD": ("wind_direction", 1.0),
    "FX1": ("wind_gust", 3.6),
    "Neff": ("cloud_cover", 12.5),
    "N": ("cloud_cover_fallback", 12.5),
    "Nl": ("cloud_cover_low", 12.5),
    "Nm": ("cloud_cover_mid", 12.5),
    "Nh": ("cloud_cover_high", 12.5),
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


def _parse_mosmix_kml(kml_bytes: bytes) -> dict:
    """Parse MOSMIX-S KML and extract forecasts + positions for all stations.

    Returns a dict keyed by station ID with forecast data, lat, and lon.
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

        lat: float | None = None
        lon: float | None = None
        pt = pm.find("kml:Point", ns)
        if pt is not None:
            coord_el = pt.find("kml:coordinates", ns)
            if coord_el is not None and coord_el.text:
                parts = coord_el.text.strip().split(",")
                try:
                    lon = float(parts[0])
                    lat = float(parts[1])
                except (ValueError, IndexError):
                    pass

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
            entry: dict[str, Any] = {"forecasts": forecast_times}
            if lat is not None and lon is not None:
                entry["lat"] = lat
                entry["lon"] = lon
            stations[station_id] = entry

    return stations


def get_mosmix_stations() -> list[DWDStation] | None:
    """Return DWDStation list from the cached MOSMIX-S KML, or None if not yet cached."""
    now_ts = datetime.now(timezone.utc).timestamp()
    for run_key in list(_kml_cache.keys()):
        ts, stations = _kml_cache[run_key]
        if (now_ts - ts) < MOSMIX_UPDATE_INTERVAL * 2:
            result: list[DWDStation] = []
            for sid, data in stations.items():
                lat = data.get("lat")
                lon = data.get("lon")
                if lat is not None and lon is not None:
                    result.append(DWDStation(sid, sid, lat, lon))
            if result:
                return result
    return None


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
        run_key = run_dt.strftime("%Y%m%d%H")

        cached = _kml_cache.get(run_key)
        if cached and (now.timestamp() - cached[0]) < MOSMIX_UPDATE_INTERVAL:
            stations = cached[1]
        else:
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
            _kml_cache[run_key] = (now.timestamp(), stations)

        if station_id not in stations:
            _LOGGER.warning("Station %s not found in MOSMIX-S", station_id)
            return None

        raw_fc = stations[station_id]["forecasts"]
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

    except asyncio.TimeoutError:
        _LOGGER.warning("MOSMIX-S fetch timeout for %s", station_id)
        return None
    except Exception as exc:
        _LOGGER.warning("MOSMIX-S fetch failed for %s: %s", station_id, exc)
        return None
