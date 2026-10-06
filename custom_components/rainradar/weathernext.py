"""WeatherNext 3 forecast access via Google Cloud Storage (Zarr).

Reads the precomputed statistics bucket only (``weathernext3_statistics_spatial``,
Requester Pays OFF). The full-ensemble bucket is deliberately not used — it is
Requester Pays and would bill egress to the user's project.

Data notes (verified against Google's docs, Sept 2026):
- Statistics store has a single continuous 1-hour ``lead_time`` axis.
- Interim hourly inits (01-05, 07-11, 13-17, 19-23 UTC) carry a 48h horizon
  with surface variables; 6-hourly inits (00/06/12/18 UTC) carry 360h.
- Dissemination lag: interim init + 7h10m, synoptic init + 7h45m.
- Longitudes use the 0-360 convention.
- Variable naming: ``*_mean``, ``*_p10``, ``*_p90`` per cell on the 0.1°
  grid and ``station_head_*`` on the 0.05° grid.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging
from typing import Any

import aiohttp

from . import wnauth
from .const import (
    WEATHERNEXT_DISCOVERY_BACKOFF_HOURS,
    WEATHERNEXT_INTERIM_LAG_MIN,
    WEATHERNEXT_STATS_BUCKET,
    WEATHERNEXT_STATS_PREFIX,
)

_LOGGER = logging.getLogger(__name__)

# 0.1° surface variables we consume (mean + p10 + p90 each).
WN_SURFACE_VARS = (
    "temperature_2m",
    "dewpoint_temperature_2m",
    "wind_speed_10m",
    "wind_speed_100m",
    "u_component_of_wind_10m",
    "v_component_of_wind_10m",
    "u_component_of_wind_100m",
    "v_component_of_wind_100m",
    "surface_solar_radiation_downwards_1hr",
    "total_sky_direct_solar_radiation_at_surface_1hr",
    "total_cloud_cover",
    "low_cloud_cover",
    "medium_cloud_cover",
    "high_cloud_cover",
    "total_precipitation_1hr",
    "imerg_tp_1hr",
    "experimental_tp_1hr",
    "mean_sea_level_pressure",
)

# 0.05° station-head variables (station-calibrated, closest to ground truth).
WN_STATION_VARS = (
    "station_head_temperature_2m",
    "station_head_dewpoint_temperature_2m",
)

def _stats_prefix(init_dt: datetime) -> str:
    """Object-store prefix of the predictions.zarr folder for an init time."""
    stamp = init_dt.strftime("%Y%m%d_%Hhr")
    return f"{WEATHERNEXT_STATS_PREFIX}/2026_to_present/{stamp}_00_preds/predictions.zarr"


def _stats_object_url(init_dt: datetime, key: str = "zarr.json") -> str:
    """Public HTTPS URL of one object inside the predictions.zarr folder.

    ``key`` defaults to ``zarr.json``, a real object, so the status code is
    unambiguous: 200 exists, 403 no allowlist access, 404 not disseminated yet.
    Probing the bare ``predictions.zarr`` prefix instead is unreliable because
    it is a directory, not an object.
    """
    return (
        f"https://{WEATHERNEXT_STATS_BUCKET}.storage.googleapis.com/"
        f"{_stats_prefix(init_dt)}/{key}"
    )


def open_stats_store(init_dt: datetime, access_token: str):
    """Open an authenticated obstore GCS store for one WN3 init.

    Returns None when the optional zarr/obstore packages are unavailable.

    Do not pass ``skip_signature``: obstore then omits the Authorization header
    and every read falls back to anonymous, which the allowlist-protected bucket
    rejects with a 403.
    """
    if zarr_missing():
        _warn_zarr_missing()
        return None

    import obstore

    return obstore.store.GCSStore(
        WEATHERNEXT_STATS_BUCKET,
        prefix=_stats_prefix(init_dt),
        credential_provider=wnauth.store_credential_provider(access_token),
    )


async def find_latest_init(
    session: aiohttp.ClientSession,
    token: str | None,
    now: datetime | None = None,
) -> datetime | None:
    """Discover the newest disseminated init by probing object URLs.

    Statistics-bucket reads work without a billing header (Requester Pays OFF),
    but the bucket itself is allowlist-protected, so pass the token as a
    Bearer header when we have one.
    """
    if now is None:
        now = datetime.now(UTC)
    cutoff = now - timedelta(minutes=WEATHERNEXT_INTERIM_LAG_MIN)
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    for back in range(WEATHERNEXT_DISCOVERY_BACKOFF_HOURS):
        candidate = cutoff.replace(minute=0, second=0, microsecond=0) - timedelta(
            hours=back
        )
        url = _stats_object_url(candidate)
        try:
            async with asyncio.timeout(20):
                async with session.get(url, headers=headers) as resp:
                    # zarr folder listings return 200 on the directory-style URL
                    # when the object prefix exists and we have access.
                    if resp.status == 200:
                        return candidate
                    if resp.status in (401, 403):
                        _LOGGER.debug(
                            "WeatherNext access denied while probing %s (%s)",
                            candidate.isoformat(),
                            resp.status,
                        )
                        return None
        except (TimeoutError, aiohttp.ClientError) as exc:
            _LOGGER.debug("WeatherNext probe %s failed: %s", candidate.isoformat(), exc)
            continue
    return None


async def preflight(
    session: aiohttp.ClientSession,
    credentials: dict[str, Any],
) -> str | None:
    """Validate credentials + allowlist. Returns an error string, or None if OK."""
    if zarr_missing():
        # Credentials may still be valid; surface the zarr situation so users
        # aren't surprised when forecasts don't appear.
        _warn_zarr_missing()
    if not wnauth.has_credentials(credentials):
        return "sign_in_required"
    token = await wnauth.get_access_token(credentials, session)
    if token is None:
        return "invalid_credentials"
    now = datetime.now(UTC)
    cutoff = now - timedelta(minutes=WEATHERNEXT_INTERIM_LAG_MIN)
    probe = cutoff.replace(minute=0, second=0, microsecond=0)
    headers = {"Authorization": f"Bearer {token}"}
    for back in range(2):
        url = _stats_object_url(probe - timedelta(hours=back))
        try:
            async with asyncio.timeout(20):
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        return None
                    if resp.status in (401, 403):
                        return "allowlist_pending"
                    if resp.status == 404:
                        continue
                    return f"http_{resp.status}"
        except (TimeoutError, aiohttp.ClientError):
            continue
    return "no_data_found"


def _lon360(lon: float) -> float:
    """Convert a -180..180 longitude to the 0..360 convention used by WN3."""
    return lon % 360.0


def _parse_init_from_url(url: str) -> datetime | None:
    """Parse the init datetime back out of a predictions.zarr URL."""
    try:
        stamp = url.split("/2026_to_present/")[1].split("_00_preds")[0]
        return datetime.strptime(stamp, "%Y%m%d_%Hhr").replace(tzinfo=UTC)
    except (IndexError, ValueError):
        return None


async def fetch_point_forecast(
    session: aiohttp.ClientSession,
    token: str | None,
    init_dt: datetime,
    lat: float,
    lon: float,
    hours: int = 48,
) -> dict[str, list[dict]] | None:
    """Extract the hourly point forecast (mean/p10/p90) for one location.

    Returns ``{"hourly": [...], "hourly_stats": {"p10": [...], "p90": [...]}}``
    where hourly entries mirror the internal forecast dict shape (ts, temperature,
    precipitation, wind_speed, cloud_cover, solar GHI/direct, ...). None on error.

    Requires the optional ``zarr`` + ``obstore`` packages (not manifest
    requirements — see ``zarr_missing``). Returns None with a one-time warning
    when they are unavailable.
    """
    if not token:
        return None
    store = open_stats_store(init_dt, token)
    if store is None:
        return None

    def _extract() -> dict[str, list[dict]] | None:
        import xarray as xr
        import zarr as zarr_mod

        zstore = zarr_mod.storage.ObjectStore(store)
        ds = xr.open_zarr(zstore, chunks={})

        sel_lon = _lon360(lon)
        result: dict[str, list[dict]] = {"hourly": [], "hourly_stats": {}}

        # Station head (0.05°) — station-calibrated temperature + dew point.
        for var in WN_STATION_VARS:
            if var not in ds:
                continue
            ds_var = ds[var]
            lat_name = next(d for d in ds_var.dims if d.startswith("lat"))
            lon_name = next(d for d in ds_var.dims if d.startswith("lon"))
            point = ds_var.sel(
                {lat_name: lat, lon_name: sel_lon}, method="nearest"
            )
            vals = point.values[:hours]
            dst = "temperature" if var == "station_head_temperature_2m" else "dew_point"
            for i, v in enumerate(vals):
                if i >= len(result["hourly"]):
                    result["hourly"].append({})
                try:
                    if v is None or v != v or v < -900:
                        continue
                    result["hourly"][i][dst] = round(float(v) - 273.15, 1)
                except (TypeError, ValueError):
                    continue

        # 0.1° surface variables.
        for var in WN_SURFACE_VARS:
            if var not in ds:
                continue
            ds_var = ds[var]
            lat_name = next(d for d in ds_var.dims if d.startswith("lat"))
            lon_name = next(d for d in ds_var.dims if d.startswith("lon"))
            point = ds_var.sel(
                {lat_name: lat, lon_name: sel_lon}, method="nearest"
            )
            mean_vals = point.values[:hours]
            dst_key = _surface_dst(var)
            if dst_key is None:
                continue
            for i, v in enumerate(mean_vals):
                if i >= len(result["hourly"]):
                    result["hourly"].append({})
                try:
                    if v is None or v != v or v < -900:
                        continue
                    result["hourly"][i][dst_key] = _surface_convert(var, float(v))
                except (TypeError, ValueError):
                    continue
            for suffix, stat_key in (("p10", "p10"), ("p90", "p90")):
                stat_name = f"{var}_{suffix}"
                if stat_name not in ds:
                    continue
                stat_point = ds[stat_name].sel(
                    {lat_name: lat, lon_name: sel_lon}, method="nearest"
                )
                stat_vals = stat_point.values[:hours]
                bucket = result["hourly_stats"].setdefault(stat_key, [])
                for i, v in enumerate(stat_vals):
                    if i >= len(bucket):
                        bucket.append({})
                    try:
                        if v is None or v != v or v < -900:
                            continue
                        bucket[i][dst_key] = _surface_convert(var, float(v))
                    except (TypeError, ValueError):
                        continue

        # Attach timestamps from init + lead_time.
        for i, entry in enumerate(result["hourly"]):
            entry["ts"] = (init_dt + timedelta(hours=i + 1)).timestamp()
        for stat_key in result["hourly_stats"]:
            for i, entry in enumerate(result["hourly_stats"][stat_key]):
                entry["ts"] = (init_dt + timedelta(hours=i + 1)).timestamp()

        # Derived fields: precipitation fallbacks + 100 m wind direction.
        for entry in result["hourly"]:
            if "precipitation" not in entry and "total_precipitation" in entry:
                entry["precipitation"] = entry.pop("total_precipitation")
            elif "precipitation" not in entry and "imerg_precipitation" in entry:
                entry["precipitation"] = entry.pop("imerg_precipitation")
            entry.pop("total_precipitation", None)
            entry.pop("imerg_precipitation", None)
            u100 = entry.pop("u100_raw", None)
            v100 = entry.pop("v100_raw", None)
            if u100 is not None and v100 is not None:
                import math

                entry["wind_direction_100m"] = round((270.0 - math.degrees(math.atan2(v100, u100))) % 360.0, 0)
        return result

    try:
        return await asyncio.to_thread(_extract)
    except Exception as exc:
        _LOGGER.warning("WeatherNext point extraction failed: %s", exc)
        return None


def _surface_dst(var: str) -> str | None:
    """Map a WN3 surface variable to the internal forecast dict key."""
    mapping = {
        "temperature_2m": "temperature",
        "dewpoint_temperature_2m": "dew_point",
        "wind_speed_10m": "wind_speed",
        "wind_speed_100m": "wind_speed_100m",
        "u_component_of_wind_10m": None,  # direction comes from 100 m components
        "v_component_of_wind_10m": None,
        "u_component_of_wind_100m": "u100_raw",
        "v_component_of_wind_100m": "v100_raw",
        "surface_solar_radiation_downwards_1hr": "solar_ghi",
        "total_sky_direct_solar_radiation_at_surface_1hr": "solar_direct",
        "total_cloud_cover": "cloud_cover",
        "low_cloud_cover": "cloud_cover_low",
        "medium_cloud_cover": "cloud_cover_mid",
        "high_cloud_cover": "cloud_cover_high",
        "total_precipitation_1hr": "total_precipitation",
        "imerg_tp_1hr": "imerg_precipitation",
        "experimental_tp_1hr": "precipitation",
        "mean_sea_level_pressure": "pressure",
    }
    return mapping.get(var)


def _surface_convert(var: str, value: float) -> float:
    """Convert a WN3 surface value to internal units."""
    if var in ("temperature_2m", "dewpoint_temperature_2m"):
        return round(value - 273.15, 1)
    if var in ("wind_speed_10m", "wind_speed_100m"):
        return round(value * 3.6, 1)
    if var.startswith(("u_component", "v_component")):
        return round(value * 3.6, 1)
    if var in (
        "surface_solar_radiation_downwards_1hr",
        "total_sky_direct_solar_radiation_at_surface_1hr",
    ):
        # J/m² accumulated over 1 h → mean W/m²
        return round(value / 3600.0, 1)
    if var.startswith(("total_cloud_cover", "low_cloud_cover", "medium_cloud_cover", "high_cloud_cover")):
        return round(value * 100.0, 0)
    if var.endswith("_1hr"):
        # precipitation m → mm
        return round(value * 1000.0, 2)
    if var == "mean_sea_level_pressure":
        return round(value / 100.0, 1)
    return round(value, 2)


def zarr_missing() -> bool:
    """True when the zarr/obstore dependency pair is unavailable.

    These are deliberately NOT manifest requirements: ``numcodecs`` (pulled in
    by zarr) ships no cp314 musllinux wheel, so a hard requirement breaks pip
    install entirely on musl-based HA deployments (Alpine containers).
    Power users can install them manually to enable WN3.
    """
    try:
        import obstore  # noqa: F401
        import zarr  # noqa: F401

        return False
    except ImportError:
        return True


_ZARR_WARNED = False


def _warn_zarr_missing() -> None:
    """Log the zarr hint once per HA process, at WARNING level."""
    global _ZARR_WARNED
    if _ZARR_WARNED:
        return
    _ZARR_WARNED = True
    _LOGGER.warning(
        "WeatherNext 3 is enabled but the optional packages 'zarr' and "
        "'obstore' are not installed (they cannot be auto-installed on all "
        "platforms). Install them manually via pip to enable WN3 forecasts; "
        "all other Rainradar features work without them."
    )
