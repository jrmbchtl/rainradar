from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
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
    ATTR_PRECIP_PROBABILITY,
    ATTR_RAIN_RATE,
    ATTR_SNOW_RATE,
    ATTR_FRESH_SNOW,
    ATTR_RAIN_24H,
    ATTR_SNOW_24H,
    ATTR_SOLAR_RADIATION,
    ATTR_SUNSHINE_DURATION,
    ATTR_VISIBILITY,
    ATTR_WEATHER_CODE,
    ATTR_WEATHER_CODE_TEXT,
    ATTR_APPARENT_TEMPERATURE,
    ATTR_UV_INDEX,
    ATTR_UV_INDEX_MAX,
    ATTR_CONDITION,
    ATTR_STATION_NAME,
    ATTR_STATION_ID,
    ATTR_STATION_DISTANCE,
    ATTR_SOURCE_ENTITY,
    ATTR_LAST_UPDATE,
    ATTR_FRAME_ERROR,
    ATTR_WARNING_LEVEL,
    ATTR_WARNING_HEADLINE,
    ATTR_WARNING_COUNT,
    ATTR_RAIN_SLOTS,
    ATTR_RAIN_2H_TOTAL,
    resolve_location_specs,
)
from .weather_coordinator import WeatherDataCoordinator
from .radar_coordinator import RadarDataCoordinator

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
    "precip_probability": ATTR_PRECIP_PROBABILITY,
    "rain_rate": ATTR_RAIN_RATE,
    "snow_rate": ATTR_SNOW_RATE,
    "fresh_snow": ATTR_FRESH_SNOW,
    "rain_24h": ATTR_RAIN_24H,
    "snow_24h": ATTR_SNOW_24H,
    "solar_radiation": ATTR_SOLAR_RADIATION,
    "sunshine_duration": ATTR_SUNSHINE_DURATION,
    "visibility": ATTR_VISIBILITY,
    "weather_code": ATTR_WEATHER_CODE,
    "weather_code_text": ATTR_WEATHER_CODE_TEXT,
    "apparent_temperature": ATTR_APPARENT_TEMPERATURE,
    "uv_index": ATTR_UV_INDEX,
    "uv_index_max": ATTR_UV_INDEX_MAX,
    "condition": ATTR_CONDITION,
    "station_name": ATTR_STATION_NAME,
    "station_id": ATTR_STATION_ID,
    "station_distance": ATTR_STATION_DISTANCE,
    "rain_slots": ATTR_RAIN_SLOTS,
    "rain_2h_total": ATTR_RAIN_2H_TOTAL,
    "warning_level": ATTR_WARNING_LEVEL,
    "warning_headline": ATTR_WARNING_HEADLINE,
    "warning_count": ATTR_WARNING_COUNT,
}

CORE_SENSORS = ("temperature", "humidity", "wind_speed", "wind_direction", "condition")
OPTIONAL_SENSORS = (
    "pressure", "dew_point", "cloud_cover", "wind_gust",
    "precipitation", "precip_probability",
    "rain_rate", "snow_rate", "fresh_snow",
    "rain_24h", "snow_24h",
    "solar_radiation", "sunshine_duration",
    "visibility", "weather_code", "weather_code_text",
    "apparent_temperature",
    "uv_index", "uv_index_max",
    "rain_2h_total",
    "warning_level", "warning_headline", "warning_count",
)
DEBUG_STATION_SENSORS = ("station_name", "station_id", "station_distance")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    weather_coordinator: WeatherDataCoordinator = hass.data[DOMAIN][entry.entry_id]
    radar_coordinator: RadarDataCoordinator = hass.data[DOMAIN][f"{entry.entry_id}_radar"]
    entities: list[SensorEntity] = []

    entities.append(
        RainradarFramesSensor(
            radar_coordinator,
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
            weather_coordinator,
            entry,
            SensorEntityDescription(
                key="stations",
                name="Stations",
                icon="mdi:weather-cloudy",
            ),
        )
    )

    location_specs = resolve_location_specs(hass, entry)
    for loc_key, loc_name, _source_entity, _lat, _lon, slug in location_specs:
        for sensor_key in CORE_SENSORS + OPTIONAL_SENSORS:
            if sensor_key not in SENSOR_TYPES:
                continue
            desc = SENSOR_TYPES[sensor_key]
            entities.append(
                RainradarLocationSensor(
                    weather_coordinator,
                    radar_coordinator,
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
        entities.append(
            RainradarRainSlotsSensor(
                radar_coordinator, entry, loc_key, loc_name, slug,
            )
        )

        for sensor_key in DEBUG_STATION_SENSORS:
            desc = SENSOR_TYPES[sensor_key]
            entities.append(
                RainradarDebugStationSensor(
                    weather_coordinator,
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

    entities.append(
        RainradarHealthSensor(
            weather_coordinator,
            entry,
            SensorEntityDescription(
                key="weather_health",
                name="Weather Health",
                icon="mdi:heart-pulse",
            ),
            "weather_coordinator",
        )
    )
    entities.append(
        RainradarHealthSensor(
            radar_coordinator,
            entry,
            SensorEntityDescription(
                key="radar_health",
                name="Radar Health",
                icon="mdi:radar",
            ),
            "radar_coordinator",
        )
    )

    _cleanup_deprecated_entities(hass, [s[5] for s in location_specs])
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
        coordinator: RadarDataCoordinator,
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
        coordinator: WeatherDataCoordinator,
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
        coordinator: WeatherDataCoordinator,
        radar_coordinator: RadarDataCoordinator,
        entry: ConfigEntry,
        loc_key: str,
        loc_name: str,
        slug: str,
        description: SensorEntityDescription,
        sensor_key: str,
    ) -> None:
        super().__init__(coordinator)
        self._radar_coordinator = radar_coordinator
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
        val = loc_data.get(field)
        if val is not None:
            return val
        if self._radar_coordinator and self._radar_coordinator.data:
            radar_loc = self._radar_coordinator.data.get("locations", {}).get(self._loc_key, {})
            return radar_loc.get(field)
        return None

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


class RainradarDebugStationSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WeatherDataCoordinator,
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
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{entry.entry_id}_debug")},
            "name": "Rainradar Debug",
            "manufacturer": "DWD",
            "model": "Debug Info",
            "sw_version": INTEGRATION_VERSION,
        }

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


class RainradarRainSlotsSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        radar_coordinator: RadarDataCoordinator,
        entry: ConfigEntry,
        loc_key: str,
        loc_name: str,
        slug: str,
    ) -> None:
        super().__init__(radar_coordinator)
        self._loc_key = loc_key
        self._loc_name = loc_name
        self._slug = slug
        self._attr_unique_id = f"{DOMAIN}_{slug}_rain_slots"
        self._attr_device_info = _common_device_info(
            entry, slug, f"Rainradar {loc_name}", "Weather Station"
        )

    @property
    def native_value(self):
        data = self.coordinator.data
        if not data:
            return None
        loc_data = data.get("locations", {}).get(self._loc_key, {})
        slots = loc_data.get("rain_slots", [])
        return len(slots) if isinstance(slots, list) else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if not data:
            return None
        loc_data = data.get("locations", {}).get(self._loc_key, {})
        slots = loc_data.get("rain_slots", [])
        if not isinstance(slots, list):
            slots = []
        next_ts = slots[0].get("start") if slots else None
        return {"slots": slots, "next_rain": next_ts}


class RainradarHealthSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator,
        entry: ConfigEntry,
        description: SensorEntityDescription,
        coordinator_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._coordinator_name = coordinator_name
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_{description.key}"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{entry.entry_id}_debug")},
            "name": "Rainradar Debug",
            "manufacturer": "DWD",
            "model": "Debug Info",
            "sw_version": INTEGRATION_VERSION,
        }

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self):
        return self.coordinator.last_update_success
