from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

from .const import OPEN_METEO_BASE, OPEN_METEO_AIR_QUALITY_BASE

_LOGGER = logging.getLogger(__name__)


async def fetch_openmeteo_weather(
    session: aiohttp.ClientSession,
    lat: float,
    lon: float,
) -> dict | None:
    """Fetch current, hourly (48h) and daily weather from Open-Meteo.

    Returns dict with 'current' and 'hourly' keys.
    'current' contains temperature, humidity, wind, pressure, etc.
    'hourly' is a list of hourly forecast dicts (48 entries).
    """
    try:
        current_params = (
            "temperature_2m,relative_humidity_2m,apparent_temperature,"
            "precipitation,rain,snowfall,weather_code,cloud_cover,"
            "pressure_msl,wind_speed_10m,wind_direction_10m,"
            "wind_gusts_10m,visibility,dew_point_2m,ozone"
        )
        hourly_params = (
            "temperature_2m,precipitation_probability,precipitation,"
            "rain,snowfall,snow_depth,visibility,uv_index,"
            "sunshine_duration,shortwave_radiation,weather_code,"
            "wind_speed_10m,wind_direction_10m,cloud_cover"
        )
        daily_params = (
            "uv_index_max,precipitation_sum,snowfall_sum,"
            "temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max,"
            "wind_speed_10m_max,wind_direction_10m_dominant,weather_code"
        )

        url = (
            f"{OPEN_METEO_BASE}?latitude={lat}&longitude={lon}"
            f"&current={current_params}"
            f"&hourly={hourly_params}"
            f"&daily={daily_params}"
            f"&timezone=auto&forecast_hours=48&forecast_days=14"
        )

        async with asyncio.timeout(15):
            async with session.get(url) as resp:
                if resp.status != 200:
                    _LOGGER.debug("Open-Meteo fetch failed: HTTP %s", resp.status)
                    return None
                data = await resp.json()

        result: dict = {}
        current = data.get("current", {})
        hourly = data.get("hourly", {})
        daily = data.get("daily", {})

        # Current
        current_fields = {
            "temperature_2m": "temperature",
            "relative_humidity_2m": "humidity",
            "apparent_temperature": "apparent_temperature",
            "precipitation": "precipitation",
            "rain": "rain_rate",
            "snowfall": "snow_rate",
            "weather_code": "weather_code_om",
            "cloud_cover": "cloud_cover",
            "pressure_msl": "pressure",
            "wind_speed_10m": "wind_speed",
            "wind_direction_10m": "wind_direction",
            "wind_gusts_10m": "wind_gust",
            "visibility": "visibility",
            "dew_point_2m": "dew_point",
            "ozone": "ozone",
        }
        for src_key, dst_key in current_fields.items():
            val = current.get(src_key)
            if val is not None:
                result[dst_key] = round(float(val), 1)

        if "wind_speed" in result:
            result["wind_speed"] = round(result["wind_speed"] * 3.6, 1)
        if "wind_gust" in result:
            result["wind_gust"] = round(result["wind_gust"] * 3.6, 1)
        if "visibility" in result:
            result["visibility"] = round(result["visibility"] / 1000, 1)

        # UV current
        uv_now = current.get("uv_index")
        if uv_now is not None:
            result["uv_index"] = round(float(uv_now), 1)

        # UV max (daily)
        uv_max_list = daily.get("uv_index_max")
        dates = daily.get("time", [])
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if uv_max_list and dates:
            for i, d in enumerate(dates):
                if d == today_str and i < len(uv_max_list):
                    val = uv_max_list[i]
                    if val is not None:
                        result["uv_index_max"] = round(float(val), 1)
                    break

        # Hourly forecast
        hourly_times = hourly.get("time", [])
        if hourly_times:
            hourly_result = []
            hourly_fields = {
                "temperature_2m": "temperature",
                "precipitation_probability": "precip_probability",
                "precipitation": "precipitation",
                "rain": "rain_rate",
                "snowfall": "snow_rate",
                "snow_depth": "snow_depth",
                "uv_index": "uv_index",
                "sunshine_duration": "sunshine_duration",
                "shortwave_radiation": "solar_radiation",
                "weather_code": "weather_code",
                "visibility": "visibility",
                "wind_speed_10m": "wind_speed",
                "wind_direction_10m": "wind_direction",
                "cloud_cover": "cloud_cover",
            }
            for i, t_str in enumerate(hourly_times):
                if i >= 48:
                    break
                entry: dict = {}
                try:
                    dt = datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc)
                    entry["ts"] = dt.timestamp()
                except (ValueError, TypeError):
                    continue
                for src_key, dst_key in hourly_fields.items():
                    arr = hourly.get(src_key)
                    if arr and i < len(arr):
                        val = arr[i]
                        if val is not None:
                            entry[dst_key] = round(float(val), 1)
                if "sunshine_duration" in entry:
                    entry["sunshine_duration"] = round(entry["sunshine_duration"] / 3600, 2)
                if "visibility" in entry:
                    entry["visibility"] = round(entry["visibility"] / 1000, 1)
                if "snow_depth" in entry:
                    entry["snow_depth"] = round(entry["snow_depth"] * 100, 1)  # m → cm
                if "wind_speed" in entry:
                    entry["wind_speed"] = round(entry["wind_speed"] * 3.6, 1)
                hourly_result.append(entry)
            result["hourly"] = hourly_result

        # Daily forecast (14 days)
        daily_times = daily.get("time", [])
        if daily_times:
            daily_forecast = []
            daily_fields = {
                "temperature_2m_max": "temperature",
                "temperature_2m_min": "templow",
                "precipitation_sum": "precipitation",
                "precipitation_probability_max": "precip_probability",
                "wind_speed_10m_max": "wind_speed",
                "wind_direction_10m_dominant": "wind_direction",
                "weather_code": "weather_code",
                "uv_index_max": "uv_index",
            }
            for i, t_str in enumerate(daily_times):
                if i >= 14:
                    break
                entry: dict = {}
                try:
                    dt = datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc)
                    entry["ts"] = dt.timestamp()
                except (ValueError, TypeError):
                    continue
                for src_key, dst_key in daily_fields.items():
                    arr = daily.get(src_key)
                    if arr and i < len(arr):
                        val = arr[i]
                        if val is not None:
                            entry[dst_key] = round(float(val), 1)
                if "uv_index" in entry:
                    uv = entry["uv_index"]
                    if uv is not None:
                        entry["uv_index"] = round(uv, 1)
                if entry:
                    daily_forecast.append(entry)
            if daily_forecast:
                result["forecast_daily"] = daily_forecast

        return result if result else None

    except asyncio.TimeoutError:
        _LOGGER.debug("Open-Meteo fetch timeout")
        return None
    except Exception as exc:
        _LOGGER.debug("Open-Meteo fetch error: %s", exc)
        return None


async def fetch_openmeteo_air_quality(
    session: aiohttp.ClientSession,
    lat: float,
    lon: float,
) -> dict | None:
    """Fetch air quality data from Open-Meteo Air Quality API.

    Returns dict with keys ``aqi_european``, ``aqi_us``, ``pm2_5``,
    ``pm10``, ``nitrogen_dioxide``, or None on failure.
    """
    try:
        url = (
            f"{OPEN_METEO_AIR_QUALITY_BASE}?latitude={lat}&longitude={lon}"
            f"&current=european_aqi,us_aqi,pm2_5,pm10,nitrogen_dioxide"
        )
        async with asyncio.timeout(15):
            async with session.get(url) as resp:
                if resp.status != 200:
                    _LOGGER.debug("Open-Meteo AQ fetch failed: HTTP %s", resp.status)
                    return None
                data = await resp.json()

        current = data.get("current", {})
        if not current:
            return None

        result: dict = {}
        field_map = {
            "european_aqi": "aqi_european",
            "us_aqi": "aqi_us",
            "pm2_5": "pm2_5",
            "pm10": "pm10",
            "nitrogen_dioxide": "nitrogen_dioxide",
        }
        for src_key, dst_key in field_map.items():
            val = current.get(src_key)
            if val is not None:
                try:
                    result[dst_key] = round(float(val), 1)
                except (ValueError, TypeError):
                    pass
        return result if result else None

    except asyncio.TimeoutError:
        _LOGGER.debug("Open-Meteo AQ fetch timeout")
        return None
    except Exception as exc:
        _LOGGER.debug("Open-Meteo AQ fetch error: %s", exc)
        return None

