"""Integration setup / unload / migration tests.

These are regression tests for the two worst historical bugs:
- unload calling a nonexistent ``async_close`` (crash on every unload/reload)
- ``async_migrate_entry`` living on the config flow class where HA never
  resolves it (v1 entries could not migrate)
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rainradar.const import DOMAIN
from tests.conftest import make_station

STATIONS = [
    make_station("01001", 53.63, 10.00, "Hamburg"),
    make_station("01975", 52.47, 9.68, "Hannover"),
]


def _patch_network():
    """Patch all outbound coordinator fetches with static data."""
    async def _fetch_stations(session):
        return STATIONS

    async def _fetch_obs(self, station_id):
        return {"temperature": 12.5, "humidity": 71.0}

    async def _fetch_daily(self, station_id):
        return {"pressure": 1013.0}

    async def _fetch_om(session, lat, lon):
        return None

    async def _fetch_frames(self, frames):
        return {"past": [], "nowcast": []}

    async def _no_op(*args, **kwargs):
        return None

    async def _noop_data(*args, **kwargs):
        return {}

    return {
        "fetch_stations": patch(
            "custom_components.rainradar.weather_coordinator.fetch_stations",
            new=_fetch_stations,
        ),
        "fetch_obs": patch(
            "custom_components.rainradar.weather_coordinator.WeatherDataCoordinator._fetch_obs",
            new=_fetch_obs,
        ),
        "fetch_daily": patch(
            "custom_components.rainradar.weather_coordinator.WeatherDataCoordinator._fetch_daily_data",
            new=_fetch_daily,
        ),
        "fetch_om": patch(
            "custom_components.rainradar.weather_coordinator.fetch_openmeteo_weather",
            new=_fetch_om,
        ),
        "mosmix": patch(
            "custom_components.rainradar.radar_coordinator.fetch_mosmix_forecasts",
            new=AsyncMock(return_value={}),
        ),
        "mosmix_ids": patch(
            "custom_components.rainradar.radar_coordinator.get_mosmix_station_ids",
            return_value=set(s.station_id for s in STATIONS),
        ),
        "frames": patch(
            "custom_components.rainradar.radar_coordinator.RadarDataCoordinator._prefetch_frames",
            new=_fetch_frames,
        ),
        "iconeu": patch(
            "custom_components.rainradar.radar_coordinator.fetch_icon_eu_precip",
            new=_noop_data,
        ),
        "warnings": patch(
            "custom_components.rainradar.radar_coordinator.fetch_dwd_warnings",
            new=_noop_data,
        ),
        "aq": patch(
            "custom_components.rainradar.radar_coordinator.fetch_openmeteo_air_quality",
            new=_noop_data,
        ),
    }


async def test_setup_and_unload_v2_entry(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    """Setup creates coordinators in runtime_data; unload does not crash."""
    mock_config_entry.add_to_hass(hass)
    patches = _patch_network()
    for p in patches.values():
        p.start()
    try:
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        assert mock_config_entry.state is ConfigEntryState.LOADED
        runtime = mock_config_entry.runtime_data
        assert runtime is not None
        assert runtime.weather_coordinator is not None
        assert runtime.radar_coordinator is not None
        # Shared station list got populated through the weather coordinator.
        assert runtime.stations == STATIONS
    finally:
        for p in patches.values():
            p.stop()

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_creates_sensors_and_weather_entities(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    patches = _patch_network()
    for p in patches.values():
        p.start()
    try:
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    finally:
        for p in patches.values():
            p.stop()

    state_ids = hass.states.async_entity_ids()
    assert any(e.startswith("sensor.") for e in state_ids)
    assert any(e.startswith("weather.") for e in state_ids)
    # Frames static path must live under the new (non-.storage) prefix.
    frame_sensors = [e for e in state_ids if "radar_frames" in e]
    assert frame_sensors


async def test_unload_without_setup_does_not_crash(hass: HomeAssistant) -> None:
    """Regression: unload used to raise AttributeError (async_close)."""
    entry = MockConfigEntry(domain=DOMAIN, title="Rainradar", version=2, data={}, options={})
    entry.add_to_hass(hass)
    patches = _patch_network()
    for p in patches.values():
        p.start()
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(entry.entry_id)
    finally:
        for p in patches.values():
            p.stop()


async def test_v1_entry_migrates_to_v3(
    hass: HomeAssistant, v1_config_entry: MockConfigEntry
) -> None:
    """Regression: migration hook must be resolved from __init__.py."""
    v1_config_entry.add_to_hass(hass)
    patches = _patch_network()
    for p in patches.values():
        p.start()
    try:
        assert await hass.config_entries.async_setup(v1_config_entry.entry_id)
        await hass.async_block_till_done()
    finally:
        for p in patches.values():
            p.stop()

    assert v1_config_entry.state is ConfigEntryState.LOADED
    assert v1_config_entry.version == 3
    # locations -> zones, legacy tracker mirrored
    assert v1_config_entry.options["zones"] == ["zone.home"]
    assert v1_config_entry.options["device_trackers"] == ["device_tracker.old_phone"]
    assert v1_config_entry.options["enable_warnings"] is True


async def test_migrate_rejects_future_version(hass: HomeAssistant) -> None:
    from custom_components.rainradar import async_migrate_entry

    entry = MockConfigEntry(domain=DOMAIN, title="Rainradar", version=4, data={}, options={})
    assert await async_migrate_entry(hass, entry) is False


async def test_reload_entry_via_update_listener(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Options updates must trigger a clean reload (no partial unload)."""
    mock_config_entry.add_to_hass(hass)
    patches = _patch_network()
    for p in patches.values():
        p.start()
    try:
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        hass.config_entries.async_update_entry(
            mock_config_entry, options={**mock_config_entry.options, "scan_interval": 900}
        )
        await hass.async_block_till_done()
        assert mock_config_entry.state is ConfigEntryState.LOADED
    finally:
        for p in patches.values():
            p.stop()


async def test_setup_survives_first_refresh_failure(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog
) -> None:
    """DWD being down at startup must not fail integration setup (WARNING only)."""
    mock_config_entry.add_to_hass(hass)

    async def _boom(self):
        raise RuntimeError("DWD down")

    patches = _patch_network()
    patches["first_refresh"] = patch(
        "custom_components.rainradar.weather_coordinator.WeatherDataCoordinator.async_config_entry_first_refresh",
        new=_boom,
    )
    for p in patches.values():
        p.start()
    try:
        with caplog.at_level(logging.WARNING):
            assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
            await hass.async_block_till_done()
    finally:
        for p in patches.values():
            p.stop()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert any("Initial weather refresh failed" in r.message for r in caplog.records)


async def test_v2_entry_migrates_to_v3(hass: HomeAssistant) -> None:
    """v2 entries gain the v3 experimental keys with safe defaults."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Rainradar",
        version=2,
        data={},
        options={"zones": ["zone.home"], "scan_interval": 600},
    )
    entry.add_to_hass(hass)
    patches = _patch_network()
    for p in patches.values():
        p.start()
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        for p in patches.values():
            p.stop()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.version == 3
    assert entry.options["enable_weathernext"] is False
    assert entry.options["enable_wn_overlay"] is True
    assert entry.options["enable_pkg_solar"] is False
    assert entry.options["enable_pkg_wind"] is False
    assert entry.options["enable_pkg_probability"] is False
    assert entry.options["enable_cams_uv"] is False
    assert entry.options["wn_gcp_project_id"] == ""
