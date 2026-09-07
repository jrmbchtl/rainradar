from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import os

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_DEVICE_TRACKER,
    CONF_DEVICE_TRACKERS,
    CONF_ENABLE_AIR_QUALITY,
    CONF_ENABLE_FORECAST,
    CONF_ENABLE_ICON_EU,
    CONF_ENABLE_UV,
    CONF_ENABLE_WARNINGS,
    CONF_LOCATIONS,
    CONF_ZONES,
    DOMAIN,
    INTEGRATION_VERSION,
    frames_cache_dir,
    frames_url_prefix,
    resolve_location_specs,
)
from .station_mapping import DWDStation

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.WEATHER]

_CARD_REGISTERED_KEY = f"{DOMAIN}_card_registered"
_FRAMES_PATH_REGISTERED_KEY = f"{DOMAIN}_frames_paths"

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class RainradarEntryData:
    """Runtime data for a rainradar config entry."""

    weather_coordinator: object
    radar_coordinator: object
    stations: list[DWDStation] = field(default_factory=list)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    await _register_card(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .radar_coordinator import RadarDataCoordinator
    from .weather_coordinator import WeatherDataCoordinator

    stations: list[DWDStation] = []
    weather_coordinator = WeatherDataCoordinator(hass, entry, stations)
    radar_coordinator = RadarDataCoordinator(hass, entry, stations)

    entry.runtime_data = RainradarEntryData(
        weather_coordinator=weather_coordinator,
        radar_coordinator=radar_coordinator,
        stations=stations,
    )

    await _register_frames_path(hass, entry.entry_id)

    radar_coordinator.data = {
        "locations": {
            loc.loc_key: {
                "rain_2h_total": 0,
                "rain_slots": [],
                "warning_level": 0,
                "warning_count": 0,
            }
            for loc in resolve_location_specs(hass, entry)
        }
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    try:
        await weather_coordinator.async_config_entry_first_refresh()
    except Exception as exc:
        _LOGGER.warning(
            "Initial weather refresh failed for %s; will retry on next interval: %s",
            entry.entry_id,
            exc,
        )

    entry.async_create_background_task(
        hass,
        _initial_radar_refresh(radar_coordinator),
        f"{DOMAIN}_initial_radar_refresh_{entry.entry_id}",
    )

    try:
        from .openmap_bridge import (
            async_register_overlay,
            async_setup_delayed_openmap_listener,
        )

        await async_register_overlay(hass, entry)
        async_setup_delayed_openmap_listener(hass, entry)
    except Exception as exc:
        _LOGGER.debug("OpenMap bridge setup failed: %s", exc)

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries to the current version.

    NOTE: HA resolves this hook on the integration component (__init__.py),
    NOT on the config flow class.
    """
    if entry.version > 2:
        # Downgrade from a future version: not supported.
        return False

    if entry.version == 1:
        data = {**entry.data}
        options = {**entry.options}
        options.setdefault(CONF_ZONES, options.pop(CONF_LOCATIONS, []))
        options.setdefault(CONF_DEVICE_TRACKERS, [])
        legacy_tracker = options.get(CONF_DEVICE_TRACKER)
        if legacy_tracker and not options[CONF_DEVICE_TRACKERS]:
            options[CONF_DEVICE_TRACKERS] = [legacy_tracker]
        options.setdefault(CONF_ENABLE_FORECAST, True)
        options.setdefault(CONF_ENABLE_ICON_EU, True)
        options.setdefault(CONF_ENABLE_UV, True)
        options.setdefault(CONF_ENABLE_WARNINGS, True)
        options.setdefault(CONF_ENABLE_AIR_QUALITY, True)
        hass.config_entries.async_update_entry(entry, data=data, options=options, version=2)

    _LOGGER.info("Config entry %s migrated to version %s", entry.entry_id, entry.version)
    return True


async def _initial_radar_refresh(coordinator):
    """Run first radar refresh in background so sensors appear immediately."""
    try:
        await coordinator.async_refresh()
    except Exception as exc:
        _LOGGER.warning(
            "Initial radar refresh failed; will retry on next interval: %s", exc
        )


async def _register_card(hass: HomeAssistant) -> None:
    """Register the Lovelace card resource once per HA instance."""
    registered = hass.data.get(_CARD_REGISTERED_KEY)
    if registered is hass:
        return

    card_path = os.path.join(
        os.path.dirname(__file__), "frontend/dist/rainradar-card.js"
    )
    if not os.path.isfile(card_path):
        _LOGGER.warning("Rainradar card JS not found at %s", card_path)
        return
    url = f"/{DOMAIN}/v{INTEGRATION_VERSION}/rainradar-card.js"

    if hass.http is not None:
        try:
            if hasattr(hass.http, "async_register_static_paths"):
                from homeassistant.components.http import StaticPathConfig

                await hass.http.async_register_static_paths(
                    [StaticPathConfig(url, card_path, cache_headers=False)]
                )
            else:
                hass.http.register_static_path(url, card_path, cache_headers=False)
        except Exception as exc:
            _LOGGER.warning("Failed to register static path %s: %s", url, exc)
    else:
        _LOGGER.debug("Rainradar: http component not available for card URL %s", url)

    try:
        resources = hass.data.get("lovelace", {}).get("resources")
        if resources is not None and hasattr(resources, "async_create_item"):
            items = await resources.async_items()
            for item in items:
                item_url = item.get("url", "")
                if (
                    f"/{DOMAIN}/" in item_url
                    and "rainradar-card" in item_url
                    and item_url != url
                ):
                    try:
                        await resources.async_delete_item(item["id"])
                        _LOGGER.info(
                            "Removed stale rainradar card resource: %s", item_url
                        )
                    except Exception as exc:
                        _LOGGER.debug(
                            "Failed to remove stale resource %s: %s", item_url, exc
                        )
            if not any(r.get("url") == url for r in items):
                await resources.async_create_item(
                    {"res_type": "js", "url": url}
                )
            _LOGGER.info("Rainradar card registered via Lovelace resources")
            hass.data[_CARD_REGISTERED_KEY] = hass
            return
    except Exception as exc:
        _LOGGER.debug("Lovelace resource registration failed: %s", exc)

    try:
        from homeassistant.components import frontend

        frontend.add_extra_js_url(hass, url)
        _LOGGER.info("Rainradar card registered via add_extra_js_url")
        hass.data[_CARD_REGISTERED_KEY] = hass
        return
    except Exception as exc:
        _LOGGER.debug("add_extra_js_url failed: %s", exc)

    _LOGGER.warning(
        "Could not auto-register Rainradar card at %s. "
        "Add it as a Lovelace resource manually.",
        url,
    )


async def _register_frames_path(hass: HomeAssistant, entry_id: str) -> None:
    """Register a per-entry static path for prefetched radar frame PNGs."""
    registered = hass.data.setdefault(_FRAMES_PATH_REGISTERED_KEY, set())
    if entry_id in registered:
        return

    if hass.http is None:
        _LOGGER.warning(
            "Rainradar: http component not available; radar frame URLs for %s "
            "will not be served (card overlays will be empty)",
            entry_id,
        )
        return
    registered.add(entry_id)

    cache_dir = frames_cache_dir(hass.config.path(""), entry_id)
    await asyncio.to_thread(cache_dir.mkdir, parents=True, exist_ok=True)
    url = frames_url_prefix(entry_id)

    from homeassistant.components.http import StaticPathConfig

    await hass.http.async_register_static_paths(
        [StaticPathConfig(url, str(cache_dir), cache_headers=False)]
    )
    _LOGGER.debug("Rainradar: frames static path registered for %s", entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry.runtime_data = None
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
