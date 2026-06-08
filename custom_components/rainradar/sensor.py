from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import entity_registry as er
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
    SENSOR_TYPES,
    ATTR_TEMPERATURE,
    ATTR_HUMIDITY,
    ATTR_WIND_SPEED,
    ATTR_WIND_DIRECTION,
    ATTR_WIND_GUST,
    ATTR_PRESSURE,
    ATTR_DEW_POINT,
    ATTR_CLOUD_COVERAGE,
    ATTR_PRECIPITATION,
    ATTR_PRECIP_INTENSITY,
    ATTR_PRECIP_PROBABILITY,
    ATTR_RAIN_RATE,
    ATTR_SNOW_RATE,
    ATTR_FRESH_SNOW,
    ATTR_SNOW_DEPTH,
    ATTR_RAIN_24H,
    ATTR_SNOW_24H,
    ATTR_SOLAR_RADIATION,
    ATTR_SUNSHINE_DURATION,
    ATTR_VISIBILITY,
    ATTR_WEATHER_CODE,
    ATTR_APPARENT_TEMPERATURE,
    ATTR_UV_INDEX,
    ATTR_UV_INDEX_MAX,
    ATTR_RADOLAN_PRECIPITATION,
    ATTR_CONDITION,
    ATTR_STATION_NAME,
    ATTR_STATION_ID,
    ATTR_STATION_DISTANCE,
    ATTR_SOURCE_ENTITY,
    ATTR_LAST_UPDATE,
    ATTR_FRAME_ERROR,
    location_slug,
)
from .coordinator import RainradarCoordinator

SENSOR_KEY_MAP = {
    "temperature": ATTR_TEMPERATURE,
    "humidity": ATTR_HUMIDITY,
    "pressure": ATTR_PRESSURE,
    "dew_point": ATTR_DEW_POINT,
    "cloud_cover": ATTR_CLOUD_COVERAGE,
    "wind_speed": ATTR_WIND_SPEED,
    "wind_direction": ATTR_WIND_DIRECTION,
    "wind_gust": ATTR_WIND_GUST,
    "precipitation": ATTR_PRECIPITATION,
    "precip_intensity": ATTR_PRECIP_INTENSITY,
    "precip_probability": ATTR_PRECIP_PROBABILITY,
    "rain_rate": ATTR_RAIN_RATE,
    "snow_rate": ATTR_SNOW_RATE,
    "fresh_snow": ATTR_FRESH_SNOW,
    "snow_depth": ATTR_SNOW_DEPTH,
    "rain_24h": ATTR_RAIN_24H,
    "snow_24h": ATTR_SNOW_24H,
    "solar_radiation": ATTR_SOLAR_RADIATION,
    "sunshine_duration": ATTR_SUNSHINE_DURATION,
    "visibility": ATTR_VISIBILITY,
    "weather_code": ATTR_WEATHER_CODE,
    "apparent_temperature": ATTR_APPARENT_TEMPERATURE,
    "uv_index": ATTR_UV_INDEX,
    "uv_index_max": ATTR_UV_INDEX_MAX,
    "radolan_precipitation": ATTR_RADOLAN_PRECIPITATION,
    "condition": ATTR_CONDITION,
}

CORE_SENSORS = ("temperature", "humidity", "wind_speed", "wind_direction", "condition")
OPTIONAL_SENSORS = (
    "pressure", "dew_point", "cloud_cover", "wind_gust",
    "precipitation", "precip_intensity", "precip_probability",
    "rain_rate", "snow_rate", "fresh_snow", "snow_depth",
    "rain_24h", "snow_24h",
    "solar_radiation", "sunshine_duration",
    "visibility", "weather_code",
    "apparent_temperature",
    "uv_index", "uv_index_max",
    "radolan_precipitation",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: RainradarCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []

    entities.append(
        RainradarFramesSensor(
            coordinator,
            entry,
            SensorEntityDescription(
                key="radar_frames",
                name="Radar Frames",
                icon="mdi:radar",
            ),
        )
    )
    entities.append(
        RainradarStationsSensor(
            coordinator,
            entry,
            SensorEntityDescription(
                key="stations",
                name="Stations",
                icon="mdi:weather-cloudy",
            ),
        )
    )

    zones = entry.options.get(CONF_ZONES, [])
    location_specs: list[tuple[str, str, str]] = []

    if not zones:
        for loc in entry.options.get(CONF_LOCATIONS, []):
            loc_name = loc.get(CONF_NAME, "unknown")
            slug = location_slug(loc_name)
            location_specs.append((f"loc::{loc_name}", loc_name, slug))

    for zone_entity in zones:
        zone_state = hass.states.get(zone_entity)
        zone_name = (
            zone_state.attributes.get("friendly_name", zone_entity)
            if zone_state
            else zone_entity
        )
        slug = location_slug(zone_entity)
        location_specs.append((f"zone::{zone_entity}", zone_name, slug))

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
        for sensor_key in CORE_SENSORS + OPTIONAL_SENSORS:
            if sensor_key not in SENSOR_TYPES:
                continue
            desc = SENSOR_TYPES[sensor_key]
            entities.append(
                RainradarLocationSensor(
                    coordinator,
                    entry,
                    loc_key,
                    loc_name,
                    slug,
                    SensorEntityDescription(
                        key=f"{sensor_key}_{slug}",
                        name=f"{loc_name} {sensor_key.replace('_', ' ').title()}",
                        native_unit_of_measurement=desc.get("unit"),
                        icon=desc.get("icon"),
                        device_class=desc.get("device_class"),
                        state_class=desc.get("state_class"),
                    ),
                    sensor_key,
                )
            )

    _cleanup_deprecated_entities(hass, [s[2] for s in location_specs])
    async_add_entities(entities)


def _cleanup_deprecated_entities(hass: HomeAssistant, slugs: list[str]) -> None:
    registry = er.async_get(hass)
    for slug in slugs:
        for old_key in ("pressure", "alerts", "sunshine"):
            unique_id = f"{DOMAIN}_{slug}_{old_key}"
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
            if entity_id is not None:
                registry.async_remove(entity_id)


def _common_device_info(entry: ConfigEntry, suffix: str, name: str, model: str) -> dict:
    return {
        "identifiers": {(DOMAIN, f"{entry.entry_id}_{suffix}")},
        "name": name,
        "manufacturer": "DWD",
        "model": model,
        "sw_version": INTEGRATION_VERSION,
    }


class RainradarFramesSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RainradarCoordinator,
        entry: ConfigEntry,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_radar_frames"
        self._attr_device_info = _common_device_info(
            entry, "summary", "Rainradar", "Weather Data"
        )

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.data is not None

    @property
    def native_value(self):
        data = self.coordinator.data
        if not data:
            return None
        radar_frames = data.get("radar_frames") or {}
        return (
            len(radar_frames.get("past", []))
            + len(radar_frames.get("nowcast", []))
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if not data:
            return None
        return {
            "frames": data.get("radar_frames") or {},
            "last_update": data.get("last_update"),
            "frame_error": data.get("frame_error"),
        }


class RainradarStationsSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RainradarCoordinator,
        entry: ConfigEntry,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_stations"
        self._attr_device_info = _common_device_info(
            entry, "summary", "Rainradar", "Weather Data"
        )

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.data is not None

    @property
    def native_value(self):
        data = self.coordinator.data
        if not data:
            return None
        return data.get("stations_count")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return None


class RainradarLocationSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RainradarCoordinator,
        entry: ConfigEntry,
        loc_key: str,
        loc_name: str,
        slug: str,
        description: SensorEntityDescription,
        sensor_key: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._loc_key = loc_key
        self._loc_name = loc_name
        self._slug = slug
        self._sensor_key = sensor_key
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_{slug}_{sensor_key}"
        self._attr_device_info = _common_device_info(
            entry, slug, f"Rainradar {loc_name}", "Weather Station"
        )

    @property
    def available(self) -> bool:
        if not (self.coordinator.last_update_success and self.coordinator.data):
            return False
        return self._loc_key in (self.coordinator.data.get("locations") or {})

    @property
    def native_value(self):
        data = self.coordinator.data
        if data is None:
            return None
        loc_data = data.get("locations", {}).get(self._loc_key, {})
        field = SENSOR_KEY_MAP.get(self._sensor_key)
        if field is None:
            return None
        return loc_data.get(field)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if data is None:
            return None
        loc_data = data.get("locations", {}).get(self._loc_key, {})
        attrs = {
            ATTR_STATION_NAME: loc_data.get(ATTR_STATION_NAME),
            ATTR_STATION_DISTANCE: loc_data.get(ATTR_STATION_DISTANCE),
            ATTR_STATION_ID: loc_data.get(ATTR_STATION_ID),
            ATTR_SOURCE_ENTITY: loc_data.get(ATTR_SOURCE_ENTITY),
        }
        return {k: v for k, v in attrs.items() if v is not None}
