"""Tests for sensor formatting, unique IDs and diagnostics."""

from __future__ import annotations

from datetime import UTC

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


async def test_wn_package_sensors_created_when_enabled(hass: HomeAssistant) -> None:
    """WN3 + packages enabled → package sensors registered and reporting."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Rainradar",
        version=3,
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
            "enable_weathernext": True,
            "enable_wn_overlay": True,
            "enable_pkg_solar": True,
            "enable_pkg_wind": True,
            "enable_pkg_probability": True,
            "enable_cams_uv": False,
            "wn_gcp_project_id": "proj",
        },
    )
    entry.add_to_hass(hass)

    from datetime import datetime

    from custom_components.rainradar.credentials import async_save_credentials

    await async_save_credentials(
        hass,
        entry.entry_id,
        {
            "wn_service_account_info": {
                "type": "service_account",
                "client_email": "sa@test.iam.googleapis.com",
                "private_key": "x",
                "token_uri": "https://oauth2.googleapis.com/token",
            },
            "wn_gcp_project_id": "proj",
        },
    )

    patches = _patch_network()
    for p in patches.values():
        p.start()
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)

        # Inject WN data directly into the coordinator before sensors read it.
        await hass.async_block_till_done()
        runtime = entry.runtime_data
        wn_coord = runtime.weathernext_coordinator
        assert wn_coord is not None
        now = datetime.now(UTC)
        wn_coord.data = {
            "locations": {
                "zone::zone.home": {
                    "hourly": [
                        {
                            "ts": now.timestamp(),
                            "temperature": 21.0,
                            "solar_ghi": 480.0,
                            "solar_direct": 300.0,
                            "cloud_cover_low": 10.0,
                            "cloud_cover_mid": 20.0,
                            "cloud_cover_high": 30.0,
                            "wind_speed_100m": 36.0,
                            "wind_direction_100m": 270.0,
                            "precipitation": 0.2,
                        }
                    ],
                    "hourly_stats": {
                        "p10": [{"ts": now.timestamp(), "temperature": 19.0}],
                        "p90": [
                            {"ts": now.timestamp(), "temperature": 31.0, "precipitation": 0.5}
                        ],
                    },
                }
            },
            "init_time": now.isoformat(),
        }
        wn_coord.last_update_success = True
        wn_coord.async_update_listeners()
        await hass.async_block_till_done()
    finally:
        for p in patches.values():
            p.stop()

    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for key, expected in (
        ("solar_ghi", "480.0"),
        ("solar_direct", "300.0"),
        ("wind_speed_100m", "36.0"),
        ("wind_direction_100m", "270.0"),
        ("cloud_cover_high", "30.0"),
        ("heat_risk_24h", "100.0"),
        ("rain_risk_24h", "100.0"),
    ):
        uid = f"rainradar_zone_home_{key}"
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, uid)
        assert entity_id, f"{uid} missing"
        state = hass.states.get(entity_id)
        assert state is not None and state.state == expected, (
            f"{uid}: {state.state if state else None} != {expected}"
        )


async def test_cams_uv_sensors_created_when_enabled(hass: HomeAssistant) -> None:
    """CAMS UV enabled → uv_index + uv_index_max_today sensors reporting."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Rainradar",
        version=3,
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
            "enable_weathernext": False,
            "enable_cams_uv": True,
        },
    )
    entry.add_to_hass(hass)

    from datetime import datetime

    from custom_components.rainradar.credentials import async_save_credentials

    await async_save_credentials(
        hass, entry.entry_id, {"cams_api_token": "user@example.com:secret"}
    )

    patches = _patch_network()
    for p in patches.values():
        p.start()
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        runtime = entry.runtime_data
        cams_coord = runtime.cams_coordinator
        assert cams_coord is not None
        now = datetime.now(UTC)
        # Deterministic "today" slots: 09:00 and 15:00 UTC of the current UTC day.
        slot1 = now.replace(hour=9, minute=0, second=0, microsecond=0)
        slot2 = now.replace(hour=15, minute=0, second=0, microsecond=0)
        cams_coord.data = {
            "locations": {
                "zone::zone.home": [
                    {"ts": slot1.timestamp(), "uv_index": 4.2},
                    {"ts": slot2.timestamp(), "uv_index": 6.0},
                ]
            },
            "run_time": now.isoformat(),
        }
        cams_coord.last_update_success = True
        cams_coord.async_update_listeners()
        await hass.async_block_till_done()
    finally:
        for p in patches.values():
            p.stop()

    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    uid = "rainradar_zone_home_uv_index"
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, uid)
    assert entity_id, f"{uid} missing"
    state = hass.states.get(entity_id)
    # uv_index picks the entry nearest 'now' within 3h; before 12:00 UTC that's
    # the 09:00 slot (4.2), after 18:00 neither slot is within 3h → 'unknown'.
    now_hour = datetime.now(UTC).hour
    if now_hour < 12:
        assert state.state == "4.2"
    elif now_hour >= 18:
        assert state.state == "unknown"
    else:
        assert state.state == "6.0"

    uid_max = "rainradar_zone_home_uv_index_max_today"
    entity_id_max = registry.async_get_entity_id("sensor", DOMAIN, uid_max)
    assert entity_id_max, f"{uid_max} missing"
    state_max = hass.states.get(entity_id_max)
    assert state_max.state == "6.0"


async def test_no_wn_or_cams_sensors_by_default(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """With everything disabled (default), no package/UV sensors are created."""
    mock_config_entry.add_to_hass(hass)
    await _setup(hass, mock_config_entry)

    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    all_uids = [
        e.unique_id
        for e in registry.entities.values()
        if e.config_entry_id == mock_config_entry.entry_id
    ]
    assert not any("solar_ghi" in u for u in all_uids)
    assert not any("wind_speed_100m" in u for u in all_uids)
    assert not any("rain_risk_24h" in u for u in all_uids)
