"""Shared fixtures for the rainradar test suite."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rainradar.const import DOMAIN
from custom_components.rainradar.station_mapping import DWDStation

TEST_CONFIG_DIR = Path(__file__).parent / "testing_config"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading the rainradar custom integration in every test."""
    yield


@pytest.fixture(autouse=True)
def setup_zone_home(hass):
    """Provide zone.home so resolve_location_specs finds a location."""
    from homeassistant.const import (
        ATTR_LATITUDE,
        ATTR_LONGITUDE,
        STATE_HOME,
    )

    hass.states.async_set(
        "zone.home",
        STATE_HOME,
        {
            "friendly_name": "Home",
            ATTR_LATITUDE: 52.47,
            ATTR_LONGITUDE: 9.68,
            "radius": 100.0,
        },
    )
    yield


@pytest.fixture
def hass_config_dir() -> str:
    """Point HA's test config dir at tests/testing_config.

    That directory contains a ``custom_components`` symlink to the repo's
    integration so HA's loader can discover the ``rainradar`` domain.
    """
    return str(TEST_CONFIG_DIR)


def make_station(
    station_id: str, lat: float, lon: float, name: str = "Teststadt"
) -> DWDStation:
    return DWDStation(station_id, name, lat, lon)


@pytest.fixture
def stations() -> list[DWDStation]:
    """A tiny deterministic station catalog spanning Germany."""
    return [
        make_station("01001", 53.63, 10.00, "Hamburg-Fuhlsbüttel"),
        make_station("01048", 51.13, 6.80, "Düsseldorf"),
        make_station("01975", 52.47, 9.68, "Hannover"),
        make_station("01091", 48.41, 11.50, "München flug"),
        make_station("01092", 48.82, 9.15, "Schnarrenberg"),
        make_station("01053", 50.78, 6.05, "Aachen"),
    ]


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """A v2 mock entry whose options mirror the config flow output."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Rainradar",
        version=2,
        data={},
        options={
            "locations": [],
            "zones": ["zone.home"],
            "device_trackers": [],
            "device_tracker": None,
            "scan_interval": 600,
            "enable_forecast": True,
            "enable_icon_eu": False,
            "enable_uv": True,
            "enable_warnings": True,
            "enable_air_quality": False,
        },
    )


@pytest.fixture
def v1_config_entry() -> MockConfigEntry:
    """A legacy v1 entry using the old options keys."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Rainradar",
        version=1,
        data={},
        options={
            "locations": ["zone.home"],
            "device_tracker": "device_tracker.old_phone",
            "enable_forecast": True,
        },
    )
    return entry


@pytest.fixture
def disable_external_http():
    """Block real network access from coordinators by default."""
    with patch(
        "custom_components.rainradar.weather_coordinator.fetch_stations",
        return_value=[],
    ):
        yield
