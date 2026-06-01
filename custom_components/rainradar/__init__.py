import logging
import os

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.WEATHER]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .coordinator import RainradarCoordinator

    coordinator = RainradarCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await _register_card(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def _register_card(hass: HomeAssistant) -> None:
    card_path = os.path.join(os.path.dirname(__file__), "frontend/dist/rainradar-card.js")
    if not os.path.isfile(card_path):
        _LOGGER.warning("Rainradar card JS not found at %s", card_path)
        return
    # Append a cache-busting version based on the file modification time so browsers
    # pick up rebuilt bundles without manual cache clearing.
    try:
        mtime = int(os.path.getmtime(card_path))
    except Exception:
        mtime = 0
    url = f"/{DOMAIN}/rainradar-card.js?v={mtime}"

    # HA 2025+: async static path registration API
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
        return

    # HA 2025+: Lovelace resource system
    try:
        resources = hass.data.get("lovelace", {}).get("resources")
        if resources is not None and hasattr(resources, "async_create_item"):
            items = await resources.async_items()
            # Remove stale resources from previous builds (mtime-based URLs)
            for item in items:
                if item.get("url", "").startswith(f"/{DOMAIN}/rainradar-card.js?") and item.get("url") != url:
                    try:
                        await resources.async_delete_item(item["id"])
                    except Exception:
                        pass
            if not any(r.get("url") == url for r in items):
                await resources.async_create_item({
                    "res_type": "module",
                    "url": url,
                })
            _LOGGER.info("Rainradar card registered via Lovelace resources")
            return
    except Exception as exc:
        _LOGGER.debug("Lovelace resource registration failed: %s", exc)

    # Legacy HA: add_extra_js_url (removed in HA 2025+)
    try:
        from homeassistant.components import frontend
        frontend.add_extra_js_url(hass, url)
        _LOGGER.info("Rainradar card registered via add_extra_js_url")
        return
    except Exception as exc:
        _LOGGER.debug("add_extra_js_url failed: %s", exc)

    _LOGGER.warning(
        "Could not auto-register Rainradar card at %s. "
        "Add it as a Lovelace resource manually.", url
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_close()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
