from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import callback

from .const import (
    DOMAIN,
    CONF_LOCATIONS,
    CONF_SCAN_INTERVAL,
    CONF_DEVICE_TRACKER,
    DEFAULT_SCAN_INTERVAL,
)

MINUTES = 60


class RainradarConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            locations = [{
                "name": user_input.get("name", "Home"),
                CONF_LATITUDE: user_input[CONF_LATITUDE],
                CONF_LONGITUDE: user_input[CONF_LONGITUDE],
            }]
            return self.async_create_entry(
                title="Rainradar",
                data={},
                options={
                    CONF_LOCATIONS: locations,
                    CONF_DEVICE_TRACKER: user_input.get(CONF_DEVICE_TRACKER),
                    CONF_SCAN_INTERVAL: user_input.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                    ),
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("name", default="Home"): str,
                    vol.Required(CONF_LATITUDE): vol.Coerce(float),
                    vol.Required(CONF_LONGITUDE): vol.Coerce(float),
                    vol.Optional(CONF_DEVICE_TRACKER): str,
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
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        current_locations = self.config_entry.options.get(CONF_LOCATIONS, [])
        device_tracker = self.config_entry.options.get(CONF_DEVICE_TRACKER)
        scan_interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )

        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_LOCATIONS: current_locations,
                    CONF_DEVICE_TRACKER: user_input.get(CONF_DEVICE_TRACKER),
                    CONF_SCAN_INTERVAL: user_input.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                    ),
                },
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_DEVICE_TRACKER, default=device_tracker): str,
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=scan_interval,
                    ): vol.All(vol.Coerce(int), vol.Range(min=60, max=3600)),
                }
            ),
        )
