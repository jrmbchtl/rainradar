from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    CONF_LOCATIONS,
    CONF_SCAN_INTERVAL,
    CONF_DEVICE_TRACKER,
    CONF_DEVICE_TRACKERS,
    CONF_ZONES,
    CONF_NAME,
    DEFAULT_SCAN_INTERVAL,
)


def _normalize_entity_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _zones_to_locations(hass, zone_entities: list[str]) -> list[dict[str, Any]]:
    locations: list[dict[str, Any]] = []
    for zone_entity_id in zone_entities:
        zone_state = hass.states.get(zone_entity_id)
        if zone_state is None:
            continue
        lat = zone_state.attributes.get(CONF_LATITUDE)
        lon = zone_state.attributes.get(CONF_LONGITUDE)
        if lat is None or lon is None:
            continue
        locations.append(
            {
                CONF_NAME: zone_state.attributes.get("friendly_name", zone_entity_id),
                CONF_LATITUDE: float(lat),
                CONF_LONGITUDE: float(lon),
            }
        )
    return locations


class RainradarConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def _get_default_zones(self) -> list[str]:
        if self.hass.states.get("zone.home") is not None:
            return ["zone.home"]
        return []

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            zone_entities = _normalize_entity_list(user_input.get(CONF_ZONES))
            device_trackers = _normalize_entity_list(user_input.get(CONF_DEVICE_TRACKERS))

            return self.async_create_entry(
                title="Rainradar",
                data={},
                options={
                    CONF_LOCATIONS: [],
                    CONF_ZONES: zone_entities,
                    CONF_DEVICE_TRACKERS: device_trackers,
                    # Backward compatibility for previous single-tracker schema.
                    CONF_DEVICE_TRACKER: device_trackers[0] if device_trackers else None,
                    CONF_SCAN_INTERVAL: user_input.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                    ),
                },
            )

        default_zones = self._get_default_zones()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_ZONES, default=default_zones): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="zone", multiple=True)
                    ),
                    vol.Optional(CONF_DEVICE_TRACKERS, default=[]): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="device_tracker", multiple=True)
                    ),
                    vol.Optional(
                        CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                    ): vol.All(vol.Coerce(int), vol.Range(min=60, max=3600)),
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return RainradarOptionsFlow(config_entry)


class RainradarOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        current_locations = self._config_entry.options.get(CONF_LOCATIONS, [])
        zones = _normalize_entity_list(self._config_entry.options.get(CONF_ZONES))
        if not zones and self.hass.states.get("zone.home") is not None:
            zones = ["zone.home"]

        device_trackers = _normalize_entity_list(
            self._config_entry.options.get(CONF_DEVICE_TRACKERS)
        )
        if not device_trackers:
            device_trackers = _normalize_entity_list(
                self._config_entry.options.get(CONF_DEVICE_TRACKER)
            )

        scan_interval = self._config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )

        if user_input is not None:
            zones = _normalize_entity_list(user_input.get(CONF_ZONES))
            device_trackers = _normalize_entity_list(user_input.get(CONF_DEVICE_TRACKERS))

            return self.async_create_entry(
                title="",
                data={
                    CONF_LOCATIONS: current_locations,
                    CONF_ZONES: zones,
                    CONF_DEVICE_TRACKERS: device_trackers,
                    CONF_DEVICE_TRACKER: device_trackers[0] if device_trackers else None,
                    CONF_SCAN_INTERVAL: user_input.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                    ),
                },
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_ZONES,
                        default=zones,
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="zone", multiple=True)
                    ),
                    vol.Optional(
                        CONF_DEVICE_TRACKERS,
                        default=device_trackers,
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="device_tracker", multiple=True)
                    ),
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=scan_interval,
                    ): vol.All(vol.Coerce(int), vol.Range(min=60, max=3600)),
                }
            ),
        )
