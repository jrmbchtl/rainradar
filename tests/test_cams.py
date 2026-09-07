"""Tests for the CAMS UV layer (run rule, parsing, scaling)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import io
from unittest.mock import MagicMock
import zipfile

import numpy as np
import pytest

from custom_components.rainradar import cams

UTC = UTC


def _tiny_nc_bytes(uv_values, base_dt):
    """Build a minimal netCDF (via xarray) zipped as netcdf_zip."""
    import xarray as xr

    times = [base_dt + timedelta(hours=i + 1) for i in range(len(uv_values))]
    time_values = np.array(
        [t.strftime("%Y-%m-%dT%H:%M:%S") for t in times], dtype="datetime64[s]"
    )
    ds = xr.Dataset(
        {
            "uvbed": (("time", "latitude", "longitude"), np.array(uv_values).reshape(-1, 1, 1)),
        },
        coords={
            "time": time_values,
            "latitude": [52.47],
            "longitude": [9.68],
        },
    )
    buf = io.BytesIO()
    ds.to_netcdf(buf, engine="scipy")
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr("data.nc", buf.getvalue())
    return zbuf.getvalue()


def test_current_run_before_10am():
    now = datetime(2026, 9, 6, 8, 0, tzinfo=UTC)
    run = cams.current_run(now)
    assert run.day == 5 and run.hour == 12  # yesterday 12 UTC


def test_current_run_midday():
    now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    run = cams.current_run(now)
    assert run.day == 6 and run.hour == 0


def test_current_run_after_22():
    now = datetime(2026, 9, 6, 23, 0, tzinfo=UTC)
    run = cams.current_run(now)
    assert run.day == 6 and run.hour == 12


def test_current_run_after_10():
    now = datetime(2026, 9, 6, 10, 30, tzinfo=UTC)
    run = cams.current_run(now)
    assert run.day == 6 and run.hour == 0


def test_parse_netcdf_scales_by_40():
    base = datetime(2026, 9, 6, 0, tzinfo=UTC)
    values = [[0.0125], [0.025], [None]]  # → 0.5, 1.0 UV
    values = [[0.0125], [0.025]]
    data = _tiny_nc_bytes(values, base)
    out = cams._parse_netcdf_zip(data, [(52.47, 9.68)], base)
    series = out[(52.47, 9.68)]
    assert len(series) == 2
    assert series[0]["uv_index"] == pytest.approx(0.5)
    assert series[1]["uv_index"] == pytest.approx(1.0)
    # timestamps follow the encoded validity times (base+1h, +2h)
    assert series[0]["ts"] == pytest.approx((base + timedelta(hours=1)).timestamp())


def test_bbox_union():
    bbox = cams._bbox_for([(52.0, 9.0), (48.0, 11.0)])
    assert bbox[0] == pytest.approx(52.5)  # north
    assert bbox[1] == pytest.approx(8.5)  # west
    assert bbox[2] == pytest.approx(47.5)  # south
    assert bbox[3] == pytest.approx(11.5)  # east


async def test_preflight_rejects_bad_format():
    assert await cams.preflight("nosecret", MagicMock()) == "invalid_token_format"
    assert await cams.preflight("user@example.com:key", MagicMock()) is None


def test_request_body_shape():
    base = datetime(2026, 9, 6, 0, tzinfo=UTC)
    body = cams._request_body(base, [(52.0, 9.0)])
    assert body["date"] == "2026-09-06"
    assert body["time"] == "00:00"
    assert body["variable"] == "uv_biologically_effective_dose"
    assert body["type"] == "forecast"
    assert body["leadtime_hour"][0] == "1"
    assert body["leadtime_hour"][-1] == "120"
