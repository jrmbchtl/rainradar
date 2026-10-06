"""WeatherNext 3 background coordinator.

Refreshes hourly (the model itself initializes hourly; data lands ~7-8h
after init). Holds the per-location hourly forecast plus the global
precipitation grid metadata for the card overlay.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import weathernext as wn, wnauth
from .const import (
    DOMAIN,
    WEATHERNEXT_UPDATE_INTERVAL,
    resolve_location_specs,
)

_LOGGER = logging.getLogger(__name__)


class WeatherNextCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Hourly WN3 forecast fetch for all configured locations."""

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
            name=f"{DOMAIN}_weathernext",
            update_interval=timedelta(seconds=WEATHERNEXT_UPDATE_INTERVAL),
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
            return {"locations": {}, "init_time": None}

        if wn.zarr_missing():
            # zarr/obstore cannot be auto-installed on every platform (musl).
            # Dormant mode: the one-time warning is logged by _warn_zarr_missing;
            # park the coordinator with empty data and no polling failure spam.
            self.update_interval = timedelta(seconds=WEATHERNEXT_UPDATE_INTERVAL * 24)
            wn._warn_zarr_missing()
            return {"locations": {}, "init_time": None}

        token = await wnauth.get_access_token(self._credentials, self._session)
        if token is None:
            # The refresh token is gone or was revoked. Ask the user to sign in
            # again — this raises the "Attention required" card on the Devices &
            # Services page, which the user clicks to open the Google consent
            # screen. Without this the entry just logs failures forever.
            self.config_entry.async_start_reauth_if_available(self.hass)
            raise UpdateFailed("WeatherNext token refresh failed")

        init_dt = await wn.find_latest_init(self._session, token)
        if init_dt is None:
            raise UpdateFailed("WeatherNext: no disseminated init found")

        try:
            per_location: dict[str, Any] = {}
            for loc in resolve_location_specs(self.hass, self.entry):
                forecast = await wn.fetch_point_forecast(
                    self._session,
                    token,
                    init_dt,
                    loc.latitude,
                    loc.longitude,
                    hours=48,
                )
                if forecast:
                    per_location[loc.loc_key] = forecast
        except Exception as exc:
            raise UpdateFailed(f"WeatherNext fetch failed: {exc}") from exc

        if not per_location:
            raise UpdateFailed("WeatherNext: no location data extracted")

        return {
            "locations": per_location,
            "init_time": init_dt.isoformat(),
        }
