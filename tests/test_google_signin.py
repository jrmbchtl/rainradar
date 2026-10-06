"""Integration test: the Google sign-in wiring resolves end to end.

Verifies that the `application_credentials` platform is discovered by HA and
that the resulting implementation builds a usable Google authorize URL — the
one path that is hard to exercise through the config flow alone, because the
flow tests mock the implementation out.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from homeassistant.components import application_credentials as ac
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.setup import async_setup_component

from custom_components.rainradar.const import DOMAIN


async def test_application_credentials_platform_resolves(
    hass: HomeAssistant,
) -> None:
    """HA discovers the rainradar platform and builds a Google authorize URL."""
    assert await async_setup_component(hass, "application_credentials", {})

    await ac.async_import_client_credential(
        hass,
        DOMAIN,
        ac.ClientCredential(
            client_id="cid.apps.googleusercontent.com", client_secret="csec"
        ),
    )

    implementations = await config_entry_oauth2_flow.async_get_implementations(
        hass, DOMAIN
    )
    assert DOMAIN in implementations
    impl = implementations[DOMAIN]
    assert impl.name == "Google (WeatherNext 3)"

    # Offline access is mandatory: without it Google issues no refresh token.
    assert impl.extra_authorize_data["access_type"] == "offline"
    assert impl.extra_authorize_data["prompt"] == "consent"
    assert impl.extra_authorize_data["scope"].endswith("/auth/devstorage.read_only")

    # The redirect URI must be this instance's HA callback endpoint.
    request = MagicMock(headers={"HA-Frontend-Base": "https://ha.example:8123"})
    with patch("homeassistant.helpers.http.current_request", MagicMock(get=MagicMock(return_value=request))):
        url = await impl.async_generate_authorize_url("flow-123")

    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth")
    assert "access_type=offline" in url
    assert "devstorage.read_only" in url
    # HA signs the flow id into the state JWT so the callback can resume it.
    assert "state=" in url
    assert "flow-123" not in url  # inside the signed JWT, not in cleartext
