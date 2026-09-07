from __future__ import annotations

from datetime import UTC
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_APPARENT_TEMPERATURE,
    ATTR_CLOUD_COVERAGE,
    ATTR_CONDITION,
    ATTR_DEW_POINT,
    ATTR_FRESH_SNOW,
    ATTR_HUMIDITY,
    ATTR_PRECIP_PROBABILITY,
    ATTR_PRECIPITATION,
    ATTR_PRESSURE,
    ATTR_RAIN_2H_TOTAL,
    ATTR_RAIN_24H,
    ATTR_RAIN_RATE,
    ATTR_RAIN_SLOTS,
    ATTR_SNOW_24H,
    ATTR_SNOW_RATE,
    ATTR_SOLAR_RADIATION,
    ATTR_SOURCE_ENTITY,
    ATTR_STATION_DISTANCE,
    ATTR_STATION_ID,
    ATTR_STATION_NAME,
    ATTR_SUNSHINE_DURATION,
    ATTR_TEMPERATURE,
    ATTR_TEMPERATURE_FORECAST,
    ATTR_UV_INDEX,
    ATTR_UV_INDEX_MAX,
    ATTR_VISIBILITY,
    ATTR_WARNING_COUNT,
    ATTR_WARNING_HEADLINE,
    ATTR_WARNING_LEVEL,
    ATTR_WEATHER_CODE,
    ATTR_WEATHER_CODE_TEXT,
    ATTR_WIND_DIRECTION,
    ATTR_WIND_GUST,
    ATTR_WIND_SPEED,
    CONF_ENABLE_PKG_PROBABILITY,
    CONF_ENABLE_PKG_SOLAR,
    CONF_ENABLE_PKG_WIND,
    DOMAIN,
    INTEGRATION_VERSION,
    SENSOR_TYPES,
    resolve_location_specs,
)
from .radar_coordinator import RadarDataCoordinator
from .weather_coordinator import WeatherDataCoordinator

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
    "ozone": "ozone",
    "soil_temp_2cm": "soil_temp_2cm",
    "soil_temp_5cm": "soil_temp_5cm",
    "soil_temp_10cm": "soil_temp_10cm",
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
    "ozone",
    "soil_temp_2cm", "soil_temp_5cm", "soil_temp_10cm",
)
DEBUG_STATION_SENSORS = ("station_name", "station_id", "station_distance")

# CAMS all-sky UV sensors (actual UV only — no clear-sky).
CAMS_UV_SENSORS = ("uv_index", "uv_index_max_today")
# WeatherNext 3 package sensors (raw variables only).
WN_SOLAR_SENSORS = ("solar_ghi", "solar_direct", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high")
WN_WIND_SENSORS = ("wind_speed_100m", "wind_direction_100m")
WN_PROBABILITY_SENSORS = ("rain_risk_24h", "frost_risk_24h", "heat_risk_24h")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    weather_coordinator = runtime.weather_coordinator
    radar_coordinator = runtime.radar_coordinator
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
    cams_coord = getattr(runtime, "cams_coordinator", None)
    for loc in location_specs:
        for sensor_key in CORE_SENSORS + OPTIONAL_SENSORS:
            # CAMS UV owns the uv_index sensor when enabled.
            if sensor_key == "uv_index" and cams_coord is not None:
                continue
            if sensor_key not in SENSOR_TYPES:
                continue
            desc = SENSOR_TYPES[sensor_key]
            entities.append(
                RainradarLocationSensor(
                    weather_coordinator,
                    radar_coordinator,
                    entry,
                    loc.loc_key,
                    loc.name,
                    loc.slug,
                    SensorEntityDescription(
                        key=f"{sensor_key}_{loc.slug}",
                        name=f"{loc.name} {sensor_key.replace('_', ' ').title()}",
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
                radar_coordinator, entry, loc.loc_key, loc.name, loc.slug,
            )
        )

        entities.append(
            RainradarTemperatureForecastSensor(
                radar_coordinator, entry, loc.loc_key, loc.name, loc.slug,
            )
        )

        # Experimental packages (WeatherNext 3 / CAMS) — created only when
        # their coordinators exist in runtime_data.
        wn_coord = getattr(runtime, "weathernext_coordinator", None)
        if cams_coord is not None:
            for sensor_key in CAMS_UV_SENSORS:
                desc = SENSOR_TYPES.get(sensor_key, {})
                entities.append(
                    RainradarCamsSensor(
                        cams_coord,
                        entry,
                        loc.loc_key,
                        loc.name,
                        loc.slug,
                        SensorEntityDescription(
                            key=f"{sensor_key}_{loc.slug}",
                            name=f"{loc.name} {sensor_key.replace('_', ' ').title()}",
                            native_unit_of_measurement=desc.get("unit"),
                            icon=desc.get("icon"),
                            device_class=desc.get("device_class"),
                            state_class=desc.get("state_class"),
                        ),
                        sensor_key,
                    )
                )
        if wn_coord is not None:
            package_sensors: list[str] = []
            if entry.options.get(CONF_ENABLE_PKG_SOLAR, False):
                package_sensors += WN_SOLAR_SENSORS
            if entry.options.get(CONF_ENABLE_PKG_WIND, False):
                package_sensors += WN_WIND_SENSORS
            if entry.options.get(CONF_ENABLE_PKG_PROBABILITY, False):
                package_sensors += WN_PROBABILITY_SENSORS
            for sensor_key in package_sensors:
                desc = SENSOR_TYPES[sensor_key]
                entities.append(
                    RainradarWnSensor(
                        wn_coord,
                        entry,
                        loc.loc_key,
                        loc.name,
                        loc.slug,
                        SensorEntityDescription(
                            key=f"{sensor_key}_{loc.slug}",
                            name=f"{loc.name} {sensor_key.replace('_', ' ').title()}",
                            native_unit_of_measurement=desc.get("unit"),
                            icon=desc.get("icon"),
                            device_class=desc.get("device_class"),
                            state_class=desc.get("state_class"),
                        ),
                        sensor_key,
                    )
                )

        for sensor_key in DEBUG_STATION_SENSORS:
            desc = SENSOR_TYPES[sensor_key]
            entities.append(
                RainradarDebugStationSensor(
                    weather_coordinator,
                    entry,
                    loc.loc_key,
                    loc.name,
                    loc.slug,
                    SensorEntityDescription(
                        key=f"{sensor_key}_{loc.slug}",
                        name=f"{loc.name} {sensor_key.replace('_', ' ').title()}",
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

    _cleanup_deprecated_entities(hass, [loc.slug for loc in location_specs])
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
    _unrecorded_attributes = frozenset({"frames"})

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
    _unrecorded_attributes = frozenset({"slots"})

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


class RainradarTemperatureForecastSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({"forecast"})

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
        self._attr_unique_id = f"{DOMAIN}_{slug}_temperature_forecast_4h"
        self._attr_device_info = _common_device_info(
            entry, slug, f"Rainradar {loc_name}", "Weather Station"
        )
        self._attr_native_unit_of_measurement = "°C"
        self._attr_icon = "mdi:thermometer-chevron-up"
        self._attr_device_class = "temperature"
        self._attr_state_class = "measurement"

    @property
    def native_value(self):
        data = self.coordinator.data
        if not data:
            return None
        loc_data = data.get("locations", {}).get(self._loc_key, {})
        forecast = loc_data.get(ATTR_TEMPERATURE_FORECAST, [])
        if isinstance(forecast, list) and len(forecast) > 0:
            return forecast[0].get("temperature")
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if not data:
            return None
        loc_data = data.get("locations", {}).get(self._loc_key, {})
        forecast = loc_data.get(ATTR_TEMPERATURE_FORECAST, [])
        if not isinstance(forecast, list):
            forecast = []
        return {"forecast": forecast}


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
    def native_value(self) -> str:
        ok = bool(self.coordinator.last_update_success) and bool(
            getattr(self.coordinator, "health_state", True)
        )
        return "on" if ok else "off"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return {
            "last_update_success": self.coordinator.last_update_success,
            "health_state": getattr(self.coordinator, "health_state", True),
        }


def _hourly_value_at(entries: list[dict], key: str, max_age_h: float = 2.0) -> float | None:
    """Return the value of `key` at the hourly entry closest to now."""
    from datetime import datetime as _dt

    if not entries:
        return None
    now_ts = _dt.now(UTC).timestamp()
    best = None
    best_delta = float("inf")
    for entry in entries:
        delta = abs(entry.get("ts", 0) - now_ts)
        if delta < best_delta:
            best_delta = delta
            best = entry
    if best is None or best_delta > max_age_h * 3600:
        return None
    return best.get(key)


class RainradarCamsSensor(CoordinatorEntity, SensorEntity):
    """Per-location all-sky UV index from CAMS (actual conditions)."""

    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({"hourly_uv"})

    def __init__(
        self,
        coordinator,
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
        self._sensor_key = sensor_key
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_{slug}_{sensor_key}"
        self._attr_device_info = _common_device_info(
            entry, slug, f"Rainradar {loc_name}", "Weather Station"
        )

    def _series(self) -> list[dict]:
        data = self.coordinator.data or {}
        return data.get("locations", {}).get(self._loc_key, [])

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and bool(self._series())

    @property
    def native_value(self):
        series = self._series()
        if self._sensor_key == "uv_index":
            return _hourly_value_at(series, "uv_index", max_age_h=3.0)
        if self._sensor_key == "uv_index_max_today":
            from datetime import datetime as _dt

            now = _dt.now(UTC)
            today0 = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            tomorrow0 = today0 + 86400
            vals = [
                e["uv_index"]
                for e in series
                if today0 <= e.get("ts", 0) < tomorrow0 and e.get("uv_index") is not None
            ]
            return round(max(vals), 1) if vals else None
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self._sensor_key == "uv_index":
            return {"hourly_uv": self._series()}
        return None


class RainradarWnSensor(CoordinatorEntity, SensorEntity):
    """Per-location WeatherNext 3 package sensor (raw variables)."""

    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({"p10", "p90", "init_time"})

    def __init__(
        self,
        coordinator,
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
        self._sensor_key = sensor_key
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_{slug}_{sensor_key}"
        self._attr_device_info = _common_device_info(
            entry, slug, f"Rainradar {loc_name}", "Weather Station"
        )

    def _hourly(self) -> list[dict]:
        data = self.coordinator.data or {}
        return data.get("locations", {}).get(self._loc_key, {}).get("hourly", [])

    def _stats(self) -> dict[str, list[dict]]:
        data = self.coordinator.data or {}
        return (
            data.get("locations", {}).get(self._loc_key, {}).get("hourly_stats", {})
        )

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and bool(self._hourly())

    @property
    def native_value(self):
        key_map = {
            "solar_ghi": "solar_ghi",
            "solar_direct": "solar_direct",
            "cloud_cover_low": "cloud_cover_low",
            "cloud_cover_mid": "cloud_cover_mid",
            "cloud_cover_high": "cloud_cover_high",
            "wind_speed_100m": "wind_speed_100m",
            "wind_direction_100m": "wind_direction_100m",
        }
        if self._sensor_key in key_map:
            return _hourly_value_at(self._hourly(), key_map[self._sensor_key])
        if self._sensor_key in ("rain_risk_24h", "frost_risk_24h", "heat_risk_24h"):
            return self._risk_value()
        return None

    def _risk_value(self) -> float | None:
        """Fraction (%) of the next 24h meeting the risk condition."""
        from datetime import datetime as _dt

        hourly = self._hourly()
        p90 = self._stats().get("p90", [])
        p10 = self._stats().get("p10", [])
        if not hourly:
            return None
        now_ts = _dt.now(UTC).timestamp()
        # Include the current hour (its start may lie slightly in the past).
        window_start = now_ts - 3600
        window = [e for e in hourly if window_start <= e.get("ts", 0) <= now_ts + 24 * 3600]
        if not window:
            return None
        p90_by_ts = {e.get("ts"): e for e in p90}
        p10_by_ts = {e.get("ts"): e for e in p10}

        def _stat(series: dict, key: str, name: str) -> float | None:
            return series.get(key, {}).get(name)

        hits = 0
        checked = 0
        for entry in window:
            ts = entry.get("ts")
            checked += 1
            if self._sensor_key == "rain_risk_24h":
                v = _stat(p90_by_ts, ts, "precipitation")
                base = entry.get("precipitation") or 0.0
                val = v if v is not None else base
                if val is not None and val > 0.1:
                    hits += 1
            elif self._sensor_key == "frost_risk_24h":
                v = _stat(p10_by_ts, ts, "temperature")
                temp = v if v is not None else entry.get("temperature")
                if temp is not None and temp < 0.5:
                    hits += 1
            elif self._sensor_key == "heat_risk_24h":
                v = _stat(p90_by_ts, ts, "temperature")
                temp = v if v is not None else entry.get("temperature")
                if temp is not None and temp > 30.0:
                    hits += 1
        if not checked:
            return None
        return round(hits / checked * 100.0, 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data or {}
        attrs: dict[str, Any] = {"init_time": data.get("init_time")}
        if self._sensor_key in ("rain_risk_24h", "frost_risk_24h", "heat_risk_24h"):
            stats = self._stats()
            if stats.get("p10"):
                attrs["p10"] = stats["p10"]
            if stats.get("p90"):
                attrs["p90"] = stats["p90"]
        return attrs
