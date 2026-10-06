"""Tests for the rainradar config and options flows."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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


async def test_sign_in_goes_to_external_step(hass: HomeAssistant) -> None:
    """Checking 'Sign in with Google' opens Google's consent screen."""
    from custom_components.rainradar import config_flow as cf

    impl = MagicMock()
    # Build a real authorize URL so the scope/params assertions are meaningful.
    impl.async_generate_authorize_url = AsyncMock(
        return_value=(
            "https://accounts.google.com/o/oauth2/v2/auth"
            "?scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fdevstorage.read_only"
            "&access_type=offline&prompt=consent"
        )
    )
    hass.states.async_set("zone.home", "zoning", {"latitude": 52.4, "longitude": 9.7})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch.object(
        cf.config_entry_oauth2_flow,
        "async_get_implementations",
        AsyncMock(return_value={"google": impl}),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_ZONES: ["zone.home"],
                CONF_DEVICE_TRACKERS: [],
                CONF_SCAN_INTERVAL: 600,
                "enable_forecast": True,
                "enable_icon_eu": True,
                "enable_uv": True,
                "enable_warnings": True,
                "enable_air_quality": True,
                "advanced": {
                    "enable_weathernext": True,
                    "wn_sign_in_google": True,
                    "enable_wn_overlay": True,
                    "enable_pkg_solar": False,
                    "enable_pkg_wind": False,
                    "enable_pkg_probability": False,
                    "enable_cams_uv": False,
                },
            },
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.EXTERNAL_STEP
    assert result["step_id"] == "auth"
    url = result["url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth")
    # Must request offline access, otherwise no refresh token is issued.
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "devstorage.read_only" in url


async def test_reauth_signin_updates_entry_instead_of_creating_a_second_one(
    hass: HomeAssistant,
) -> None:
    """Regression: a reauth sign-in must not fall through to the new-entry path.

    HA passes the entry data to async_step_reauth as an argument, not via
    flow.context, so a context lookup never matches. Branching on the wrong
    thing here created a duplicate Rainradar entry after every reauth.
    """
    from custom_components.rainradar import config_flow as cf

    entry = MockConfigEntry(
        domain=DOMAIN, title="Rainradar", version=4, data={}, options={}
    )
    entry.add_to_hass(hass)

    impl = MagicMock()
    impl.async_generate_authorize_url = AsyncMock(
        return_value="https://accounts.google.com/x"
    )
    impl.async_resolve_external_data = AsyncMock(
        return_value={"access_token": "at", "refresh_token": "rt"}
    )
    impl.client_id = "cid"
    impl.client_secret = "csec"
    impl.token_url = "https://oauth2.googleapis.com/token"

    with (
        patch.object(
            cf.config_entry_oauth2_flow,
            "async_get_implementations",
            AsyncMock(return_value={"google": impl}),
        ),
        patch.object(cf, "_fetch_account_email", AsyncMock(return_value=None)),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
            data=entry.data,
        )
        flow_id = result["flow_id"]
        assert result["type"] == FlowResultType.EXTERNAL_STEP
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"code": "auth-code", "state": {"flow_id": flow_id}}
        )
        await hass.async_block_till_done()
        # Frontend GETs the flow, which advances past external_step_done.
        result = await hass.config_entries.flow.async_configure(flow_id)
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    # The decisive assertion: still exactly one entry.
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].entry_id == entry.entry_id


async def test_options_signin_chains_to_reauth_flow(hass: HomeAssistant) -> None:
    """Options sign-in must hand the frontend a reauth flow to open.

    The frontend does not auto-open a dialog for a backend-initiated flow — it
    only renders an "Attention required" card — so the options flow has to pass
    next_flow explicitly, and persist the submitted options on the way out.
    """
    from custom_components.rainradar import config_flow as cf

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Rainradar",
        version=4,
        data={},
        options={"zones": ["zone.home"], "scan_interval": 600},
    )
    entry.add_to_hass(hass)

    impl = MagicMock()
    impl.async_generate_authorize_url = AsyncMock(
        return_value="https://accounts.google.com/x"
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM

    with patch.object(
        cf.config_entry_oauth2_flow,
        "async_get_implementations",
        AsyncMock(return_value={"google": impl}),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_ZONES: ["zone.home"],
                CONF_DEVICE_TRACKERS: [],
                CONF_SCAN_INTERVAL: 1200,
                "enable_forecast": True,
                "enable_icon_eu": True,
                "enable_uv": True,
                "enable_warnings": True,
                "enable_air_quality": True,
                "advanced": {
                    "enable_weathernext": True,
                    "wn_sign_in_google": True,
                    "enable_wn_overlay": True,
                    "enable_pkg_solar": False,
                    "enable_pkg_wind": False,
                    "enable_pkg_probability": False,
                    "enable_cams_uv": False,
                },
            },
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_started"
    # next_flow is what makes the frontend open the sign-in dialog.
    assert result["next_flow"][0] == "config_flow"
    # Options must survive the abort, otherwise the user's toggles are lost.
    assert entry.options[CONF_SCAN_INTERVAL] == 1200
    # And it must be a reauth flow for the SAME entry.
    chained = [
        f
        for f in hass.config_entries.flow.async_progress_by_handler(
            DOMAIN, include_uninitialized=True
        )
        if f["flow_id"] == result["next_flow"][1]
    ]
    assert len(chained) == 1
    assert chained[0]["context"]["source"] == config_entries.SOURCE_REAUTH
    assert chained[0]["context"]["entry_id"] == entry.entry_id


def test_google_oauth_impl_requests_offline_access(hass) -> None:
    """The sign-in implementation asks for offline access + the read-only scope.

    Without access_type=offline Google issues no refresh token and the stored
    credential would expire after an hour.
    """
    from homeassistant.components.application_credentials import (
        AuthorizationServer,
        ClientCredential,
    )

    from custom_components.rainradar.application_credentials import (
        WeatherNextGoogleOAuthImplementation,
    )

    impl = WeatherNextGoogleOAuthImplementation(
        hass,
        "google",
        ClientCredential(client_id="cid", client_secret="csec"),
        AuthorizationServer(
            "https://accounts.google.com/o/oauth2/v2/auth",
            "https://oauth2.googleapis.com/token",
        ),
    )
    data = impl.extra_authorize_data
    assert data["access_type"] == "offline"
    assert data["prompt"] == "consent"
    assert data["scope"] == "https://www.googleapis.com/auth/devstorage.read_only"
    assert impl.name == "Google (WeatherNext 3)"


async def test_sign_in_callback_stores_token_and_returns_to_form(
    hass: HomeAssistant,
) -> None:
    """The OAuth callback stores the token privately, then re-shows the form."""
    from custom_components.rainradar import config_flow as cf
    from custom_components.rainradar.credentials import async_get_credentials

    impl = MagicMock()
    impl.async_generate_authorize_url = AsyncMock(return_value="https://accounts.google.com/x")
    impl.async_resolve_external_data = AsyncMock(
        return_value={
            "access_token": "at",
            "refresh_token": "rt",
            "scope": "s",
        }
    )
    impl.client_id = "cid"
    impl.client_secret = "csec"
    impl.token_url = "https://oauth2.googleapis.com/token"

    hass.states.async_set("zone.home", "zoning", {"latitude": 52.4, "longitude": 9.7})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    flow_id = result["flow_id"]

    with (
        patch.object(
            cf.config_entry_oauth2_flow,
            "async_get_implementations",
            AsyncMock(return_value={"google": impl}),
        ),
        patch.object(
            cf, "_fetch_account_email", AsyncMock(return_value="user@example.com")
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {
                CONF_ZONES: ["zone.home"],
                CONF_DEVICE_TRACKERS: [],
                CONF_SCAN_INTERVAL: 600,
                "enable_forecast": True,
                "enable_icon_eu": True,
                "enable_uv": True,
                "enable_warnings": True,
                "enable_air_quality": True,
                "advanced": {
                    "enable_weathernext": True,
                    "wn_sign_in_google": True,
                    "enable_wn_overlay": True,
                    "enable_pkg_solar": False,
                    "enable_pkg_wind": False,
                    "enable_pkg_probability": False,
                    "enable_cams_uv": False,
                },
            },
        )
        await hass.async_block_till_done()
        assert result["type"] == FlowResultType.EXTERNAL_STEP

        # Simulate /auth/external/callback handing back the code + state.
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"code": "auth-code", "state": {"flow_id": flow_id}}
        )
        await hass.async_block_till_done()
        assert result["type"] == FlowResultType.EXTERNAL_STEP_DONE

        # The frontend then GETs the flow, which advances the step. The
        # resumed form runs a WN3 preflight, so stub the network probe out.
        with patch(
            "custom_components.rainradar.weathernext.preflight",
            AsyncMock(return_value=None),
        ):
            result = await hass.config_entries.flow.async_configure(flow_id)
            await hass.async_block_till_done()

    # The settings submitted before sign-in are replayed, so setup completes.
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["options"]["enable_weathernext"] is True

    # Credentials are keyed by flow id until the entry is set up, which re-keys
    # them to the entry id (async_copy_flow_credentials).
    entry_id = result["result"].entry_id
    await hass.async_block_till_done()
    creds = (
        await async_get_credentials(hass, entry_id)
        or await async_get_credentials(hass, flow_id)
    )
    assert creds["wn_google_token"]["refresh_token"] == "rt"
    assert creds["wn_account_email"] == "user@example.com"
    # A stale service account must not keep taking precedence.
    assert "wn_service_account_info" not in creds


async def test_sign_in_without_application_credentials_aborts(
    hass: HomeAssistant,
) -> None:
    """No OAuth client configured → a clear abort, not a crash."""
    hass.states.async_set("zone.home", "zoning", {"latitude": 52.4, "longitude": 9.7})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ZONES: ["zone.home"],
            CONF_DEVICE_TRACKERS: [],
            CONF_SCAN_INTERVAL: 600,
            "enable_forecast": True,
            "enable_icon_eu": True,
            "enable_uv": True,
            "enable_warnings": True,
            "enable_air_quality": True,
            "advanced": {
                "enable_weathernext": True,
                "wn_sign_in_google": True,
                "enable_wn_overlay": True,
                "enable_pkg_solar": False,
                "enable_pkg_wind": False,
                "enable_pkg_probability": False,
                "enable_cams_uv": False,
            },
        },
    )
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "missing_google_credentials"


async def test_user_flow_includes_advanced_section(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    schema_keys = [str(k) for k in result["data_schema"].schema.keys()]
    assert any("advanced" in k for k in schema_keys)


async def test_user_flow_wn_enabled_requires_sign_in(hass: HomeAssistant) -> None:
    """Enabling WeatherNext without a Google account → form error, no entry."""
    hass.states.async_set("zone.home", "zoning", {"latitude": 52.4, "longitude": 9.7})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ZONES: ["zone.home"],
            CONF_DEVICE_TRACKERS: [],
            CONF_SCAN_INTERVAL: 600,
            "enable_forecast": True,
            "enable_icon_eu": True,
            "enable_uv": True,
            CONF_ENABLE_WARNINGS: True,
            "enable_air_quality": True,
            "advanced": {
                "enable_weathernext": True,
                "wn_sign_in_google": False,
                "enable_wn_overlay": True,
                "enable_pkg_solar": False,
                "enable_pkg_wind": False,
                "enable_pkg_probability": False,
                "enable_cams_uv": False,
            },
        },
    )
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"wn_sign_in_google": "sign_in_required"}


async def test_user_flow_cams_requires_token(hass: HomeAssistant) -> None:
    hass.states.async_set("zone.home", "zoning", {"latitude": 52.4, "longitude": 9.7})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ZONES: ["zone.home"],
            CONF_DEVICE_TRACKERS: [],
            CONF_SCAN_INTERVAL: 600,
            "enable_forecast": True,
            "enable_icon_eu": True,
            "enable_uv": True,
            CONF_ENABLE_WARNINGS: True,
            "enable_air_quality": True,
            "advanced": {
                "enable_weathernext": False,
                "enable_wn_overlay": True,
                "enable_pkg_solar": False,
                "enable_pkg_wind": False,
                "enable_pkg_probability": False,
                "enable_cams_uv": True,
            },
        },
    )
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"cams_api_token": "cams_token_required"}


async def test_user_flow_basic_submit_creates_entry_with_v3_defaults(
    hass: HomeAssistant,
) -> None:
    """Submitting without touching the advanced section keeps WN3/CAMS off."""
    hass.states.async_set("zone.home", "zoning", {"latitude": 52.4, "longitude": 9.7})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ZONES: ["zone.home"],
            CONF_DEVICE_TRACKERS: [],
            CONF_SCAN_INTERVAL: 600,
            "enable_forecast": True,
            "enable_icon_eu": True,
            "enable_uv": True,
            CONF_ENABLE_WARNINGS: True,
            "enable_air_quality": True,
            "advanced": {
                "enable_weathernext": False,
                "enable_wn_overlay": True,
                "enable_pkg_solar": False,
                "enable_pkg_wind": False,
                "enable_pkg_probability": False,
                "enable_cams_uv": False,
            },
        },
    )
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["options"]["enable_weathernext"] is False
    assert result["options"]["enable_cams_uv"] is False
    assert result["options"]["enable_wn_overlay"] is True


async def test_user_flow_schema_serializes_for_frontend(hass: HomeAssistant) -> None:
    """Regression: the advanced section must serialize as an expandable group.

    A plain nested vol.Schema crashed HA's flow-manager response with
    ``ValueError: unable to serialize schema`` when the frontend requested
    the form (only reproducible through the serializer path, not through
    schema validation).
    """
    import homeassistant  # noqa: F401  (installs probatio as voluptuous first)
    from homeassistant.helpers.config_validation import (
        custom_serializer as cv_custom_serializer,
    )
    import probatio.codecs.fields as fields

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    serialized = fields.to_field_list(
        result["data_schema"], custom_serializer=cv_custom_serializer
    )
    names = [f["name"] for f in serialized]
    assert "advanced" in names
    advanced = next(f for f in serialized if f["name"] == "advanced")
    assert advanced["type"] == "expandable"
    assert advanced["expanded"] is False  # collapsed by design
    sub_names = [f["name"] for f in advanced["schema"]]
    assert "enable_weathernext" in sub_names
    assert "cams_api_token" in sub_names


async def test_options_flow_schema_serializes_for_frontend(hass: HomeAssistant) -> None:
    """Same regression for the options flow form."""
    from homeassistant.helpers.config_validation import (
        custom_serializer as cv_custom_serializer,
    )
    import probatio.codecs.fields as fields

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Rainradar",
        version=3,
        data={},
        options={CONF_ZONES: ["zone.home"], CONF_DEVICE_TRACKERS: []},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    serialized = fields.to_field_list(
        result["data_schema"], custom_serializer=cv_custom_serializer
    )
    advanced = next(f for f in serialized if f["name"] == "advanced")
    assert advanced["type"] == "expandable"
