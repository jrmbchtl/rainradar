from __future__ import annotations

import asyncio
import io
import logging
import zipfile
from datetime import date, timedelta, datetime, timezone
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DWD_CDC_HOURLY,
    DWD_CDC_10MIN,
    DWD_CDC_DAILY,
    apparent_temperature,
    resolve_condition,
    resolve_location_specs,
    WW_CODE_TO_TEXT,
    CDC_10MIN_PRODUCTS,
    CDC_10MIN_FILENAMES,
    CDC_HOURLY_PRODUCTS,
    CDC_DAILY_PRODUCTS,
)
from .station_mapping import fetch_stations, find_nearest_stations, DWDStation
from .openmeteo import fetch_openmeteo_weather

_LOGGER = logging.getLogger(__name__)


class WeatherDataCoordinator(DataUpdateCoordinator):
    """Fast coordinator for current weather sensor data.

    Fetches DWD 10-min + hourly + daily observations and Open-Meteo
    current weather. Returns sensor values immediately without waiting
    for radar frames or forecasts.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: aiohttp.ClientSession,
        stations: list[DWDStation],
    ) -> None:
        self.entry = entry
        scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_weather",
            update_interval=timedelta(seconds=scan_interval),
        )
        self._session = session
        self._stations = stations
        self._uv_max_date: dict[str, date] = {}

    async def _get_zip_text(self, base_url: str, path: str, filename: str) -> str | None:
        url = f"{base_url}/{path}/{filename}"
        try:
            async with asyncio.timeout(15):
                async with self._session.get(url) as resp:
                    if resp.status != 200:
                        return None
                    raw = await resp.read()
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                for name in zf.namelist():
                    if name.startswith("produkt_"):
                        raw_text = zf.read(name)
                        for enc in ("utf-8", "latin-1"):
                            try:
                                return raw_text.decode(enc)
                            except (UnicodeDecodeError, UnicodeEncodeError):
                                continue
                        return raw_text.decode("utf-8", errors="replace")
            return None
        except Exception:
            return None

    async def _fetch_product(
        self, station_id: str, product: str, source: str = "auto"
    ) -> tuple[list[str], list[str], str | None]:
        if source == "auto" or source == "10min":
            if product in CDC_10MIN_PRODUCTS:
                path_10, cols_10, keys_10 = CDC_10MIN_PRODUCTS[product]
                filename = CDC_10MIN_FILENAMES[product].format(station_id=station_id)
                text = await self._get_zip_text(DWD_CDC_10MIN, path_10, filename)
                if text is not None:
                    return cols_10, keys_10, text
        if source == "auto" or source == "hourly":
            if product in CDC_HOURLY_PRODUCTS:
                path_h, cols_h, keys_h = CDC_HOURLY_PRODUCTS[product]
                filename = f"stundenwerte_{product}_{station_id}_akt.zip"
                text = await self._get_zip_text(DWD_CDC_HOURLY, path_h, filename)
                if text is not None:
                    return cols_h, keys_h, text
        return [], [], None

    async def _fetch_obs(self, station_id: str) -> dict[str, Any]:
        result: dict = {}
        for product in ("TU", "FF", "RR", "SO"):
            cols, keys, text = await self._fetch_product(station_id, product)
            if text is None:
                continue
            try:
                lines = text.strip().split("\n")
                if len(lines) < 2:
                    continue
                header = lines[0]
                data_lines = lines[1:]
                header_cols = [c.strip() for c in header.split(";")]
                idxs = [header_cols.index(c) for c in cols if c in header_cols]
                used_keys = keys[:len(idxs)]
                last = data_lines[-1].split(";")
                for idx_field, result_key in zip(idxs, used_keys):
                    if idx_field >= len(last):
                        continue
                    val = last[idx_field].strip()
                    if val and val not in ("-999", "999.0", "-999.0"):
                        try:
                            result[result_key] = round(float(val), 1)
                        except (ValueError, TypeError):
                            pass
            except Exception as exc:
                _LOGGER.debug("Parse error for %s: %s", product, exc)

            for product in ("DD", "CO", "VV"):
                if product not in CDC_HOURLY_PRODUCTS:
                    continue
                path_h, cols_h, keys_h = CDC_HOURLY_PRODUCTS[product]
                filename = f"stundenwerte_{product}_{station_id}_akt.zip"
                text = await self._get_zip_text(DWD_CDC_HOURLY, path_h, filename)
                if text is None:
                    continue
                try:
                    lines = text.strip().split("\n")
                    if len(lines) < 2:
                        continue
                    header = lines[0]
                    header_cols = [c.strip() for c in header.split(";")]
                    for col, key in zip(cols_h, keys_h):
                        if col in header_cols:
                            idx = header_cols.index(col)
                            last = lines[-1].split(";")
                            if idx < len(last):
                                val = last[idx].strip()
                                if val and val not in ("-999", "999.0", "-999.0"):
                                    try:
                                        raw = float(val)
                                        if col == "V_V":
                                            raw = raw / 10.0  # 0.1 km → km
                                        if col == "N":
                                            if raw < 0:
                                                continue
                                            raw = raw * 12.5  # oktas (0-8) → percent (0-100)
                                        result[key] = round(raw, 1)
                                    except (ValueError, TypeError):
                                        pass
                except Exception as exc:
                    _LOGGER.debug("Parse error for %s: %s", product, exc)

        if "wind_speed" in result:
            result["wind_speed"] = round(result["wind_speed"] * 3.6, 1)
        return result

    async def _fetch_daily_data(self, station_id: str) -> dict[str, Any]:
        result: dict = {}
        for product, (path, cols, keys) in CDC_DAILY_PRODUCTS.items():
            filename = f"tageswerte_{product}_{station_id}_akt.zip"
            text = await self._get_zip_text(DWD_CDC_DAILY, path, filename)
            if text is None:
                continue
            try:
                lines = text.strip().split("\n")
                if len(lines) < 2:
                    continue
                header = lines[0]
                header_cols = [c.strip() for c in header.split(";")]
                last = lines[-1].split(";")
                for col, key in zip(cols, keys):
                    if col in header_cols:
                        idx = header_cols.index(col)
                        if idx < len(last):
                            val = last[idx].strip()
                            if val and val not in ("-999", "999.0", "-999.0"):
                                try:
                                    raw = float(val)
                                    if col == "NM":
                                        if raw < 0:
                                            continue
                                        raw = raw * 12.5  # oktas (0-8) → percent (0-100)
                                    result[key] = round(raw, 1)
                                except (ValueError, TypeError):
                                    pass
            except Exception as exc:
                _LOGGER.debug("Daily parse error for %s: %s", product, exc)
        return result

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            if not self._stations:
                fetched = await fetch_stations(self._session)
                self._stations.extend(fetched)

            location_specs = resolve_location_specs(self.hass, self.entry)

            BACKUP_DEPTH = 3

            result_locations: dict[str, dict] = {}
            all_station_ids: set[str] = set()
            location_top_stations: dict[str, list[str]] = {}

            for loc_key, loc_name, source_entity, lat, lon, _sl in location_specs:
                top_stations = find_nearest_stations(lat, lon, self._stations, n=BACKUP_DEPTH)
                if not top_stations:
                    _LOGGER.warning("No station found for %s", loc_name)
                    continue
                top_ids = [s[0].station_id for s in top_stations]
                location_top_stations[loc_key] = top_ids
                all_station_ids.update(top_ids)
                result_locations[loc_key] = {
                    "location_name": loc_name,
                    "source_entity": source_entity,
                    "station_id": top_ids[0],
                    "station_name": top_stations[0][0].name,
                    "station_distance_km": top_stations[0][1],
                    "latitude": lat,
                    "longitude": lon,
                }

            obs_tasks = {
                sid: asyncio.create_task(self._fetch_obs(sid))
                for sid in all_station_ids
            }
            daily_tasks = {
                sid: asyncio.create_task(self._fetch_daily_data(sid))
                for sid in all_station_ids
            }
            obs_results: dict[str, dict] = {}
            daily_results: dict[str, dict] = {}
            for sid, task in obs_tasks.items():
                obs_results[sid] = await task
            for sid, task in daily_tasks.items():
                daily_results[sid] = await task

            # Open-Meteo current for each location (parallel)
            om_results: dict[str, dict | None] = {}
            async def _fetch_om(loc_key: str, lat: float, lon: float) -> tuple[str, dict | None]:
                try:
                    return loc_key, await fetch_openmeteo_weather(self._session, lat, lon)
                except Exception:
                    return loc_key, None
            om_tasks = [
                asyncio.create_task(_fetch_om(loc_key, lat, lon))
                for loc_key, _, _, lat, lon, _sl in location_specs
            ]
            for task in asyncio.as_completed(om_tasks):
                loc_key, result = await task
                om_results[loc_key] = result

            for loc_key in result_locations:
                loc = result_locations[loc_key]
                lat = loc["latitude"]
                lon = loc["longitude"]
                top_ids = location_top_stations.get(loc_key, [])

                for backup_sid in top_ids:
                    if backup_sid in obs_results:
                        for k, v in obs_results[backup_sid].items():
                            if k not in loc or loc[k] is None:
                                loc[k] = v
                for backup_sid in top_ids:
                    if backup_sid in daily_results:
                        for k, v in daily_results[backup_sid].items():
                            if k not in loc or loc[k] is None:
                                loc[k] = v
                # Merge Open-Meteo (fills any remaining gaps)
                om_data = om_results.get(loc_key)
                if om_data:
                    for k, v in om_data.items():
                        if k == "hourly":
                            if v:
                                loc["forecast_hourly"] = v
                            continue
                        if k == "forecast_daily":
                            if v:
                                loc["forecast_daily"] = v
                            continue
                        if k not in loc or loc[k] is None:
                            loc[k] = v
                    # OM pressure is sea-level — always override DWD station-level
                    if "pressure" in om_data:
                        loc["pressure"] = om_data["pressure"]

                # Compute condition via priority chain
                if "weather_code" in loc and loc["weather_code"] is not None:
                    dwd_ww = int(loc["weather_code"])
                else:
                    dwd_ww = None
                mosmix_ww = None
                om_wmo = loc.get("weather_code_om")
                if om_wmo is not None:
                    om_wmo = int(om_wmo)
                cloud_cover = loc.get("cloud_cover")
                precip = loc.get("precipitation")
                temp = loc.get("temperature")

                loc["condition"] = resolve_condition(
                    dwd_ww=dwd_ww,
                    mosmix_ww=mosmix_ww,
                    openmeteo_wmo=om_wmo,
                    cloud_cover=cloud_cover,
                    precipitation=precip,
                    temperature=temp,
                )
                if loc.get("weather_code") is None and om_wmo is not None:
                    loc["weather_code"] = om_wmo
                wc = loc.get("weather_code")
                loc["weather_code_text"] = WW_CODE_TO_TEXT.get(int(wc)) if wc is not None else None

                # UV max — only update once per day
                today = datetime.now(timezone.utc).date()
                if "uv_index_max" in om_data and self._uv_max_date.get(loc_key) != today:
                    loc["uv_index_max"] = om_data["uv_index_max"]
                    self._uv_max_date[loc_key] = today

                # rain_24h / snow_24h from Open-Meteo daily forecast (today's sum)
                if om_data and "forecast_daily" in om_data and om_data["forecast_daily"]:
                    today_fc = om_data["forecast_daily"][0]
                    if "precipitation" in today_fc and today_fc["precipitation"] is not None:
                        loc["rain_24h"] = today_fc["precipitation"]
                    if "snowfall_sum" in today_fc and today_fc["snowfall_sum"] is not None:
                        loc["snow_24h"] = today_fc["snowfall_sum"]

                # Apparent temperature (only compute Steadman if OM didn't provide)
                if ("apparent_temperature" not in loc or loc["apparent_temperature"] is None) and "temperature" in loc:
                    t = loc["temperature"]
                    h = loc.get("humidity")
                    w = loc.get("wind_speed")
                    if h is not None and w is not None:
                        loc["apparent_temperature"] = apparent_temperature(t, h, w)
                    elif h is not None:
                        loc["apparent_temperature"] = apparent_temperature(t, h, 0)
                    else:
                        loc["apparent_temperature"] = t

                # Solar radiation from Open-Meteo hourly (take current hour)
                if "solar_radiation" not in loc or loc["solar_radiation"] is None:
                    if om_data and "hourly" in om_data:
                        now_hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00")
                        for h_entry in om_data["hourly"]:
                            h_ts = h_entry.get("ts")
                            if h_ts:
                                h_iso = datetime.fromtimestamp(h_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:00")
                                if h_iso == now_hour and "solar_radiation" in h_entry:
                                    loc["solar_radiation"] = h_entry["solar_radiation"]
                                    break

                # Fresh snow (snow_rate) + snow depth from Open-Meteo hourly
                if ("fresh_snow" not in loc or loc["fresh_snow"] is None) and om_data and "hourly" in om_data:
                    for h_entry in om_data["hourly"]:
                        val = h_entry.get("snow_rate")
                        if val is not None and val > 0:
                            loc["fresh_snow"] = val
                            break
                if ("snow_depth" not in loc or loc["snow_depth"] is None) and om_data and "hourly" in om_data:
                    for h_entry in om_data["hourly"]:
                        val = h_entry.get("snow_depth")
                        if val is not None:
                            loc["snow_depth"] = val
                            break

                # precip_probability from OM hourly (take first hourly entry)
                if ("precip_probability" not in loc or loc["precip_probability"] is None) and om_data and "hourly" in om_data:
                    for h_entry in om_data["hourly"]:
                        val = h_entry.get("precip_probability")
                        if val is not None:
                            loc["precip_probability"] = val
                            break

                # Defaults for fields that are often None in the data
                if loc.get("uv_index") is None:
                    loc["uv_index"] = 0
                if loc.get("fresh_snow") is None:
                    loc["fresh_snow"] = 0
                if loc.get("rain_24h") is None:
                    loc["rain_24h"] = 0
                if loc.get("snow_24h") is None:
                    loc["snow_24h"] = 0

            return {
                "locations": result_locations,
                "stations_count": len(self._stations),
                "last_update": datetime.now(timezone.utc).isoformat(),
            }

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise UpdateFailed(f"Weather data update failed: {exc}") from exc
