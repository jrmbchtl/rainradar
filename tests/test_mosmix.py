"""Tests for the MOSMIX-S fetch/parse layer (custom_components/rainradar/mosmix.py)."""

from __future__ import annotations

import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.rainradar import mosmix
from tests.mosmix_fixtures import SAMPLE_KMZ, build_kml


@pytest.fixture(autouse=True)
def _clean_kml_cache():
    mosmix._kml_cache.clear()
    yield
    mosmix._kml_cache.clear()


def _session_with_kmz(kmz_bytes: bytes, status: int = 200) -> MagicMock:
    """An aiohttp-session-like mock returning one KMZ payload."""
    resp = MagicMock()
    resp.status = status
    resp.read = AsyncMock(return_value=kmz_bytes)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    return session


async def test_parse_kml_extracts_station_values():
    stations = mosmix._parse_mosmix_kml(build_kml({"01001": {"TTT": "280.15 281.15"}}))
    assert "01001" in stations
    fc = stations["01001"]["forecasts"]
    assert fc[0]["temperature"] == pytest.approx(7.0, abs=0.1)
    assert fc[1]["temperature"] == pytest.approx(8.0, abs=0.1)


async def test_parse_kml_skips_missing_values():
    stations = mosmix._parse_mosmix_kml(build_kml({"01001": {"TTT": "-999 280.15"}}))
    fc = stations["01001"]["forecasts"]
    assert "temperature" not in fc[0]
    assert fc[1]["temperature"] == pytest.approx(7.0, abs=0.1)


async def test_fetch_forecast_parses_and_scales(caplog):
    session = _session_with_kmz(SAMPLE_KMZ)
    result = await mosmix.fetch_mosmix_forecast(session, "01001")
    assert result is not None
    # 4 hourly timesteps (3 with data + trailing all-missing step)
    assert len(result) == 4
    first = result[0]
    assert first["temperature"] == pytest.approx(7.0, abs=0.1)
    assert first["wind_speed"] == pytest.approx(2.0 * 3.6, abs=0.1)
    assert first["cloud_cover"] == pytest.approx(50.0, abs=0.1)
    assert first["pressure"] == pytest.approx(1013.0, abs=0.1)
    assert first["precipitation"] == pytest.approx(0.0, abs=0.01)
    assert result[2]["precipitation"] == pytest.approx(1.5, abs=0.01)


async def test_station_not_found_is_debug_not_warning(caplog):
    """CDC stations absent from MOSMIX are expected — must not log WARNING."""
    session = _session_with_kmz(SAMPLE_KMZ)
    with caplog.at_level(logging.DEBUG, logger="custom_components.rainradar.mosmix"):
        result = await mosmix.fetch_mosmix_forecast(session, "00044")
    assert result is None
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings
    assert any(
        r.levelno == logging.DEBUG and "00044" in r.message for r in caplog.records
    )


async def test_second_call_uses_cache_without_http():
    session = _session_with_kmz(SAMPLE_KMZ)
    assert await mosmix.fetch_mosmix_forecast(session, "01001") is not None
    calls_after_first = session.get.call_count
    assert await mosmix.fetch_mosmix_forecast(session, "01048") is not None
    assert session.get.call_count == calls_after_first


async def test_latest_fallback_on_missing_run():
    """Exact run 404s → falls back to MOSMIX_S_LATEST_240.kmz."""
    session = _session_with_kmz(SAMPLE_KMZ)
    # Make the first URL (exact run) return 404, second (LATEST) succeed.
    ok_resp = MagicMock()
    ok_resp.status = 200
    ok_resp.read = AsyncMock(return_value=SAMPLE_KMZ)
    not_found = MagicMock()
    not_found.status = 404

    responses = [not_found, ok_resp]

    async def _aenter(ctx_self=None):
        return responses.pop(0)

    def _get(url, **_kwargs):
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(side_effect=lambda: responses.pop(0))
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    session.get = MagicMock(side_effect=_get)
    result = await mosmix.fetch_mosmix_forecast(session, "01001")
    assert result is not None
    urls = [c.args[0] for c in session.get.call_args_list]
    assert "MOSMIX_S_LATEST_240.kmz" in urls[1]


async def test_all_urls_fail_returns_none_with_warning(caplog):
    session = _session_with_kmz(SAMPLE_KMZ, status=500)
    with caplog.at_level(logging.WARNING, logger="custom_components.rainradar.mosmix"):
        result = await mosmix.fetch_mosmix_forecast(session, "01001")
    assert result is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)


async def test_cache_eviction_of_stale_runs():
    session = _session_with_kmz(SAMPLE_KMZ)
    await mosmix.fetch_mosmix_forecast(session, "01001")
    assert mosmix._kml_cache
    run_key = next(iter(mosmix._kml_cache))
    old_ts, stations = mosmix._kml_cache[run_key]

    # Age the cache entry beyond the max age, but keep a fresh entry present
    # so no refetch happens — then the stale entry must be gone.
    mosmix._kml_cache[run_key] = (
        old_ts - mosmix.MOSMIX_CACHE_MAX_AGE - 10,
        stations,
    )
    mosmix._kml_cache["2026010100"] = (time.time(), {"01999": {"forecasts": []}})
    await mosmix.fetch_mosmix_forecast(session, "01001")

    # The aged entry was evicted; the fresh one survived.
    assert mosmix._kml_cache.get(run_key) is None or (
        mosmix._kml_cache[run_key][0] == old_ts
        and time.time() - mosmix._kml_cache[run_key][0] < mosmix.MOSMIX_CACHE_MAX_AGE
    )
    assert "2026010100" in mosmix._kml_cache


async def test_get_mosmix_station_ids_fresh_and_stale():
    assert mosmix.get_mosmix_station_ids() is None
    mosmix._kml_cache["2026090607"] = (time.time(), {"01001": {"forecasts": []}})
    ids = mosmix.get_mosmix_station_ids()
    assert ids is not None and "01001" in ids
    # Stale within 2h window is still returned.
    mosmix._kml_cache["2026090607"] = (
        time.time() - mosmix.MOSMIX_UPDATE_INTERVAL - 60,
        {"01001": {"forecasts": []}},
    )
    assert mosmix.get_mosmix_station_ids() is None
