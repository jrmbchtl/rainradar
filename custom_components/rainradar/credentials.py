"""Credential storage for rainradar's experimental data sources.

Service-account JSON (WeatherNext) and the ADS API token (CAMS) live in a
dedicated HA Store (``.storage/rainradar_credentials``) instead of config
entry options so they never show up in entry dumps or diagnostics.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}_credentials"
STORAGE_VERSION = 1


def _store(hass: HomeAssistant) -> Store:
    return Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True)


async def async_get_credentials(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    """Return the credential dict for one config entry (may be empty)."""
    data = await _store(hass).async_load() or {}
    return data.get(entry_id, {})


async def async_save_credentials(
    hass: HomeAssistant, entry_id: str, credentials: dict[str, Any]
) -> None:
    """Persist credentials for one entry, preserving other entries."""
    store = _store(hass)
    data = await store.async_load() or {}
    data = {**data, entry_id: credentials}
    await store.async_save(data)


async def async_remove_credentials(hass: HomeAssistant, entry_id: str) -> None:
    """Drop credentials for one entry."""
    store = _store(hass)
    data = await store.async_load() or {}
    if entry_id in data:
        data = {k: v for k, v in data.items() if k != entry_id}
        await store.async_save(data)


async def async_copy_flow_credentials(
    hass: HomeAssistant, flow_id: str, entry_id: str
) -> None:
    """Re-key credentials saved during the user flow to the new entry id."""
    store = _store(hass)
    data = await store.async_load() or {}
    if flow_id in data:
        data[entry_id] = data.pop(flow_id)
        await store.async_save(data)
