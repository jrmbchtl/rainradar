"""CAMS UV background coordinator.

Checks every 2h whether a new CAMS run (00/12 UTC, available by 10:00/22:00)
has landed and fetches it once. Retains the last good series on failures so
sensors keep reporting through ADS queue hiccups.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import cams
from .const import (
    CAMS_UPDATE_INTERVAL,
    DOMAIN,
    resolve_location_specs,
)

_LOGGER = logging.getLogger(__name__)


class CamsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Twice-daily CAMS UV index fetch for all configured locations."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        credentials: dict[str, Any],
    ) -> None:
        self.entry = entry
        self._credentials = credentials
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_cams",
            update_interval=timedelta(seconds=CAMS_UPDATE_INTERVAL),
        )

    @property
    def _session(self):
        return aiohttp_client.async_get_clientsession(self.hass)

    async def _async_update_data(self) -> dict[str, Any]:
        locations = [
            (loc.latitude, loc.longitude)
            for loc in resolve_location_specs(self.hass, self.entry)
        ]
        if not locations:
            return {"locations": {}, "run_time": None}

        result = await cams.fetch_cams_uv(
            self._session,
            self._credentials["cams_api_token"],
            locations,
        )
        if result is None:
            # Keep last good data if we have any.
            if self.data and self.data.get("locations"):
                _LOGGER.debug("CAMS UV fetch failed; retaining previous series")
                return self.data
            return {"locations": {}, "run_time": None}

        return {
            "locations": {
                loc.loc_key: result.get((loc.latitude, loc.longitude), [])
                for loc in resolve_location_specs(self.hass, self.entry)
            },
            "run_time": cams.current_run().isoformat(),
        }
