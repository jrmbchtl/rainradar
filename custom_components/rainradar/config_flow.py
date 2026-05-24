from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    CONF_LOCATIONS,
    CONF_SCAN_INTERVAL,
    CONF_DEVICE_TRACKER,
    CONF_TRACKED_LOCATION_NAME,
    DEFAULT_SCAN_INTERVAL,
)


class RainradarConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors = {}
        if user_input is not None:
            return self.async_create_entry(
                title="Rainradar",
                data={},
                options={
                    CONF_LOCATIONS: user_input.get(CONF_LOCATIONS, []),
                    CONF_SCAN_INTERVAL: user_input.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                    ),
                    CONF_DEVICE_TRACKER: user_input.get(CONF_DEVICE_TRACKER),
                    CONF_TRACKED_LOCATION_NAME: user_input.get(
                        CONF_TRACKED_LOCATION_NAME, "Tracked"
                    ),
                },
            )

        home_lat = self.hass.config.latitude
        home_lon = self.hass.config.longitude

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_LOCATIONS,
                        default=[
                            {
                                CONF_NAME: "Home",
                                CONF_LATITUDE: home_lat,
                                CONF_LONGITUDE: home_lon,
                            }
                        ],
                    ): selector.ObjectSelector(),
                    vol.Optional(CONF_DEVICE_TRACKER): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="device_tracker",
                        )
                    ),
                    vol.Optional(
                        CONF_TRACKED_LOCATION_NAME, default="Tracked"
                    ): str,
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=DEFAULT_SCAN_INTERVAL,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            mode=selector.NumberSelectorMode.BOX,
                            step=10,
                            min=60, max=3600,
                            unit_of_measurement="seconds",
                        )
                    ),
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return RainradarOptionsFlow(config_entry)


class RainradarOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_LOCATIONS: user_input.get(CONF_LOCATIONS, []),
                    CONF_SCAN_INTERVAL: user_input.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                    ),
                    CONF_DEVICE_TRACKER: user_input.get(CONF_DEVICE_TRACKER),
                    CONF_TRACKED_LOCATION_NAME: user_input.get(
                        CONF_TRACKED_LOCATION_NAME, "Tracked"
                    ),
                },
            )

        current_locations = self.config_entry.options.get(CONF_LOCATIONS, [])
        scan_interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        device_tracker = self.config_entry.options.get(CONF_DEVICE_TRACKER)
        tracked_name = self.config_entry.options.get(
            CONF_TRACKED_LOCATION_NAME, "Tracked"
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_LOCATIONS,
                        default=current_locations,
                    ): selector.ObjectSelector(),
                    vol.Optional(CONF_DEVICE_TRACKER, default=device_tracker): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="device_tracker",
                        )
                    ),
                    vol.Optional(
                        CONF_TRACKED_LOCATION_NAME,
                        default=tracked_name,
                    ): str,
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=scan_interval,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            mode=selector.NumberSelectorMode.BOX,
                            step=10,
                            min=60, max=3600,
                            unit_of_measurement="seconds",
                        )
                    ),
                }
            ),
        )
