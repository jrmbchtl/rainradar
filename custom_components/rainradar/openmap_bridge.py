from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_COMPONENT_LOADED
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, RADAR_BBOX_LONLAT

_LOGGER = logging.getLogger(__name__)

DOMAIN_OPENMAP = "openmap"
OVERLAY_ID = "rainradar_dwd_radar"


async def async_register_overlay(hass: HomeAssistant, entry: ConfigEntry) -> None:
    if DOMAIN_OPENMAP not in hass.config.components:
        return
    try:
        await hass.services.async_call(
            DOMAIN_OPENMAP,
            "register_overlay",
            {
                "overlay": {
                    "id": OVERLAY_ID,
                    "name": "Rainradar (DWD)",
                    "type": "rainradar",
                    "entry_id": entry.entry_id,
                    "bounds": list(RADAR_BBOX_LONLAT),
                    "opacity": 0.7,
                }
            },
            blocking=True,
        )
        _LOGGER.info("Rainradar overlay registered with OpenMap")
    except Exception as exc:
        _LOGGER.debug("OpenMap bridge: register failed: %s", exc)


async def async_unregister_overlay(hass: HomeAssistant) -> None:
    if DOMAIN_OPENMAP not in hass.config.components:
        return
    try:
        await hass.services.async_call(
            DOMAIN_OPENMAP,
            "unregister_overlay",
            {"overlay_id": OVERLAY_ID},
            blocking=True,
        )
    except Exception as exc:
        _LOGGER.debug("OpenMap bridge: unregister failed: %s", exc)


@callback
def async_setup_delayed_openmap_listener(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Listen for openmap loading after rainradar setup."""

    @callback
    def _component_loaded(event):
        if event.data.get("component") == DOMAIN_OPENMAP:
            entry.async_create_background_task(
                hass,
                async_register_overlay(hass, entry),
                f"{DOMAIN}_openmap_register_{entry.entry_id}",
            )

    entry.async_on_unload(
        hass.bus.async_listen(EVENT_COMPONENT_LOADED, _component_loaded)
    )
