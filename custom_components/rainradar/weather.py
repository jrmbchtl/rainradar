from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.weather import (
    WeatherEntity,
    Forecast,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    CONF_LOCATIONS,
    CONF_NAME,
    CONF_DEVICE_TRACKER,
    CONF_DEVICE_TRACKERS,
    CONF_ZONES,
    CONF_TRACKED_LOCATION_NAME,
    INTEGRATION_VERSION,
    location_slug,
    condition_from_dwd_ww,
)
from .coordinator import RainradarCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[WeatherEntity] = []

    zones = entry.options.get(CONF_ZONES, [])
    location_specs: list[tuple[str, str, str]] = []

    if zones:
        for zone_entity in zones:
            zone_state = hass.states.get(zone_entity)
            zone_name = (
                zone_state.attributes.get("friendly_name", zone_entity)
                if zone_state
                else zone_entity
            )
            slug = location_slug(zone_entity)
            location_specs.append((f"zone::{zone_entity}", zone_name, slug))
    else:
        for loc in entry.options.get(CONF_LOCATIONS, []):
            loc_name = loc.get(CONF_NAME, "unknown")
            slug = location_slug(loc_name)
            location_specs.append((f"loc::{loc_name}", loc_name, slug))

    trackers = entry.options.get(CONF_DEVICE_TRACKERS, [])
    if not trackers:
        tracker = entry.options.get(CONF_DEVICE_TRACKER)
        if tracker:
            trackers = [tracker]

    for tracker_entity in trackers:
        tracker_state = hass.states.get(tracker_entity)
        tracked_name = (
            tracker_state.attributes.get("friendly_name", tracker_entity)
            if tracker_state
            else entry.options.get(CONF_TRACKED_LOCATION_NAME, tracker_entity)
        )
        slug = location_slug(tracker_entity)
        location_specs.append((f"tracker::{tracker_entity}", tracked_name, slug))

    for loc_key, loc_name, slug in location_specs:
        entities.append(
            RainradarWeatherEntity(
                coordinator,
                entry,
                loc_key,
                loc_name,
                slug,
            )
        )

    async_add_entities(entities)


class RainradarWeatherEntity(CoordinatorEntity, WeatherEntity):
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_pressure_unit = UnitOfPressure.HPA
    _attr_native_wind_speed_unit = UnitOfSpeed.KILOMETERS_PER_HOUR
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RainradarCoordinator,
        entry: ConfigEntry,
        loc_key: str,
        loc_name: str,
        slug: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._loc_key = loc_key
        self._loc_name = loc_name
        self._slug = slug
        self._attr_unique_id = f"{DOMAIN}_weather_{slug}"
        self._attr_name = loc_name
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{entry.entry_id}_{slug}")},
            "name": f"Rainradar {loc_name}",
            "manufacturer": "DWD",
            "model": "Weather Station",
            "sw_version": INTEGRATION_VERSION,
        }

    @property
    def available(self) -> bool:
        if not (self.coordinator.last_update_success and self.coordinator.data):
            return False
        return bool(
            self.coordinator.data.get("locations", {}).get(self._loc_key, {}).get("temperature")
        )

    def _loc_data(self) -> dict:
        return self.coordinator.data.get("locations", {}).get(self._loc_key, {})

    @property
    def condition(self):
        loc = self._loc_data()
        code = loc.get("weather_code")
        if code is not None and isinstance(code, (int, float)):
            return condition_from_dwd_ww(int(code))
        return loc.get("condition")

    @property
    def native_temperature(self):
        return self._loc_data().get("temperature")

    @property
    def native_temperature_feels_like(self):
        return self._loc_data().get("apparent_temperature")

    @property
    def native_humidity(self):
        return self._loc_data().get("humidity")

    @property
    def native_pressure(self):
        return self._loc_data().get("pressure")

    @property
    def native_wind_speed(self):
        return self._loc_data().get("wind_speed")

    @property
    def native_wind_bearing(self):
        return self._loc_data().get("wind_direction")

    @property
    def native_wind_gust_speed(self):
        return self._loc_data().get("wind_gust")

    @property
    def native_precipitation_unit(self):
        return "mm/h"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        loc_data = self._loc_data()
        attrs = {
            "station_name": loc_data.get("station_name"),
            "station_distance_km": loc_data.get("station_distance_km"),
            "station_id": loc_data.get("station_id"),
            "source_entity": loc_data.get("source_entity"),
        }
        return {k: v for k, v in attrs.items() if v is not None}

    @property
    def forecast(self) -> Forecast | None:
        loc_data = self._loc_data()
        raw_forecast = loc_data.get("forecast")
        if not raw_forecast:
            return None

        result: Forecast = []
        for fc in raw_forecast:
            ts = fc.get("ts")
            if ts is None:
                continue
            entry: dict[str, Any] = {
                "datetime": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            }
            if "temperature" in fc:
                entry["temperature"] = fc["temperature"]
            if "temp_min" in fc:
                entry["templow"] = fc["temp_min"]
            if "temp_max" in fc:
                entry["temperature"] = fc.get("temperature", fc["temp_max"])
            if "precipitation" in fc:
                entry["precipitation"] = fc["precipitation"]
            if "precip_probability" in fc:
                entry["precipitation_probability"] = int(fc["precip_probability"])
            if "wind_speed" in fc:
                entry["wind_speed"] = fc["wind_speed"]
            if "wind_direction" in fc:
                entry["wind_bearing"] = fc["wind_direction"]
            if "wind_gust" in fc:
                entry["wind_gust"] = fc["wind_gust"]
            if "cloud_cover" in fc:
                entry["cloud_coverage"] = fc["cloud_cover"]
            if "weather_code" in fc:
                try:
                    entry["condition"] = condition_from_dwd_ww(int(fc["weather_code"]))
                except (ValueError, TypeError):
                    pass
            result.append(entry)

        return result
