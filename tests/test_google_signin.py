"""Integration test: the Google sign-in wiring resolves end to end.

Verifies that the `application_credentials` platform is discovered by HA and
that the resulting implementation builds a usable Google authorize URL — the
one path that is hard to exercise through the config flow alone, because the
flow tests mock the implementation out.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from homeassistant.components import application_credentials as ac
from homeassistant.components.application_credentials import (
    AuthorizationServer,
    ClientCredential,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.setup import async_setup_component
import pytest

from custom_components.rainradar.application_credentials import (
    WeatherNextGoogleOAuthImplementation,
)
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
    assert "my.home-assistant.io" not in url
    # HA signs the flow id into the state JWT so the callback can resume it.
    assert "state=" in url
    assert "flow-123" not in url  # inside the signed JWT, not in cleartext


def _impl(hass) -> WeatherNextGoogleOAuthImplementation:
    """Build the implementation with a dummy credential."""
    return WeatherNextGoogleOAuthImplementation(
        hass,
        "rainradar",
        ClientCredential(client_id="cid", client_secret="csec"),
        AuthorizationServer(
            "https://accounts.google.com/o/oauth2/v2/auth",
            "https://oauth2.googleapis.com/token",
        ),
    )


def test_redirect_uri_prefers_external_url(hass: HomeAssistant) -> None:
    """Google must be sent the instance's own callback, not my.home-assistant.io.

    HA's default resolves to https://my.home-assistant.io/redirect/oauth
    whenever the `my` component is loaded (it ships with default_config, so
    effectively always). That needs Nabu Casa to resolve the instance and does
    not match the URI the user registers — Google answers redirect_uri_mismatch.
    """
    hass.config.external_url = "https://homeassistant.bechtle.land"
    assert (
        _impl(hass).redirect_uri
        == "https://homeassistant.bechtle.land/auth/external/callback"
    )


def test_redirect_uri_strips_trailing_slash(hass: HomeAssistant) -> None:
    """A trailing slash would send "...//auth/external/callback" and be rejected."""
    hass.config.external_url = "https://homeassistant.example.com/"
    assert (
        _impl(hass).redirect_uri
        == "https://homeassistant.example.com/auth/external/callback"
    )


def test_redirect_uri_falls_back_to_internal_url(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Without external_url the internal one is used, with a warning."""
    import logging

    hass.config.external_url = None
    hass.config.internal_url = "http://homeassistant.local:8123"
    with caplog.at_level(logging.WARNING, logger="custom_components.rainradar"):
        uri = _impl(hass).redirect_uri
    assert uri == "http://homeassistant.local:8123/auth/external/callback"
    assert any("No external_url configured" in r.message for r in caplog.records)


def test_redirect_uri_falls_back_to_ha_default(hass: HomeAssistant) -> None:
    """With neither URL configured, defer to HA's own resolution."""
    import homeassistant.helpers.config_entry_oauth2_flow as oauth_flow

    hass.config.external_url = None
    hass.config.internal_url = None
    request = MagicMock(headers={"HA-Frontend-Base": "https://ha.example:8123"})
    with patch(
        "homeassistant.helpers.http.current_request",
        MagicMock(get=MagicMock(return_value=request)),
    ):
        assert _impl(hass).redirect_uri.endswith(oauth_flow.AUTH_CALLBACK_PATH)


async def test_authorize_url_carries_the_instance_redirect_uri(
    hass: HomeAssistant,
) -> None:
    """End-to-end: the URL Google receives must contain the instance callback.

    This is the exact shape of the reported `redirect_uri_mismatch`: the user
    registers <instance>/auth/external/callback but HA sends
    my.home-assistant.io/redirect/oauth.
    """
    hass.config.external_url = "https://homeassistant.bechtle.land"
    request = MagicMock(headers={"HA-Frontend-Base": "https://ignored.example"})
    with patch(
        "homeassistant.helpers.http.current_request",
        MagicMock(get=MagicMock(return_value=request)),
    ):
        url = await _impl(hass).async_generate_authorize_url("flow-123")

    from urllib.parse import parse_qs, urlparse

    redirect_uri = parse_qs(urlparse(url).query)["redirect_uri"][0]
    assert redirect_uri == "https://homeassistant.bechtle.land/auth/external/callback"
    assert "my.home-assistant.io" not in url
