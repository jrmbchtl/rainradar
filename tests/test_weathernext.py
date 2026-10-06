"""Tests for WeatherNext helpers (auth, discovery, parsing, frames)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from custom_components.rainradar import weathernext as wn, wnauth, wnframes

UTC = UTC


def _google_creds() -> dict:
    """A stored OAuth credential as the sign-in flow writes it."""
    return {
        "wn_google_token": {
            "refresh_token": "1//refresh",
            "client_id": "cid.apps.googleusercontent.com",
            "client_secret": "secret",
            "token_uri": "https://oauth2.googleapis.com/token",
        },
        "wn_account_email": "user@example.com",
    }


def _session_post_ok(payload: dict | None = None) -> MagicMock:
    """A session whose token refresh returns ``payload``."""
    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(return_value=payload or {"access_token": "at", "expires_in": 3600})

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    post_ctx = MagicMock()
    post_ctx.__aenter__ = AsyncMock(return_value=resp)
    post_ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=post_ctx)
    return session


def _sa_info() -> dict:
    return {
        "type": "service_account",
        "client_email": "sa@example.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----\nX\n-----END PRIVATE KEY-----\n",
        "token_uri": "https://oauth2.googleapis.com/token",
    }


def _session_ok(status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status = status

    async def _aenter(_self=None):
        return resp

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(side_effect=lambda: resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    return session


def _patch_token(monkeypatch):
    async def _fake_token(info, session):
        return "fake-token"

    monkeypatch.setattr(wn, "get_access_token", _fake_token)


async def test_access_token_from_refresh_token(monkeypatch):
    """A stored refresh token is exchanged for a cached access token."""
    wnauth._TOKEN_CACHE.clear()
    session = _session_post_ok()
    token = await wnauth.get_access_token(_google_creds(), session)
    assert token == "at"
    # The refresh grant carries the client credentials and is cached afterwards.
    _, kwargs = session.post.call_args
    payload = kwargs["data"]
    assert payload["grant_type"] == "refresh_token"
    assert payload["refresh_token"] == "1//refresh"
    assert payload["client_secret"] == "secret"
    assert await wnauth.get_access_token(_google_creds(), session) == "at"
    assert session.post.call_count == 1  # served from cache
    wnauth._TOKEN_CACHE.clear()


async def test_access_token_refresh_failure_returns_none(monkeypatch):
    """A rejected refresh token yields None rather than raising."""
    wnauth._TOKEN_CACHE.clear()
    session = _session_post_ok()
    resp = MagicMock()
    resp.status = 401
    resp.text = AsyncMock(return_value="invalid_grant")
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session.post = MagicMock(return_value=ctx)
    assert await wnauth.get_access_token(_google_creds(), session) is None
    wnauth._TOKEN_CACHE.clear()


def test_has_credentials_and_describe():
    assert wnauth.has_credentials({}) is False
    assert wnauth.has_credentials(None) is False
    assert wnauth.has_credentials(_google_creds()) is True
    # A legacy service-account key still counts so nothing silently breaks.
    assert wnauth.has_credentials({"wn_service_account_info": {"a": 1}}) is True
    assert wnauth.describe({}) == "not_configured"
    assert wnauth.describe(_google_creds()) == "signed_in_as_user@example.com"


def test_store_credential_provider_shape():
    """obstore needs a str token plus a real datetime expiry."""
    provider = wnauth.store_credential_provider("ya29.token")
    cred = provider()
    assert cred["token"] == "ya29.token"
    assert isinstance(cred["expires_at"], datetime)
    assert cred["expires_at"] > datetime.now(UTC) + timedelta(minutes=30)


def test_open_stats_store_is_authenticated():
    """Regression: the store must send a bearer token, not read anonymously.

    The bucket is allowlist-protected, so an unauthenticated store can never
    return data. ``skip_signature=True`` must not be used here — obstore then
    omits the Authorization header entirely (verified against a local mock).
    """
    store = wn.open_stats_store(datetime(2026, 9, 6, 6, tzinfo=UTC), "ya29.token")
    if store is None:
        pytest.skip("zarr/obstore not installed")
    assert type(store).__name__ == "GCSStore"
    assert store.credential_provider is not None
    assert store.credential_provider()["token"] == "ya29.token"


async def test_find_latest_init_walks_back(monkeypatch):
    """Newest init found within the dissemination-lag window."""
    now = datetime(2026, 9, 6, 14, 20, tzinfo=UTC)
    # cutoff = 14:20 - 7h10m = 07:10 → probed inits: 07, 06, 05...
    # Simulate: 07 returns 404, 06 returns 200.
    seen = []

    def _get(url, **kwargs):
        seen.append(url)
        status = 404 if "20260906_07hr" in url else 200
        return _session_ok(status).get(url, **kwargs)

    monkeypatch.setattr(
        "custom_components.rainradar.weathernext.aiohttp.ClientSession", MagicMock
    )
    session = MagicMock()
    session.get = _get
    found = await wn.find_latest_init(session, "tok", now=now)
    assert found is not None
    assert found.hour == 6
    assert "20260906_06hr" in seen[-1]


async def test_find_latest_init_access_denied(monkeypatch):
    """401/403 during probing means allowlist missing → None (no retry storm)."""
    now = datetime(2026, 9, 6, 14, 20, tzinfo=UTC)
    session = _session_ok(403)
    found = await wn.find_latest_init(session, "tok", now=now)
    assert found is None
    assert session.get.call_count == 1


def test_lon360():
    assert wn._lon360(9.7) == pytest.approx(9.7)
    assert wn._lon360(-2.0) == pytest.approx(358.0)
    assert wn._lon360(200.0) == pytest.approx(200.0)


def test_surface_convert():
    assert wn._surface_convert("temperature_2m", 280.15) == pytest.approx(7.0, abs=0.1)
    assert wn._surface_convert("wind_speed_10m", 10.0) == pytest.approx(36.0)
    assert wn._surface_convert("surface_solar_radiation_downwards_1hr", 3600.0 * 500) == pytest.approx(500.0)
    assert wn._surface_convert("experimental_tp_1hr", 0.001) == pytest.approx(1.0)
    assert wn._surface_convert("total_cloud_cover", 0.5) == pytest.approx(50.0)


def test_parse_init_from_url():
    url = "https://weathernext3_statistics_spatial.storage.googleapis.com/weathernext_3_0_0_statistics/zarr/2026_to_present/20260906_06hr_00_preds/predictions.zarr"
    dt = wn._parse_init_from_url(url)
    assert dt is not None
    assert dt == datetime(2026, 9, 6, 6, tzinfo=UTC)


def test_stats_object_url_format():
    url = wn._stats_object_url(datetime(2026, 9, 6, 6, tzinfo=UTC))
    assert "20260906_06hr_00_preds/predictions.zarr/zarr.json" in url
    assert url.startswith("https://weathernext3_statistics_spatial.storage.googleapis.com/")


async def test_find_latest_init_probes_a_real_object():
    """Probing must target an object, not the bare directory prefix.

    The prefix form is not an object, so its 200-vs-404 is ambiguous; zarr.json
    gives a clean 200 / 403 / 404 signal.
    """
    now = datetime(2026, 9, 6, 14, 20, tzinfo=UTC)
    seen: list[str] = []

    def _get(url, **kwargs):
        seen.append(url)
        return _session_ok(404 if "07hr" in url else 200).get(url, **kwargs)

    session = MagicMock()
    session.get = _get
    found = await wn.find_latest_init(session, "tok", now=now)
    assert found is not None and found.hour == 6
    assert seen and all(url.endswith("/zarr.json") for url in seen)


def test_wnframes_grid_to_rgba():
    grid = np.full((4, 4), np.nan, dtype=np.float32)
    grid[0, 0] = 0.0  # below ramp → transparent
    grid[1, 1] = 1.0  # green band
    grid[2, 2] = 30.0  # red band
    rgba = wnframes.grid_to_rgba(grid)
    assert rgba.shape == (4, 4, 4)
    # transparent pixels
    assert rgba[0, 0, 3] == 0
    assert rgba[3, 3, 3] == 0
    # rain pixels are opaque and colored (not gray)
    assert rgba[1, 1, 3] == 255
    assert rgba[2, 2, 3] == 255
    assert not (rgba[2, 2, 0] == rgba[2, 2, 1] == rgba[2, 2, 2])


def test_zarr_missing_detection(monkeypatch):
    """zarr_missing() flips correctly based on import availability."""
    import builtins

    real_import = builtins.__import__

    def _blocking_import(name, *args, **kwargs):
        if name == "zarr" or name == "obstore":
            raise ImportError(f"blocked for test: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)
    assert wn.zarr_missing() is True


async def test_fetch_point_forecast_returns_none_when_zarr_missing(caplog):
    """Without zarr, point extraction returns None and warns once."""
    import logging

    with patch.object(wn, "zarr_missing", return_value=True), patch.object(
        wn, "_ZARR_WARNED", False
    ):
        with caplog.at_level(logging.WARNING, logger="custom_components.rainradar.weathernext"):
            result = await wn.fetch_point_forecast(
                _session_ok(), "tok", datetime(2026, 9, 6, 6, tzinfo=UTC), 52.0, 9.7
            )
    assert result is None
    assert any("optional packages 'zarr'" in r.message for r in caplog.records)
