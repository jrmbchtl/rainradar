from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
import voluptuous as vol

from .const import (
    CONF_DEVICE_TRACKER,
    CONF_DEVICE_TRACKERS,
    CONF_ENABLE_AIR_QUALITY,
    CONF_ENABLE_FORECAST,
    CONF_ENABLE_ICON_EU,
    CONF_ENABLE_UV,
    CONF_ENABLE_WARNINGS,
    CONF_LOCATIONS,
    CONF_SCAN_INTERVAL,
    CONF_ZONES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    normalize_entity_list,
)

ENABLE_TOGGLE_KEYS = (
    CONF_ENABLE_FORECAST,
    CONF_ENABLE_ICON_EU,
    CONF_ENABLE_UV,
    CONF_ENABLE_WARNINGS,
    CONF_ENABLE_AIR_QUALITY,
)


def _build_schema(
    zones: list[str],
    device_trackers: list[str],
    scan_interval: int,
    toggles: dict[str, bool],
) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_ZONES, default=zones): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="zone", multiple=True)
            ),
            vol.Optional(
                CONF_DEVICE_TRACKERS, default=device_trackers
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="device_tracker", multiple=True)
            ),
            vol.Optional(
                CONF_SCAN_INTERVAL, default=scan_interval
            ): vol.All(vol.Coerce(int), vol.Range(min=60, max=3600)),
            vol.Optional(
                CONF_ENABLE_FORECAST, default=toggles.get(CONF_ENABLE_FORECAST, True)
            ): bool,
            vol.Optional(
                CONF_ENABLE_ICON_EU, default=toggles.get(CONF_ENABLE_ICON_EU, True)
            ): bool,
            vol.Optional(
                CONF_ENABLE_UV, default=toggles.get(CONF_ENABLE_UV, True)
            ): bool,
            vol.Optional(
                CONF_ENABLE_WARNINGS, default=toggles.get(CONF_ENABLE_WARNINGS, True)
            ): bool,
            vol.Optional(
                CONF_ENABLE_AIR_QUALITY,
                default=toggles.get(CONF_ENABLE_AIR_QUALITY, True),
            ): bool,
        }
    )


def _result_options(user_input: dict[str, Any], current_locations: list) -> dict[str, Any]:
    zone_entities = normalize_entity_list(user_input.get(CONF_ZONES))
    device_trackers = normalize_entity_list(user_input.get(CONF_DEVICE_TRACKERS))
    return {
        CONF_LOCATIONS: current_locations,
        CONF_ZONES: zone_entities,
        CONF_DEVICE_TRACKERS: device_trackers,
        # Legacy single-tracker mirror kept for older card versions.
        CONF_DEVICE_TRACKER: device_trackers[0] if device_trackers else None,
        CONF_SCAN_INTERVAL: user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        **{key: bool(user_input.get(key, True)) for key in ENABLE_TOGGLE_KEYS},
    }


class RainradarConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the rainradar config flow."""

    VERSION = 2

    def _get_default_zones(self) -> list[str]:
        if self.hass.states.get("zone.home") is not None:
            return ["zone.home"]
        return []

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(
                title="Rainradar",
                data={},
                options=_result_options(user_input, current_locations=[]),
            )

        return self.async_show_form(
            step_id="user",
            data_schema=_build_schema(
                zones=self._get_default_zones(),
                device_trackers=[],
                scan_interval=DEFAULT_SCAN_INTERVAL,
                toggles={},
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return RainradarOptionsFlow()


class RainradarOptionsFlow(config_entries.OptionsFlow):
    """Handle the rainradar options flow.

    Uses the built-in ``self.config_entry`` property — do not store the
    entry on the instance (that pattern is phased out in HA).
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        entry = self.config_entry
        current_locations = entry.options.get(CONF_LOCATIONS, [])

        zones = normalize_entity_list(entry.options.get(CONF_ZONES))
        if not zones and self.hass.states.get("zone.home") is not None:
            zones = ["zone.home"]

        device_trackers = normalize_entity_list(entry.options.get(CONF_DEVICE_TRACKERS))
        if not device_trackers:
            device_trackers = normalize_entity_list(entry.options.get(CONF_DEVICE_TRACKER))

        scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        toggles = {key: entry.options.get(key, True) for key in ENABLE_TOGGLE_KEYS}

        if user_input is not None:
            return self.async_create_entry(
                title="",
                data=_result_options(user_input, current_locations=current_locations),
            )

        return self.async_show_form(
            step_id="init",
            data_schema=_build_schema(
                zones=zones,
                device_trackers=device_trackers,
                scan_interval=scan_interval,
                toggles=toggles,
            ),
        )
