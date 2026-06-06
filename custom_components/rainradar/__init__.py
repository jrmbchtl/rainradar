import asyncio
import logging
import os
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, frames_cache_dir, frames_url_prefix, INTEGRATION_VERSION

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.WEATHER]

_CARD_REGISTERED_KEY = f"{DOMAIN}_card_registered"
_FRAMES_PATH_REGISTERED_KEY = f"{DOMAIN}_frames_paths"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    await _register_card(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .coordinator import RainradarCoordinator

    coordinator = RainradarCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await _register_frames_path(hass, entry.entry_id)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as exc:
        _LOGGER.warning(
            "Initial DWD refresh failed for %s; will retry on next interval: %s",
            entry.entry_id,
            exc,
        )
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


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
    url = f"/{DOMAIN}/rainradar-card.js"

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

    try:
        resources = hass.data.get("lovelace", {}).get("resources")
        if resources is not None and hasattr(resources, "async_create_item"):
            items = await resources.async_items()
            for item in items:
                item_url = item.get("url", "")
                if item_url.startswith(url) and item_url != url:
                    try:
                        await resources.async_delete_item(item["id"])
                    except Exception:
                        pass
            if not any(r.get("url") == url for r in items):
                await resources.async_create_item(
                    {"res_type": "module", "url": url}
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
    registered.add(entry_id)

    cache_dir = frames_cache_dir(hass.config.path(""), entry_id)
    # mkdir is a blocking syscall; offload to a thread so the event loop
    # stays responsive on the first setup of an integration.
    await asyncio.to_thread(cache_dir.mkdir, parents=True, exist_ok=True)
    url = frames_url_prefix(entry_id)

    if hasattr(hass.http, "async_register_static_paths"):
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(url, str(cache_dir), cache_headers=False)]
        )
    else:
        hass.http.register_static_path(url, str(cache_dir), cache_headers=False)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_close()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
