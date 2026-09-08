"""Tests for the MOSMIX-S fetch/parse layer (custom_components/rainradar/mosmix.py)."""

from __future__ import annotations

import io
import logging
import time
from unittest.mock import AsyncMock, MagicMock
import zipfile

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


def _session_with_responses(responses: list[MagicMock]) -> MagicMock:
    """Session mock popping one prepared response per request."""

    def _get(url, **_kwargs):
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(side_effect=lambda: responses.pop(0))
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    session = MagicMock()
    session.get = MagicMock(side_effect=_get)
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


async def test_fetch_forecast_parses_and_scales():
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


async def test_stream_parse_indexes_all_stations():
    """The streaming parser records the full station index (needed for
    candidate filtering) while retaining only wanted forecast series."""

    kmz = zipfile.ZipFile(io.BytesIO(SAMPLE_KMZ))
    kml_name = next(n for n in kmz.namelist() if n.endswith(".kml"))
    with kmz.open(kml_name) as fh:
        run = mosmix._parse_mosmix_kml_stream(fh, wanted={"01001"})
    assert run.station_ids == {"01001", "01048"}
    assert set(run.forecasts) == {"01001"}
    assert len(run.forecasts["01001"]) == 4


async def test_batched_fetch_returns_all_wanted():
    session = _session_with_kmz(SAMPLE_KMZ)
    result = await mosmix.fetch_mosmix_forecasts(session, {"01001", "01048", "99999"})
    assert set(result) == {"01001", "01048"}  # 99999 not in fixture
    assert len(result["01001"]) == 4
    assert result["01048"][0]["temperature"] == 12.0


async def test_batched_fetch_single_http_call_for_two_stations():
    """Regression: multiple stations must share ONE download per hour."""
    session = _session_with_kmz(SAMPLE_KMZ)
    await mosmix.fetch_mosmix_forecasts(session, {"01001", "01048"})
    assert session.get.call_count == 1


async def test_station_not_found_is_debug_not_warning(caplog):
    """CDC stations absent from MOSMIX are expected — must not log WARNING."""
    session = _session_with_kmz(SAMPLE_KMZ)
    with caplog.at_level(logging.DEBUG, logger="custom_components.rainradar.mosmix"):
        result = await mosmix.fetch_mosmix_forecast(session, "00044")
    assert result is None
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings


async def test_batched_call_uses_cache_without_http():
    """Regression: one batched call serves all wanted stations with a single
    download; a follow-up batch fully covered by the cache adds zero calls."""
    session = _session_with_kmz(SAMPLE_KMZ)
    first = await mosmix.fetch_mosmix_forecasts(session, {"01001", "01048"})
    assert set(first) == {"01001", "01048"}
    calls_after_first = session.get.call_count
    second = await mosmix.fetch_mosmix_forecasts(session, {"01001", "01048"})
    assert set(second) == {"01001", "01048"}
    assert session.get.call_count == calls_after_first


async def test_latest_fallback_on_missing_run():
    """Exact run 404s → falls back to MOSMIX_S_LATEST_240.kmz."""
    ok_resp = MagicMock()
    ok_resp.status = 200
    ok_resp.read = AsyncMock(return_value=SAMPLE_KMZ)
    not_found = MagicMock()
    not_found.status = 404
    session = _session_with_responses([not_found, ok_resp])

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
    old_ts, run = mosmix._kml_cache[run_key]

    # Age the cache entry beyond the max age, but keep a fresh entry present
    # so no refetch happens — then the stale entry must be gone.
    mosmix._kml_cache[run_key] = (
        old_ts - mosmix.MOSMIX_CACHE_MAX_AGE - 10,
        run,
    )
    # A fresh dummy run (empty forecasts) is now the freshest cache entry —
    # the batched fetch sees its wanted station missing and refetches, which
    # also evicts the aged entry. Result: stale gone, fresh refetched entry
    # present and serving 01001.
    mosmix._kml_cache["2026010100"] = (time.time(), mosmix._MosmixRun(time.time()))
    result = await mosmix.fetch_mosmix_forecasts(session, {"01001"})

    assert "2026010100" in mosmix._kml_cache
    assert result.get("01001"), "refetch must serve 01001"
    assert run_key not in mosmix._kml_cache or (
        time.time() - mosmix._kml_cache[run_key][0] < mosmix.MOSMIX_CACHE_MAX_AGE
    )


async def test_get_mosmix_station_ids_fresh_and_stale():
    assert mosmix.get_mosmix_station_ids() is None
    run = mosmix._MosmixRun(time.time())
    run.station_ids = {"01001"}
    mosmix._kml_cache["2026090607"] = (time.time(), run)
    ids = mosmix.get_mosmix_station_ids()
    assert ids is not None and "01001" in ids
    # Stale beyond the 2h window is not returned.
    run2 = mosmix._MosmixRun(time.time() - mosmix.MOSMIX_UPDATE_INTERVAL - 60)
    run2.station_ids = {"01001"}
    mosmix._kml_cache["2026090607"] = (time.time() - mosmix.MOSMIX_UPDATE_INTERVAL - 60, run2)
    assert mosmix.get_mosmix_station_ids() is None


async def test_stream_parse_memory_bounded():
    """The streaming parser must stay far below the old full-DOM footprint."""
    # Sanity guard on the big real-world file when available: 5,648 stations
    # x 240h x 16 vars used to retain 1.2GB; the streaming parse peaks at ~2MB.
    import os

    big = "/tmp/opencode/mosmix_latest.kmz"
    if not os.path.exists(big):
        pytest.skip("real MOSMIX file not available")
    import tracemalloc

    kmz = zipfile.ZipFile(big)
    kml_name = next(n for n in kmz.namelist() if n.endswith(".kml"))
    tracemalloc.start()
    with kmz.open(kml_name) as fh:
        run = mosmix._parse_mosmix_kml_stream(fh, wanted={"01001", "01048"})
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(run.station_ids) == 5648
    assert peak < 50 * 1024 * 1024, f"stream parse peak too high: {peak/1e6:.0f}MB"
