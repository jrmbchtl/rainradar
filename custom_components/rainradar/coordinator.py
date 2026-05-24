from __future__ import annotations

import asyncio
import csv
import io
import logging
import zipfile
from datetime import timedelta, datetime, timezone

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    CONF_LOCATIONS,
    CONF_NAME,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_SCAN_INTERVAL,
    CONF_DEVICE_TRACKER,
    CONF_DEVICE_TRACKERS,
    CONF_ZONES,
    CONF_TRACKED_LOCATION_NAME,
    DEFAULT_SCAN_INTERVAL,
    DWD_OPENDATA,
)
from .station_mapping import fetch_stations, find_nearest_station, DWDStation

_LOGGER = logging.getLogger(__name__)

CDC_BASE = f"{DWD_OPENDATA}/climate_environment/CDC/observations_germany/climate/hourly"
CDC_PRODUCTS = {
    "TU": ("air_temperature/recent", ["TT_TU", "RF_TU"], ["temperature", "humidity"]),
    "FF": ("wind/recent", ["F", "D"], ["wind_speed", "wind_direction"]),
    "RR": ("precipitation/recent", ["R1"], ["precipitation"]),
}
CDC_ICON_MAP = {
    "sunny": "mdi:weather-sunny",
    "partlycloudy": "mdi:weather-partly-cloudy",
    "cloudy": "mdi:weather-cloudy",
    "fog": "mdi:weather-fog",
    "rainy": "mdi:weather-rainy",
    "snowy": "mdi:weather-snowy",
}

CONDITION_FROM_TEMP = [
    (20, "sunny"),
    (10, "partlycloudy"),
    (5, "cloudy"),
    (-10, "fog"),
    (-99, "fog"),
]


class RainradarCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.locations: list[dict] = entry.options.get(CONF_LOCATIONS, [])
        scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )

        self.stations: list[DWDStation] = []
        self.radar_frames: dict[str, list[str]] = {}
        self._session = aiohttp.ClientSession()

    @property
    def session(self) -> aiohttp.ClientSession:
        return self._session

    @staticmethod
    def _normalize_entity_list(value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str)]
        return []

    async def _get_zip_text(self, product: str, station_id: str) -> str | None:
        path, _, _ = CDC_PRODUCTS[product]
        url = f"{CDC_BASE}/{path}/stundenwerte_{product}_{station_id}_akt.zip"
        try:
            async with asyncio.timeout(15):
                async with self.session.get(url) as resp:
                    if resp.status != 200:
                        _LOGGER.debug("No %s data for station %s", product, station_id)
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
        except Exception as exc:
            _LOGGER.debug("Failed to fetch %s for %s: %s", product, station_id, exc)
            return None

    async def _fetch_obs(self, station_id: str) -> dict:
        result: dict = {}
        tasks = {}
        for prod in CDC_PRODUCTS:
            tasks[prod] = asyncio.create_task(self._get_zip_text(prod, station_id))

        for prod, task in tasks.items():
            text = await task
            if text is None:
                continue
            _, csv_cols, result_keys = CDC_PRODUCTS[prod]
            try:
                lines = text.strip().split("\n")
                if len(lines) < 2:
                    continue
                header = lines[0]
                data_lines = lines[1:]
                header_cols = [c.strip() for c in header.split(";")]
                idxs = [header_cols.index(c) for c in csv_cols]
                last = data_lines[-1].split(";")
                for idx_field, result_key in zip(idxs, result_keys):
                    if idx_field >= len(last):
                        continue
                    val = last[idx_field].strip()
                    if val and val not in ("-999", "999.0", "-999.0"):
                        try:
                            result[result_key] = round(float(val), 1)
                        except (ValueError, TypeError):
                            pass
            except Exception as exc:
                _LOGGER.debug("Parse error for %s: %s", prod, exc)

        if "wind_speed" in result:
            result["wind_speed"] = round(result["wind_speed"] * 3.6, 1)

        if "temperature" in result:
            t = result["temperature"]
            for threshold, cond in CONDITION_FROM_TEMP:
                if t >= threshold:
                    result["condition"] = cond
                    result["icon"] = CDC_ICON_MAP.get(cond, "mdi:weather-cloudy")
                    break
        return result

    def _generate_radar_frames(self) -> dict[str, list[str]]:
        now = datetime.now(timezone.utc)
        radar: list[str] = []
        forecast: list[str] = []

        for i in range(24, 0, -1):
            radar.append((now - timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        for i in range(24):
            radar.append((now + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        for i in range(14):
            forecast.append((now + timedelta(hours=1 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"))

        return {"past": radar[:24], "nowcast": radar[24:], "forecast": forecast}

    async def _async_update_data(self) -> dict:
        try:
            if not self.stations:
                self.stations = await fetch_stations(self.session)
                _LOGGER.info("Loaded %d DWD stations", len(self.stations))

            station_map: dict[str, tuple[str, str, DWDStation]] = {}
            zone_entities = self._normalize_entity_list(self.entry.options.get(CONF_ZONES))
            if not zone_entities:
                for loc in self.locations:
                    lat = loc.get(CONF_LATITUDE)
                    lon = loc.get(CONF_LONGITUDE)
                    if lat is not None and lon is not None:
                        nearest = find_nearest_station(lat, lon, self.stations)
                        if nearest is not None:
                            loc_name = loc.get(CONF_NAME, "unknown")
                            station_map[loc_name] = (loc_name, "manual", nearest)

            for zone_entity in zone_entities:
                zone_state = self.hass.states.get(zone_entity)
                if zone_state is None:
                    continue
                zlat = zone_state.attributes.get(CONF_LATITUDE)
                zlon = zone_state.attributes.get(CONF_LONGITUDE)
                if zlat is None or zlon is None:
                    continue
                zone_name = zone_state.attributes.get("friendly_name", zone_entity)
                znearest = find_nearest_station(float(zlat), float(zlon), self.stations)
                if znearest is not None:
                    station_map[f"zone::{zone_entity}"] = (
                        zone_name,
                        zone_entity,
                        znearest,
                    )

            tracker_entities = self._normalize_entity_list(
                self.entry.options.get(CONF_DEVICE_TRACKERS)
            )
            if not tracker_entities:
                tracker_entities = self._normalize_entity_list(
                    self.entry.options.get(CONF_DEVICE_TRACKER)
                )

            for tracker_entity in tracker_entities:
                tracker_state = self.hass.states.get(tracker_entity)
                if tracker_state is not None:
                    tlat = tracker_state.attributes.get("latitude")
                    tlon = tracker_state.attributes.get("longitude")
                    if tlat is not None and tlon is not None:
                        tname = tracker_state.attributes.get("friendly_name", tracker_entity)
                        if not isinstance(tname, str):
                            tname = self.entry.options.get(
                                CONF_TRACKED_LOCATION_NAME,
                                tracker_entity,
                            )
                        tnearest = find_nearest_station(tlat, tlon, self.stations)
                        if tnearest is not None:
                            station_map[f"tracker::{tracker_entity}"] = (
                                tname,
                                tracker_entity,
                                tnearest,
                            )

            station_ids = list({entry[2].station_id for entry in station_map.values()})
            obs_tasks = {sid: asyncio.create_task(self._fetch_obs(sid)) for sid in station_ids}
            obs_results: dict[str, dict] = {}
            for sid, task in obs_tasks.items():
                obs_results[sid] = await task

            self.radar_frames = self._generate_radar_frames()

            result: dict[str, dict] = {}
            for loc_key, (loc_name, source_entity, station) in station_map.items():
                loc_data = obs_results.get(station.station_id, {})
                result[loc_key] = {
                    "location_name": loc_name,
                    "source_entity": source_entity,
                    "station_id": station.station_id,
                    "station_name": station.name,
                    "station_distance_km": station.distance_km,
                    **loc_data,
                }

            return {
                "locations": result,
                "radar_frames": self.radar_frames,
                "stations_count": len(self.stations),
                "last_update": datetime.now(timezone.utc).isoformat(),
            }

        except Exception as exc:
            raise UpdateFailed(f"Failed to update DWD data: {exc}") from exc

    async def async_close(self):
        if not self._session.closed:
            await self._session.close()
