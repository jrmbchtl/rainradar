from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    CONF_LOCATIONS,
    CONF_SCAN_INTERVAL,
    CONF_DEVICE_TRACKER,
    CONF_DEVICE_TRACKERS,
    CONF_ZONES,
    CONF_ENABLE_FORECAST,
    CONF_ENABLE_ICON_EU,
    CONF_ENABLE_UV,
    CONF_ENABLE_WARNINGS,
    CONF_ENABLE_AIR_QUALITY,
    DEFAULT_SCAN_INTERVAL,
    normalize_entity_list,
)


class RainradarConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    @staticmethod
    async def async_migrate_entry(
        hass, config_entry: config_entries.ConfigEntry
    ) -> bool:
        if config_entry.version == 1:
            data = {**config_entry.data}
            options = {**config_entry.options}
            options.setdefault(CONF_ZONES, options.pop(CONF_LOCATIONS, []))
            options.setdefault(CONF_DEVICE_TRACKERS, [])
            legacy_tracker = options.get(CONF_DEVICE_TRACKER)
            if legacy_tracker and not options[CONF_DEVICE_TRACKERS]:
                options[CONF_DEVICE_TRACKERS] = [legacy_tracker]
            options.setdefault(CONF_ENABLE_FORECAST, True)

            options.setdefault(CONF_ENABLE_ICON_EU, True)
            options.setdefault(CONF_ENABLE_UV, True)
            hass.config_entries.async_update_entry(
                config_entry, data=data, options=options, version=2
            )
        return True

    def _get_default_zones(self) -> list[str]:
        if self.hass.states.get("zone.home") is not None:
            return ["zone.home"]
        return []

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            zone_entities = normalize_entity_list(user_input.get(CONF_ZONES))
            device_trackers = normalize_entity_list(user_input.get(CONF_DEVICE_TRACKERS))

            return self.async_create_entry(
                title="Rainradar",
                data={},
                options={
                    CONF_LOCATIONS: [],
                    CONF_ZONES: zone_entities,
                    CONF_DEVICE_TRACKERS: device_trackers,
                    CONF_DEVICE_TRACKER: device_trackers[0] if device_trackers else None,
                    CONF_SCAN_INTERVAL: user_input.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                    ),
                    CONF_ENABLE_FORECAST: user_input.get(CONF_ENABLE_FORECAST, True),
                    CONF_ENABLE_ICON_EU: user_input.get(CONF_ENABLE_ICON_EU, True),
                    CONF_ENABLE_UV: user_input.get(CONF_ENABLE_UV, True),
                    CONF_ENABLE_WARNINGS: user_input.get(CONF_ENABLE_WARNINGS, True),
                    CONF_ENABLE_AIR_QUALITY: user_input.get(CONF_ENABLE_AIR_QUALITY, True),
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
                    vol.Optional(CONF_ENABLE_FORECAST, default=True): bool,
                    vol.Optional(CONF_ENABLE_ICON_EU, default=True): bool,
                    vol.Optional(CONF_ENABLE_UV, default=True): bool,
                    vol.Optional(CONF_ENABLE_WARNINGS, default=True): bool,
                    vol.Optional(CONF_ENABLE_AIR_QUALITY, default=True): bool,
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
        zones = normalize_entity_list(self._config_entry.options.get(CONF_ZONES))
        if not zones and self.hass.states.get("zone.home") is not None:
            zones = ["zone.home"]

        device_trackers = normalize_entity_list(
            self._config_entry.options.get(CONF_DEVICE_TRACKERS)
        )
        if not device_trackers:
            device_trackers = normalize_entity_list(
                self._config_entry.options.get(CONF_DEVICE_TRACKER)
            )

        scan_interval = self._config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )

        enable_forecast = self._config_entry.options.get(CONF_ENABLE_FORECAST, True)
        enable_icon_eu = self._config_entry.options.get(CONF_ENABLE_ICON_EU, True)
        enable_uv = self._config_entry.options.get(CONF_ENABLE_UV, True)
        enable_warnings = self._config_entry.options.get(CONF_ENABLE_WARNINGS, True)
        enable_air_quality = self._config_entry.options.get(CONF_ENABLE_AIR_QUALITY, True)

        if user_input is not None:
            zones = normalize_entity_list(user_input.get(CONF_ZONES))
            device_trackers = normalize_entity_list(user_input.get(CONF_DEVICE_TRACKERS))

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
                    CONF_ENABLE_FORECAST: user_input.get(CONF_ENABLE_FORECAST, enable_forecast),
                    CONF_ENABLE_ICON_EU: user_input.get(CONF_ENABLE_ICON_EU, enable_icon_eu),
                    CONF_ENABLE_UV: user_input.get(CONF_ENABLE_UV, enable_uv),
                    CONF_ENABLE_WARNINGS: user_input.get(CONF_ENABLE_WARNINGS, enable_warnings),
                    CONF_ENABLE_AIR_QUALITY: user_input.get(CONF_ENABLE_AIR_QUALITY, enable_air_quality),
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
                    vol.Optional(CONF_ENABLE_FORECAST, default=enable_forecast): bool,
                    vol.Optional(CONF_ENABLE_ICON_EU, default=enable_icon_eu): bool,
                    vol.Optional(CONF_ENABLE_UV, default=enable_uv): bool,
                    vol.Optional(CONF_ENABLE_WARNINGS, default=enable_warnings): bool,
                    vol.Optional(CONF_ENABLE_AIR_QUALITY, default=enable_air_quality): bool,
                }
            ),
        )
