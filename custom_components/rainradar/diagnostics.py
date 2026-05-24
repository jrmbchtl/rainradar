from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import RainradarCoordinator

TO_REDACT = {"station_name", "station_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator: RainradarCoordinator = hass.data[DOMAIN][entry.entry_id]
    data = coordinator.data or {}

    return async_redact_data(
        {
            "config_entry": entry.as_dict(),
            "coordinator_data": {
                "locations": data.get("locations", {}),
                "stations_count": data.get("stations_count"),
                "radar_frames_summary": {
                    "past": len(data.get("radar_frames", {}).get("past", [])),
                    "nowcast": len(data.get("radar_frames", {}).get("nowcast", [])),
                    "forecast": len(data.get("radar_frames", {}).get("forecast", [])),
                },
                "last_update": data.get("last_update"),
            },
        },
        TO_REDACT,
    )
