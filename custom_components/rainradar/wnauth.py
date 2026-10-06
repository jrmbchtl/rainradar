"""Stored Google credentials and access-token acquisition for WeatherNext 3.

Two shapes of credential are understood, both living in the private
``.storage/rainradar_credentials`` store (never in entry options/diagnostics):

``wn_google_token``
    The current path. A ``{"refresh_token", "client_id", "client_secret", ...}``
    dict produced by the "Sign in with Google" OAuth2 flow. The access token is
    minted on demand and cached for most of its lifetime.

``wn_service_account_info``
    Legacy. A service-account JSON key, used before the OAuth flow existed.

The access token is used for every request to the allowlist-protected
statistics bucket: the discovery probe in ``find_latest_init`` / ``preflight``
and the Zarr reads in :mod:`.wnzarr`, which send it as a Bearer header.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
import time
from typing import Any

import aiohttp

from .const import WEATHERNEXT_OAUTH_SCOPE, WEATHERNEXT_TOKEN_URL

_LOGGER = logging.getLogger(__name__)

# Refresh this many seconds before the token actually expires.
_EXPIRY_MARGIN_S = 300

# client_id -> (expiry_monotonic, access_token)
_TOKEN_CACHE: dict[str, tuple[float, str]] = {}


def has_credentials(creds: dict[str, Any] | None) -> bool:
    """True when any usable WeatherNext credential is stored."""
    creds = creds or {}
    return bool(creds.get("wn_google_token") or creds.get("wn_service_account_info"))


def describe(creds: dict[str, Any] | None) -> str:
    """Human-readable credential status for the config flow's status field."""
    creds = creds or {}
    if account := creds.get("wn_account_email"):
        return f"signed_in_as_{account}"
    if creds.get("wn_google_token"):
        return "signed_in"
    if creds.get("wn_service_account_info"):
        return "service_account"
    return "not_configured"


def _clear_cached_token(client_id: str) -> None:
    """Drop any cached access token for a client (used after a failed refresh)."""
    _TOKEN_CACHE.pop(client_id, None)


async def get_access_token(
    credentials: dict[str, Any] | None,
    session: aiohttp.ClientSession,
) -> str | None:
    """Return a valid access token for the stored credential, or None.

    Tries the OAuth refresh token first and falls back to the legacy
    service-account key. The token is cached until shortly before expiry.
    """
    credentials = credentials or {}

    if google_token := credentials.get("wn_google_token"):
        return await _access_token_from_refresh_token(google_token, session)

    if service_account := credentials.get("wn_service_account_info"):
        return await _access_token_from_service_account(service_account)

    return None


async def _access_token_from_refresh_token(
    token_info: dict[str, Any],
    session: aiohttp.ClientSession,
) -> str | None:
    """Exchange the stored refresh token for an access token."""
    client_id = token_info.get("client_id") or ""
    refresh_token = token_info.get("refresh_token")
    if not refresh_token:
        _LOGGER.warning("WeatherNext: stored Google credential has no refresh token")
        return None

    cached = _TOKEN_CACHE.get(client_id)
    if cached and cached[0] > time.monotonic():
        return cached[1]

    payload = {
        "client_id": client_id,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    if client_secret := token_info.get("client_secret"):
        payload["client_secret"] = client_secret

    try:
        async with asyncio.timeout(30):
            async with session.post(WEATHERNEXT_TOKEN_URL, data=payload) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    _clear_cached_token(client_id)
                    _LOGGER.warning(
                        "WeatherNext token refresh failed (%s): %s",
                        resp.status,
                        body[:200],
                    )
                    return None
                data = await resp.json(content_type=None)
    except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
        _LOGGER.warning("WeatherNext token refresh failed: %s", exc)
        return None

    access_token = data.get("access_token")
    if not access_token:
        _clear_cached_token(client_id)
        _LOGGER.warning("WeatherNext token refresh returned no access token")
        return None

    expires_in = int(data.get("expires_in") or 3600)
    _TOKEN_CACHE[client_id] = (
        time.monotonic() + max(expires_in - _EXPIRY_MARGIN_S, 60),
        access_token,
    )
    return access_token


async def _access_token_from_service_account(
    service_account_info: dict[str, Any],
) -> str | None:
    """Mint an access token from a legacy service-account JSON key."""
    import google.auth.transport.requests
    from google.oauth2 import service_account

    client_email = service_account_info.get("client_email", "")

    cached = _TOKEN_CACHE.get(client_email)
    if cached and cached[0] > time.monotonic():
        return cached[1]

    def _sign() -> tuple[str, datetime | None]:
        creds = service_account.Credentials.from_service_account_info(
            service_account_info, scopes=[WEATHERNEXT_OAUTH_SCOPE]
        )
        request = google.auth.transport.requests.Request()
        creds.refresh(request)
        return creds.token, creds.expiry

    try:
        token, expiry = await asyncio.to_thread(_sign)
    except Exception as exc:
        _LOGGER.warning("WeatherNext token refresh failed: %s", exc)
        return None

    ttl = (
        (expiry - datetime.now(UTC)).total_seconds() - _EXPIRY_MARGIN_S
        if expiry
        else 3000
    )
    _TOKEN_CACHE[client_email] = (time.monotonic() + max(ttl, 60), token)
    return token
