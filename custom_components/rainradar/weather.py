from __future__ import annotations

from homeassistant.components.weather import WeatherEntity
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
)
from .coordinator import RainradarCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[WeatherEntity] = []

    def add_weather_entity(loc_key: str, loc_name: str, safe_name: str) -> None:
        entities.append(
            RainradarWeatherEntity(
                coordinator,
                entry,
                loc_key,
                loc_name,
                safe_name,
            )
        )

    zones = entry.options.get(CONF_ZONES, [])
    if zones:
        for zone_entity in zones:
            zone_state = hass.states.get(zone_entity)
            zone_name = (
                zone_state.attributes.get("friendly_name", zone_entity)
                if zone_state
                else zone_entity
            )
            safe_zone = zone_entity.replace(".", "_").replace("-", "_")
            add_weather_entity(f"zone::{zone_entity}", zone_name, safe_zone)
    else:
        for loc in entry.options.get(CONF_LOCATIONS, []):
            loc_name = loc.get(CONF_NAME, "unknown")
            safe_name = loc_name.lower().replace(" ", "_").replace("-", "_")
            add_weather_entity(loc_name, loc_name, safe_name)

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
        safe_tracked = tracker_entity.replace(".", "_").replace("-", "_")
        add_weather_entity(f"tracker::{tracker_entity}", tracked_name, safe_tracked)

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
        safe_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._loc_key = loc_key
        self._loc_name = loc_name
        self._safe_name = safe_name
        self._attr_unique_id = f"{DOMAIN}_weather_{safe_name}"
        self._attr_name = loc_name
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{entry.entry_id}_{safe_name}")},
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
        return self._loc_data().get("condition")

    @property
    def native_temperature(self):
        return self._loc_data().get("temperature")

    @property
    def native_humidity(self):
        return self._loc_data().get("humidity")

    @property
    def native_pressure(self):
        return None

    @property
    def native_wind_speed(self):
        return self._loc_data().get("wind_speed")

    @property
    def native_wind_bearing(self):
        return self._loc_data().get("wind_direction")

    @property
    def extra_state_attributes(self) -> dict | None:
        loc_data = self._loc_data()
        attrs = {
            "station_name": loc_data.get("station_name"),
            "station_distance_km": loc_data.get("station_distance_km"),
            "station_id": loc_data.get("station_id"),
            "source_entity": loc_data.get("source_entity"),
        }
        return {k: v for k, v in attrs.items() if v is not None}

    @property
    def forecast(self):
        return None
