"""Tests for sensor formatting, unique IDs and diagnostics."""

from __future__ import annotations

from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rainradar.const import DOMAIN
from tests.conftest import make_station
from tests.test_init import _patch_network

STATIONS = [
    make_station("01975", 52.47, 9.68, "Hannover"),
]


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, stations=STATIONS):
    patches = _patch_network()
    for p in patches.values():
        p.start()
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        for p in patches.values():
            p.stop()
    return patches


async def test_unique_ids_are_unique(hass: HomeAssistant, mock_config_entry) -> None:
    mock_config_entry.add_to_hass(hass)
    await _setup(hass, mock_config_entry)

    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entries = [
        e for e in registry.entities.values() if e.config_entry_id == mock_config_entry.entry_id
    ]
    unique_ids = [e.unique_id for e in entries]
    assert len(unique_ids) == len(set(unique_ids)), "duplicate unique_ids found"
    assert unique_ids  # entities were actually registered
    # has_entity_name convention: registry names come from device + entity
    assert all(e.has_entity_name for e in entries)


async def test_health_sensors_are_diagnostic_on_off(
    hass: HomeAssistant, mock_config_entry
) -> None:
    mock_config_entry.add_to_hass(hass)
    await _setup(hass, mock_config_entry)

    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for key in ("weather_health", "radar_health"):
        uid = f"rainradar_{key}"
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, uid)
        assert entity_id, f"{uid} missing"
        registry_entry = registry.async_get(entity_id)
        assert registry_entry.entity_category == EntityCategory.DIAGNOSTIC
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state in ("on", "off")


async def test_station_sensors_are_diagnostic(
    hass: HomeAssistant, mock_config_entry
) -> None:
    mock_config_entry.add_to_hass(hass)
    await _setup(hass, mock_config_entry)

    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    uid = "rainradar_zone_home_station_name"
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, uid)
    assert entity_id, f"{uid} missing"
    entry = registry.async_get(entity_id)
    assert entry.entity_category == EntityCategory.DIAGNOSTIC


async def test_diagnostics_redacts_and_reports(
    hass: HomeAssistant, mock_config_entry
) -> None:
    mock_config_entry.add_to_hass(hass)
    await _setup(hass, mock_config_entry)

    from custom_components.rainradar.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    diag = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    assert "config_entry" in diag
    assert "weather_data" in diag
    radar = diag["radar_data"]
    # Regression: old code read the dead 'forecasts_by_station' key.
    assert "forecast_locations" in radar
    assert radar["past"] == 0 and radar["nowcast"] == 0


async def test_weather_entity_available_at_zero_degrees(hass: HomeAssistant) -> None:
    """Regression: bool(0.0) is False made the entity unavailable at 0 °C."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Rainradar",
        version=2,
        data={},
        options={
            "locations": [],
            "zones": ["zone.home"],
            "device_trackers": [],
            "scan_interval": 600,
            "enable_forecast": True,
            "enable_icon_eu": False,
            "enable_uv": True,
            "enable_warnings": True,
            "enable_air_quality": False,
        },
    )
    entry.add_to_hass(hass)

    patches = _patch_network()

    async def _zero_obs(station_id):
        return {"temperature": 0.0, "humidity": 90.0}

    patches["fetch_obs"] = patches["fetch_obs"]  # keep reference
    for p in patches.values():
        p.start()
    # override after start
    patches["fetch_obs"].stop()
    from unittest.mock import patch as _patch

    async def _fetch_obs(self, station_id):
        return {"temperature": 0.0, "humidity": 90.0}

    zero_patch = _patch(
        "custom_components.rainradar.weather_coordinator.WeatherDataCoordinator._fetch_obs",
        new=_fetch_obs,
    )
    zero_patch.start()
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        zero_patch.stop()
        for p in patches.values():
            p.stop()

    weather_states = [
        s for s in hass.states.async_entity_ids("weather") if s.startswith("weather.")
    ]
    assert weather_states
    for entity_id in weather_states:
        state = hass.states.get(entity_id)
        assert state.state != "unavailable", (
            f"{entity_id} unavailable at 0.0 °C (bool() regression)"
        )
