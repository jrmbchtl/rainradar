from __future__ import annotations

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
    ATTR_PRECIPITATION,
    ATTR_CONDITION,
)
from .coordinator import RainradarCoordinator

FIELD_MAP = {
    "temperature": ATTR_TEMPERATURE,
    "humidity": ATTR_HUMIDITY,
    "wind_speed": ATTR_WIND_SPEED,
    "wind_direction": ATTR_WIND_DIRECTION,
    "precipitation": ATTR_PRECIPITATION,
    "condition": ATTR_CONDITION,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: RainradarCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    safe_names: list[str] = []

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

    def add_location_entities(loc_key: str, loc_name: str, safe_name: str) -> None:
        safe_names.append(safe_name)
        for sensor_key, desc in SENSOR_TYPES.items():
            entities.append(
                RainradarSensor(
                    coordinator,
                    entry,
                    loc_key,
                    loc_name,
                    safe_name,
                    SensorEntityDescription(
                        key=f"{sensor_key}_{safe_name}",
                        name=f"{loc_name} {desc.get('name', sensor_key)}",
                        native_unit_of_measurement=desc.get("unit"),
                        icon=desc.get("icon"),
                    ),
                    sensor_key,
                )
            )

    zones = entry.options.get(CONF_ZONES, [])

    if not zones:
        for loc in entry.options.get(CONF_LOCATIONS, []):
            loc_name = loc.get(CONF_NAME, "unknown")
            safe_name = loc_name.lower().replace(" ", "_").replace("-", "_")
            add_location_entities(loc_name, loc_name, safe_name)

    for zone_entity in zones:
        zone_state = hass.states.get(zone_entity)
        zone_name = (
            zone_state.attributes.get("friendly_name", zone_entity)
            if zone_state
            else zone_entity
        )
        safe_zone = zone_entity.replace(".", "_").replace("-", "_")
        add_location_entities(f"zone::{zone_entity}", zone_name, safe_zone)

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
        add_location_entities(
            f"tracker::{tracker_entity}", tracked_name, safe_tracked
        )

    _cleanup_deprecated_entities(hass, safe_names)

    async_add_entities(entities)


def _cleanup_deprecated_entities(hass: HomeAssistant, safe_names: list[str]) -> None:
    registry = er.async_get(hass)
    for safe_name in safe_names:
        for sensor_key in ("pressure", "alerts", "sunshine"):
            unique_id = f"{DOMAIN}_{safe_name}_{sensor_key}"
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
            + len(radar_frames.get("forecast", []))
        )

    @property
    def extra_state_attributes(self) -> dict | None:
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
    def extra_state_attributes(self) -> dict | None:
        data = self.coordinator.data
        if not data:
            return None
        # Keep payload compact: HA caps sensor attribute state at 16 KiB
        # in the recorder. We send {lat, lon} per station; the card can
        # use the lat/lon for the marker and show coordinates in the
        # tooltip. Names are exposed separately in `station_names` and
        # truncated to a safe budget.
        stations: list[dict[str, float]] = []
        names: dict[str, str] = {}
        name_budget = 4096
        for i, station in enumerate(self.coordinator.stations):
            stations.append({"lat": station.lat, "lon": station.lon})
            if name_budget > 0:
                truncated = station.name[:32]
                entry_size = len(truncated) + 8
                if entry_size <= name_budget:
                    names[str(i)] = truncated
                    name_budget -= entry_size
        return {"stations": stations, "station_names": names}


class RainradarSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RainradarCoordinator,
        entry: ConfigEntry,
        loc_key: str,
        loc_name: str,
        safe_name: str,
        description: SensorEntityDescription,
        sensor_key: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._loc_key = loc_key
        self._loc_name = loc_name
        self._safe_name = safe_name
        self._sensor_key = sensor_key
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_{safe_name}_{sensor_key}"
        self._attr_device_info = _common_device_info(
            entry, safe_name, f"Rainradar {loc_name}", "Weather Station"
        )

    @property
    def available(self) -> bool:
        if not (self.coordinator.last_update_success and self.coordinator.data):
            return False
        return self._sensor_key in (self.coordinator.data.get("locations", {})
                                     .get(self._loc_key) or {})

    @property
    def native_value(self):
        data = self.coordinator.data
        if data is None:
            return None
        loc_data = data.get("locations", {}).get(self._loc_key, {})
        field = FIELD_MAP.get(self._sensor_key)
        if field is None:
            return None
        return loc_data.get(field)

    @property
    def extra_state_attributes(self) -> dict | None:
        data = self.coordinator.data
        if data is None:
            return None
        loc_data = data.get("locations", {}).get(self._loc_key, {})
        attrs = {
            "station_name": loc_data.get("station_name"),
            "station_distance_km": loc_data.get("station_distance_km"),
            "station_id": loc_data.get("station_id"),
            "source_entity": loc_data.get("source_entity"),
        }
        return {k: v for k, v in attrs.items() if v is not None}
