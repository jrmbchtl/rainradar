from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

TO_REDACT = {"station_name", "station_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    weather_coordinator = hass.data[DOMAIN][entry.entry_id]
    radar_coordinator = hass.data[DOMAIN].get(f"{entry.entry_id}_radar")

    weather_data = weather_coordinator.data or {}
    radar_data = radar_coordinator.data if radar_coordinator else {}

    return async_redact_data(
        {
            "config_entry": entry.as_dict(),
            "weather_data": {
                "locations": weather_data.get("locations", {}),
                "stations_count": weather_data.get("stations_count"),
                "last_update": weather_data.get("last_update"),
            },
            "radar_data": {
                "past": len(radar_data.get("radar_frames", {}).get("past", [])),
                "nowcast": len(radar_data.get("radar_frames", {}).get("nowcast", [])),
                "forecasts": sum(len(v) for v in radar_data.get("forecasts_by_station", {}).values()),
                "last_update": radar_data.get("last_update"),
            },
        },
        TO_REDACT,
    )
