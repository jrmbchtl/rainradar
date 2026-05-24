from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
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
    SENSOR_TYPES,
    ATTR_TEMPERATURE,
    ATTR_HUMIDITY,
    ATTR_WIND_SPEED,
    ATTR_WIND_DIRECTION,
    ATTR_PRESSURE,
    ATTR_PRECIPITATION,
    ATTR_CONDITION,
    ATTR_ALERTS,
    ATTR_SUNSHINE,
)
from .coordinator import RainradarCoordinator

FIELD_MAP = {
    "temperature": ATTR_TEMPERATURE,
    "humidity": ATTR_HUMIDITY,
    "wind_speed": ATTR_WIND_SPEED,
    "wind_direction": ATTR_WIND_DIRECTION,
    "pressure": ATTR_PRESSURE,
    "precipitation": ATTR_PRECIPITATION,
    "condition": ATTR_CONDITION,
    "alerts": ATTR_ALERTS,
    "sunshine": ATTR_SUNSHINE,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: RainradarCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []

    def add_location_entities(loc_key: str, loc_name: str, safe_name: str) -> None:
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
        zone_name = zone_state.attributes.get("friendly_name", zone_entity) if zone_state else zone_entity
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
        add_location_entities(f"tracker::{tracker_entity}", tracked_name, safe_tracked)

    async_add_entities(entities)


class RainradarSensor(CoordinatorEntity, SensorEntity):
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
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{entry.entry_id}_{safe_name}")},
            "name": f"Rainradar {loc_name}",
            "manufacturer": "DWD",
            "model": "Weather Station",
            "sw_version": "0.1.0",
        }

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
        if self._sensor_key == "condition":
            attrs["icon"] = loc_data.get("icon")
        return {k: v for k, v in attrs.items() if v is not None}
