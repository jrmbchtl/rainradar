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

ADD_ANOTHER = "add_another"
SUBMIT = "submit"

LOCATION_FIELDS = {
    vol.Required(CONF_NAME): selector.TextSelector(),
    vol.Required(CONF_LATITUDE): selector.NumberSelector(
        selector.NumberSelectorConfig(
            mode=selector.NumberSelectorMode.BOX,
            step=0.000001,
            min=-90, max=90,
        )
    ),
    vol.Required(CONF_LONGITUDE): selector.NumberSelector(
        selector.NumberSelectorConfig(
            mode=selector.NumberSelectorMode.BOX,
            step=0.000001,
            min=-180, max=180,
        )
    ),
}

LOCATION_SCHEMA = vol.Schema(LOCATION_FIELDS)


class RainradarConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._device_tracker: str | None = None
        self._tracked_name: str = "Tracked"
        self._scan_interval: int = DEFAULT_SCAN_INTERVAL
        self._locations: list[dict[str, Any]] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._device_tracker = user_input.get(CONF_DEVICE_TRACKER)
            self._tracked_name = user_input.get(CONF_TRACKED_LOCATION_NAME, "Tracked")
            self._scan_interval = user_input.get(
                CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
            )
            self._locations.append({
                CONF_NAME: user_input[CONF_NAME],
                CONF_LATITUDE: user_input[CONF_LATITUDE],
                CONF_LONGITUDE: user_input[CONF_LONGITUDE],
            })
            return await self.async_step_add_location()

        home_lat = self.hass.config.latitude
        home_lon = self.hass.config.longitude

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default="Home"): selector.TextSelector(),
                    vol.Required(CONF_LATITUDE, default=home_lat): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            mode=selector.NumberSelectorMode.BOX,
                            step=0.000001,
                            min=-90, max=90,
                        )
                    ),
                    vol.Required(CONF_LONGITUDE, default=home_lon): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            mode=selector.NumberSelectorMode.BOX,
                            step=0.000001,
                            min=-180, max=180,
                        )
                    ),
                    vol.Optional(CONF_DEVICE_TRACKER): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="device_tracker")
                    ),
                    vol.Optional(
                        CONF_TRACKED_LOCATION_NAME, default="Tracked"
                    ): selector.TextSelector(),
                    vol.Optional(
                        CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            mode=selector.NumberSelectorMode.BOX,
                            step=10, min=60, max=3600,
                            unit_of_measurement="seconds",
                        )
                    ),
                }
            ),
        )

    async def async_step_add_location(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._locations.append({
                CONF_NAME: user_input[CONF_NAME],
                CONF_LATITUDE: user_input[CONF_LATITUDE],
                CONF_LONGITUDE: user_input[CONF_LONGITUDE],
            })
            if user_input.get(ADD_ANOTHER):
                return await self.async_step_add_location()
            return self.async_create_entry(
                title="Rainradar",
                data={},
                options={
                    CONF_LOCATIONS: self._locations,
                    CONF_DEVICE_TRACKER: self._device_tracker,
                    CONF_TRACKED_LOCATION_NAME: self._tracked_name,
                    CONF_SCAN_INTERVAL: self._scan_interval,
                },
            )

        return self.async_show_form(
            step_id="add_location",
            data_schema=vol.Schema(
                {
                    **LOCATION_FIELDS,
                    vol.Optional(ADD_ANOTHER, default=False): selector.BooleanSelector(),
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
        self._locations: list[dict[str, Any]] = list(
            config_entry.options.get(CONF_LOCATIONS, [])
        )

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            action = user_input.get("action", "save")
            if action == "add_location":
                return await self.async_step_add_location()
            return self.async_create_entry(
                title="",
                data={
                    CONF_LOCATIONS: self._locations,
                    CONF_DEVICE_TRACKER: user_input.get(CONF_DEVICE_TRACKER),
                    CONF_TRACKED_LOCATION_NAME: user_input.get(
                        CONF_TRACKED_LOCATION_NAME, "Tracked"
                    ),
                    CONF_SCAN_INTERVAL: user_input.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                    ),
                },
            )

        device_tracker = self.config_entry.options.get(CONF_DEVICE_TRACKER)
        tracked_name = self.config_entry.options.get(
            CONF_TRACKED_LOCATION_NAME, "Tracked"
        )
        scan_interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )

        location_descriptions = "\n".join(
            f'{loc.get(CONF_NAME, "?")} ({loc.get(CONF_LATITUDE, "?")}, {loc.get(CONF_LONGITUDE, "?")})'
            for loc in self._locations
        ) or "None"

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "locations_display",
                        default=location_descriptions,
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(multiline=True)
                    ),
                    vol.Optional(CONF_DEVICE_TRACKER, default=device_tracker): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="device_tracker")
                    ),
                    vol.Optional(
                        CONF_TRACKED_LOCATION_NAME,
                        default=tracked_name,
                    ): selector.TextSelector(),
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=scan_interval,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            mode=selector.NumberSelectorMode.BOX,
                            step=10, min=60, max=3600,
                            unit_of_measurement="seconds",
                        )
                    ),
                    vol.Required("action", default="add_location"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {"value": "add_location", "label": "Add location"},
                                {"value": "save", "label": "Save"},
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
        )

    async def async_step_add_location(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._locations.append({
                CONF_NAME: user_input[CONF_NAME],
                CONF_LATITUDE: user_input[CONF_LATITUDE],
                CONF_LONGITUDE: user_input[CONF_LONGITUDE],
            })
            return await self.async_step_init()

        return self.async_show_form(
            step_id="add_location",
            data_schema=vol.Schema(LOCATION_FIELDS),
        )
