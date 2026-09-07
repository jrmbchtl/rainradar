"""CAMS global UV index via the Copernicus Atmosphere Data Store (ADS).

Fetches ``uv_biologically_effective_dose`` (all-sky, param 214002) from the
``cams-global-atmospheric-composition-forecasts`` dataset and converts it to
the WHO UV index (x 40). Clear-sky UV is deliberately not fetched.

The ADS API is synchronous (cdsapi) — all calls run via
``hass.async_add_executor_job``. Requests use one bounding box covering all
configured locations to keep queue volume at one per update.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import io
import logging
from typing import Any
import zipfile

import aiohttp

from .const import (
    CAMS_ADS_URL,
    CAMS_DATASET,
    CAMS_LEADTIME_HOURS,
    CAMS_UV_SCALE,
)

_LOGGER = logging.getLogger(__name__)

_TIMEOUT_S = 900  # ADS queueing can be slow; generous cap per request


def current_run(now: datetime | None = None) -> datetime:
    """Return the newest CAMS forecast base time that should be available.

    CAMS issues 00/12 UTC runs; 00 is available by 10:00 UTC and 12 by 22:00.
    """
    if now is None:
        now = datetime.now(UTC)
    hour = now.hour
    if hour >= 22:
        base = now.replace(hour=12, minute=0, second=0, microsecond=0)
    elif hour >= 10:
        base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        prev = now - timedelta(days=1)
        base = prev.replace(hour=12, minute=0, second=0, microsecond=0)
    return base


def _bbox_for(locations: list[tuple[float, float]], margin: float = 0.5) -> list[float]:
    """Union bounding box [North, West, South, East] over all locations."""
    lats = [lat for lat, _ in locations]
    lons = [lon for _, lon in locations]
    return [
        min(90.0, max(lats) + margin),
        max(-180.0, min(lons) - margin),
        max(-90.0, min(lats) - margin),
        min(180.0, max(lons) + margin),
    ]


def _request_body(
    base_dt: datetime, locations: list[tuple[float, float]]
) -> dict[str, Any]:
    return {
        "date": base_dt.strftime("%Y-%m-%d"),
        "time": base_dt.strftime("%H:%M"),
        "leadtime_hour": [str(h) for h in range(1, CAMS_LEADTIME_HOURS + 1)],
        "type": "forecast",
        "variable": "uv_biologically_effective_dose",
        "area": _bbox_for(locations),
        "format": "netcdf_zip",
    }


def _parse_netcdf_zip(
    data: bytes, locations: list[tuple[float, float]], base_dt: datetime
) -> dict[tuple[float, float], list[dict]]:
    """Parse the netcdf_zip response → {location: [hourly uv entries]}."""
    import xarray as xr

    out: dict[tuple[float, float], list[dict]] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        nc_name = next(n for n in zf.namelist() if n.endswith(".nc"))
        ds = xr.open_dataset(io.BytesIO(zf.read(nc_name)))

    uv_var = next(
        (v for v in ds.data_vars if "uvbed" in v.lower() or "uv" in v.lower()),
        None,
    )
    if uv_var is None:
        raise ValueError("uv_bed variable not found in CAMS response")

    da = ds[uv_var]
    lat_name = next(d for d in da.dims if d.lower().startswith("lat"))
    lon_name = next(d for d in da.dims if d.lower().startswith("lon"))
    time_name = next((d for d in da.dims if d.lower().startswith("time")), None)
    if time_name is None:
        # lead-time dimension fallback
        time_name = next(d for d in da.dims if d != lat_name and d != lon_name)

    times = ds[time_name].values
    for lat, lon in locations:
        point = da.sel({lat_name: lat, lon_name: lon}, method="nearest")
        values = point.values
        series: list[dict] = []
        for i, v in enumerate(values):
            try:
                if v is None or v != v or v < 0:
                    continue
                ts = (
                    base_dt + timedelta(hours=i + 1)
                ).timestamp()
                series.append({"ts": ts, "uv_index": round(float(v) * CAMS_UV_SCALE, 1)})
            except (TypeError, ValueError):
                continue
        # Some CAMS netCDFs already carry absolute validity times; when the
        # first decoded time is not near base_dt, trust the encoded times.
        if len(times) >= len(series) and series:
            try:
                first = _to_ts(times[0])
                if first is not None and abs(first - series[0]["ts"]) > 5400:
                    series = []
                    for i, v in enumerate(values[: len(times)]):
                        ts = _to_ts(times[i])
                        if ts is None or v is None or v != v or v < 0:
                            continue
                        series.append(
                            {"ts": ts, "uv_index": round(float(v) * CAMS_UV_SCALE, 1)}
                        )
            except (TypeError, ValueError):
                pass
        out[(lat, lon)] = series

    ds.close()
    return out


def _to_ts(value: Any) -> float | None:
    """Best-effort conversion of a netCDF time value to a POSIX timestamp."""
    import numpy as np

    try:
        return float(np.datetime64(value, "s").astype("datetime64[s]").astype(float))
    except (TypeError, ValueError):
        return None


async def fetch_cams_uv(
    session: aiohttp.ClientSession,
    api_token: str,
    locations: list[tuple[float, float]],
    now: datetime | None = None,
) -> dict[tuple[float, float], list[dict]] | None:
    """Fetch the hourly all-sky UV index series for every location.

    Returns ``{(lat, lon): [{"ts": ..., "uv_index": ...}, ...]}`` or None.
    """
    if not locations:
        return None

    base_dt = current_run(now)
    body = _request_body(base_dt, locations)

    def _run() -> bytes:
        import cdsapi

        client = cdsapi.Client(url=CAMS_ADS_URL, key=api_token, quiet=True, debug=False)
        target = _temp_path()
        try:
            client.retrieve(CAMS_DATASET, body, target=target)
            with open(target, "rb") as f:
                return f.read()
        finally:
            _cleanup(target)

    try:
        data = await asyncio.wait_for(asyncio.to_thread(_run), timeout=_TIMEOUT_S)
    except (TimeoutError, Exception) as exc:
        _LOGGER.warning("CAMS UV fetch failed: %s", exc)
        return None

    try:
        return await asyncio.to_thread(_parse_netcdf_zip, data, locations, base_dt)
    except Exception as exc:
        _LOGGER.warning("CAMS UV parse failed: %s", exc)
        return None


async def preflight(api_token: str, session: aiohttp.ClientSession) -> str | None:
    """Light validation of the ADS token (form of a key string). Returns error or None."""
    if not api_token or ":" not in api_token:
        return "invalid_token_format"
    return None


def _temp_path() -> str:
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    return path


def _cleanup(path: str) -> None:
    import os

    try:
        os.unlink(path)
    except OSError:
        pass
