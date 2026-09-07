"""Tests for the rainradar config and options flows."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rainradar.const import (
    CONF_DEVICE_TRACKERS,
    CONF_ENABLE_WARNINGS,
    CONF_SCAN_INTERVAL,
    CONF_ZONES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)


async def test_user_flow_shows_form(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    hass.states.async_set("zone.home", "zoning", {"latitude": 52.4, "longitude": 9.7})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ZONES: ["zone.home"],
            CONF_DEVICE_TRACKERS: [],
            CONF_SCAN_INTERVAL: 300,
            "enable_forecast": True,
            "enable_icon_eu": True,
            "enable_uv": True,
            CONF_ENABLE_WARNINGS: False,
            "enable_air_quality": True,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_SCAN_INTERVAL] == 300
    assert result["options"][CONF_ENABLE_WARNINGS] is False
    assert result["options"][CONF_DEVICE_TRACKERS] == []


async def test_options_flow_roundtrip(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Rainradar",
        version=2,
        data={},
        options={
            CONF_ZONES: ["zone.home"],
            CONF_DEVICE_TRACKERS: [],
            CONF_SCAN_INTERVAL: 600,
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_ZONES: ["zone.home"],
            CONF_DEVICE_TRACKERS: ["device_tracker.phone"],
            CONF_SCAN_INTERVAL: 1200,
            "enable_forecast": True,
            "enable_icon_eu": True,
            "enable_uv": False,
            CONF_ENABLE_WARNINGS: True,
            "enable_air_quality": True,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SCAN_INTERVAL] == 1200
    assert entry.options["enable_uv"] is False
    assert entry.options[CONF_DEVICE_TRACKERS] == ["device_tracker.phone"]


async def test_scan_interval_bounds_enforced(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "homeassistant.config_entries.ConfigFlow.async_show_form",
        side_effect=lambda **kwargs: kwargs,
    ):
        pass  # schema validation is exercised via options below
    # Invalid value must raise a form error, not crash.
    from voluptuous import Invalid

    schema = result["data_schema"]
    try:
        schema({CONF_ZONES: [], CONF_DEVICE_TRACKERS: [], CONF_SCAN_INTERVAL: 10})
    except Invalid:
        pass
    else:
        raise AssertionError("scan_interval below minimum should be rejected")
    assert schema({CONF_ZONES: [], CONF_DEVICE_TRACKERS: [], CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL})
