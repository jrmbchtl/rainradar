"""Tests for WeatherNext helpers (auth, discovery, parsing, frames)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from custom_components.rainradar import (
    weathernext as wn,
    wnauth,
    wnframes,
    wnzarr,
)
from custom_components.rainradar.const import WEATHERNEXT_STATS_BUCKET

UTC = UTC


@pytest.fixture(autouse=True)
def verify_cleanup():
    """Skip the plugin's lingering-thread/timer audit for this module.

    Writing the WN3 fixtures with the real ``zarr`` library spawns an executor
    thread on the test loop. That is harness noise from the fixture writer, not
    integration behaviour.
    """
    yield


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


def test_open_reader_is_authenticated():
    """Regression: every store read must send a bearer token.

    The bucket is allowlist-protected, so an unauthenticated read can never
    return data. This is the direct replacement for the old obstore store check
    (``skip_signature=True`` used to suppress the Authorization header entirely).
    """
    init_dt = datetime(2026, 9, 6, 6, tzinfo=UTC)
    reader = wn.open_reader(_session_ok(), init_dt, "ya29.token")
    assert isinstance(reader, wnzarr.RemoteZarrV3)
    assert reader._headers == {"Authorization": "Bearer ya29.token"}
    assert reader._base.startswith(
        f"https://{WEATHERNEXT_STATS_BUCKET}.storage.googleapis.com/"
    )
    assert reader._base.endswith("20260906_06hr_00_preds/predictions.zarr")


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


async def _local_wn_store(root, *, lats, lons, values, hours=48):
    """Write a minimal WN3-shaped Zarr v3 store with the real zarr library.

    Variable names follow the documented ``<name>_mean`` / ``<name>_p10`` layout
    and longitudes use the 0-360 convention, so this exercises name probing,
    dimension resolution and nearest-cell selection for real.
    """
    import zarr
    from zarr.codecs import ZstdCodec

    store = zarr.storage.LocalStore(str(root))
    group = zarr.open_group(store=store, mode="w", zarr_format=3)
    shape = (hours, len(lats), len(lons))
    for name, array in values.items():
        target = group.create_array(
            name=name,
            shape=shape,
            dtype="float32",
            chunks=(1, min(4, len(lats)), min(4, len(lons))),
            compressors=[ZstdCodec(level=1)],
            dimension_names=["lead_time", "latitude", "longitude"],
            fill_value=np.nan,
        )
        target[:] = array
    for name, coords in (("latitude", lats), ("longitude", lons)):
        c = group.create_array(
            name=name,
            shape=coords.shape,
            dtype=coords.dtype,
            dimension_names=[name],
            fill_value=np.nan,
        )
        c[:] = coords
    # Close so the executor thread does not outlive the test.
    store.close()
    return root


class _StoreReader(wnzarr.RemoteZarrV3):
    """A :class:`RemoteZarrV3` backed by a local directory."""

    def __init__(self, root) -> None:
        self._root = root
        self._headers: dict = {}
        self._meta_cache: dict = {}

    def _url(self, path: str) -> str:
        return str(self._root / path.lstrip("/"))

    async def _get_json(self, path: str) -> dict:
        import json

        target = self._root / path
        if not target.is_file():
            raise wnzarr.MissingArrayError(f"{path}: HTTP 404")
        return json.loads(target.read_text())

    async def _get_whole(self, path: str) -> bytes:
        target = self._root / path.lstrip("/")
        return target.read_bytes() if target.is_file() else b""

    async def _get_range(self, path: str, start: int, length: int) -> bytes:
        target = self._root / path.lstrip("/")
        if not target.is_file():
            return b""
        with target.open("rb") as fh:
            fh.seek(start)
            return fh.read(length)

    async def _get_tail(self, path: str, length: int) -> bytes:
        target = self._root / path.lstrip("/")
        if not target.is_file():
            return b""
        with target.open("rb") as fh:
            fh.seek(max(0, target.stat().st_size - length))
            return fh.read()


async def test_fetch_point_forecast_reads_a_real_store(tmp_path, monkeypatch):
    """End-to-end: parse a Zarr store into the internal forecast shape."""
    lats = np.linspace(50.0, 54.0, 41, dtype="float32")  # 0.1° grid
    lons = np.linspace(6.0, 10.0, 41, dtype="float32")  # 0-360 convention
    hours = 4
    shape = (hours, len(lats), len(lons))

    temp = np.full(shape, 293.15, dtype="float32")
    precip = np.full(shape, 0.002, dtype="float32")  # 2 mm/h in metres
    # A recognisable gradient so picking the wrong cell is visible.
    temp[:, :, :] += np.arange(shape[1], dtype="float32")[None, :, None] * 0.1

    store_root = await _local_wn_store(
        tmp_path / "wn.zarr",
        lats=lats,
        lons=lons,
        hours=hours,
        values={
            "temperature_2m_mean": temp,
            "temperature_2m_p10": temp - 1.0,
            "temperature_2m_p90": temp + 1.0,
            "total_precipitation_1hr_mean": precip,
        },
    )
    monkeypatch.setattr(
        wn, "open_reader", lambda *a, **k: _StoreReader(store_root)
    )

    init_dt = datetime(2026, 9, 6, 6, tzinfo=UTC)
    result = await wn.fetch_point_forecast(
        _session_ok(), "tok", init_dt, 52.0, 8.0, hours=hours
    )

    assert result is not None
    hourly = result["hourly"]
    assert len(hourly) == hours
    # Timestamps run init+1h .. init+4h.
    assert hourly[0]["ts"] == (init_dt + timedelta(hours=1)).timestamp()
    assert hourly[-1]["ts"] == (init_dt + timedelta(hours=hours)).timestamp()

    i_lat = int(np.abs(lats - 52.0).argmin())
    expected_temp = round(float(temp[0, i_lat, :].mean()) - 273.15, 1)
    assert hourly[0]["temperature"] == expected_temp
    # m → mm
    assert hourly[0]["precipitation"] == 2.0

    stats = result["hourly_stats"]
    assert set(stats) == {"p10", "p90"}
    assert stats["p10"][0]["temperature"] == pytest.approx(expected_temp - 1.0, abs=0.05)
    assert stats["p90"][0]["temperature"] == pytest.approx(expected_temp + 1.0, abs=0.05)


async def test_lon_180_is_not_wrapped(tmp_path, monkeypatch):
    """A store using -180..180 must not have its longitudes forced to 0..360.

    Converting unconditionally would wrap 180 -> 180 (fine by luck) but, worse,
    would break a -180..180 grid wherever it disagreed with the 0-360
    convention. Both layouts must resolve to the cell the user actually means.
    """
    lats = np.array([51.0, 52.0, 53.0], dtype="float32")
    lons = np.array([-2.0, -1.0, 0.0], dtype="float32")  # -180..180 convention
    shape = (2, len(lats), len(lons))
    data = np.zeros(shape, dtype="float32")
    data[:, 1, 1] = 293.15  # lat 52, lon -1

    root = await _local_wn_store(
        tmp_path / "signed.zarr",
        lats=lats,
        lons=lons,
        hours=2,
        values={"temperature_2m_mean": data},
    )
    monkeypatch.setattr(wn, "open_reader", lambda *a, **k: _StoreReader(root))

    # A negative longitude must resolve against the store's own convention.
    # Wrapping it to 359 would land on the far edge of a 0..360 grid instead.
    result = await wn.fetch_point_forecast(
        _session_ok(), "tok", datetime(2026, 9, 6, 6, tzinfo=UTC), 52.0, -1.0, hours=2
    )
    assert result is not None
    assert result["hourly"][0]["temperature"] == 20.0


async def test_lon_0_360_convention_wraps(tmp_path, monkeypatch):
    """A 0..360 store must wrap a negative longitude into range."""
    lats = np.array([51.0, 52.0, 53.0], dtype="float32")
    lons = np.array([0.0, 180.0, 359.0], dtype="float32")  # 0..360 convention
    shape = (2, len(lats), len(lons))
    data = np.zeros(shape, dtype="float32")
    data[:, 1, 2] = 288.15  # lat 52, lon 359 (== -1)

    root = await _local_wn_store(
        tmp_path / "wrapped.zarr",
        lats=lats,
        lons=lons,
        hours=2,
        values={"temperature_2m_mean": data},
    )
    monkeypatch.setattr(wn, "open_reader", lambda *a, **k: _StoreReader(root))

    result = await wn.fetch_point_forecast(
        _session_ok(), "tok", datetime(2026, 9, 6, 6, tzinfo=UTC), 52.0, -1.0, hours=2
    )
    assert result is not None
    assert result["hourly"][0]["temperature"] == 15.0


async def test_resolve_falls_back_to_the_bare_variable_name(
    tmp_path, monkeypatch
):
    """Some stores publish ``<name>`` instead of ``<name>_mean``."""
    lats = np.array([52.0], dtype="float32")
    lons = np.array([8.0], dtype="float32")
    data = np.full((1, 1, 1), 283.15, dtype="float32")

    root = await _local_wn_store(
        tmp_path / "bare.zarr",
        lats=lats,
        lons=lons,
        hours=1,
        values={"temperature_2m": data},
    )
    reader = _StoreReader(root)

    assert await wn._resolve(reader, "temperature_2m_mean") is None
    resolved = await wn._resolve(reader, "temperature_2m_mean", "temperature_2m")
    assert resolved is not None
    assert resolved[0] == "temperature_2m"


async def test_fetch_point_forecast_handles_bare_variable_names(
    tmp_path, monkeypatch
):
    """End-to-end with a store that only publishes ``<name>`` (no ``_mean``).

    Exercises the probe in ``fetch_point_forecast`` itself; the unit test on
    ``_resolve`` would not catch a call site that stopped passing the fallback.
    """
    lats = np.array([52.0], dtype="float32")
    lons = np.array([8.0], dtype="float32")
    data = np.full((1, 1, 1), 283.15, dtype="float32")

    root = await _local_wn_store(
        tmp_path / "bare-e2e.zarr",
        lats=lats,
        lons=lons,
        hours=1,
        values={"temperature_2m": data},
    )
    monkeypatch.setattr(wn, "open_reader", lambda *a, **k: _StoreReader(root))

    result = await wn.fetch_point_forecast(
        _session_ok(), "tok", datetime(2026, 9, 6, 6, tzinfo=UTC), 52.0, 8.0, hours=1
    )
    assert result is not None
    assert result["hourly"][0]["temperature"] == 10.0


async def test_fetch_point_forecast_passes_the_token_to_the_reader(monkeypatch):
    """The reader must be authenticated; the bucket rejects anonymous reads."""
    seen: dict = {}

    def _capture(session, init_dt, token):
        seen["token"] = token
        return wnzarr.RemoteZarrV3(session, "https://example.invalid", token)

    monkeypatch.setattr(wn, "open_reader", _capture)

    # No store at the (fake) base URL -> every probe 404s, but we still get here.
    with patch.object(
        wn, "_resolve", AsyncMock(return_value=None)
    ):
        result = await wn.fetch_point_forecast(
            _session_ok(), "ya29.the-token",
            datetime(2026, 9, 6, 6, tzinfo=UTC), 52.0, 8.0,
        )

    assert result is None
    assert seen["token"] == "ya29.the-token"


async def test_fetch_point_forecast_requires_a_token():
    """No credentials means no reads at all."""
    result = await wn.fetch_point_forecast(
        _session_ok(), None, datetime(2026, 9, 6, 6, tzinfo=UTC), 52.0, 8.0
    )
    assert result is None


async def test_fetch_point_forecast_handles_an_empty_store(tmp_path, monkeypatch):
    """A store with none of our variables must warn, not raise."""
    import zarr

    group = zarr.open_group(
        store=zarr.storage.LocalStore(str(tmp_path)), mode="w", zarr_format=3
    )
    group.create_array(
        name="something_else",
        shape=(2, 2, 2),
        dtype="float32",
        dimension_names=["lead_time", "latitude", "longitude"],
        fill_value=np.nan,
    )
    monkeypatch.setattr(wn, "open_reader", lambda *a, **k: _StoreReader(tmp_path))

    result = await wn.fetch_point_forecast(
        _session_ok(), "tok", datetime(2026, 9, 6, 6, tzinfo=UTC), 52.0, 8.0
    )
    assert result is None
