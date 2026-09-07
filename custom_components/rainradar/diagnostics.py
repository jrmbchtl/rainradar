from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

TO_REDACT = {"station_name", "station_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    runtime = entry.runtime_data
    weather_coordinator = runtime.weather_coordinator if runtime else None
    radar_coordinator = runtime.radar_coordinator if runtime else None

    weather_data = (weather_coordinator.data if weather_coordinator else None) or {}
    radar_data = (radar_coordinator.data if radar_coordinator else None) or {}

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
                "forecast_locations": len(radar_data.get("mosmix_by_location", {})),
                "last_update": radar_data.get("last_update"),
            },
        },
        TO_REDACT,
    )
