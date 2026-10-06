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
import math
from typing import Any

import aiohttp

from . import wnauth, wnzarr
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


def stats_base_url(init_dt: datetime) -> str:
    """HTTPS base URL of one init's ``predictions.zarr`` folder.

    Reads go straight over HTTPS with an ``Authorization: Bearer`` header. No
    ``obstore``/``zarr`` dependency: ``zarr`` pulls in ``numcodecs``, which has
    no musllinux wheels for CPython 3.11+, so it cannot be a hard requirement
    without breaking installs on HA OS. See :mod:`.wnzarr`.
    """
    return (
        f"https://{WEATHERNEXT_STATS_BUCKET}.storage.googleapis.com/"
        f"{_stats_prefix(init_dt)}"
    )


def open_reader(
    session: aiohttp.ClientSession, init_dt: datetime, token: str | None
) -> wnzarr.RemoteZarrV3:
    """Open a Zarr v3 reader for one WN3 init."""
    return wnzarr.RemoteZarrV3(session, stats_base_url(init_dt), token)


async def _resolve(
    reader: wnzarr.RemoteZarrV3, *candidates: str
) -> tuple[str, wnzarr.ArrayMeta] | None:
    """Return the first candidate array that exists in the store.

    WN3 publishes surface variables as ``<name>_mean``; older/alternate layouts
    use the bare ``<name>``. Probe rather than guess so both work.
    """
    for name in candidates:
        try:
            return name, await reader.array_meta(name)
        except wnzarr.MissingArrayError:
            continue
        except (TimeoutError, wnzarr.WNZarrError, aiohttp.ClientError) as exc:
            _LOGGER.debug("WeatherNext probe of %s failed: %s", name, exc)
            return None
    return None


async def _coordinate(
    reader: wnzarr.RemoteZarrV3, *names: str
) -> Any:
    """Read a 1-D coordinate array, trying each candidate name in turn."""
    tried: list[str] = []
    for candidate in names:
        if not candidate or candidate in tried:
            continue
        tried.append(candidate)
        try:
            values = await reader.coordinate(candidate)
        except wnzarr.MissingArrayError:
            continue
        except (TimeoutError, wnzarr.WNZarrError, aiohttp.ClientError) as exc:
            _LOGGER.debug("WeatherNext coordinate %s failed: %s", candidate, exc)
            return None
        if values is not None and values.ndim == 1:
            return values
    return None


async def _lat_lon_coordinates(
    reader: wnzarr.RemoteZarrV3, meta: wnzarr.ArrayMeta
) -> tuple[Any, Any] | None:
    """Fetch the grid's latitude and longitude coordinate arrays.

    Coordinate array names are usually the dimension names, but fall back to
    the common aliases so an unexpected layout still resolves.
    """
    dims = wnzarr.grid_dims(meta)
    if dims is None:
        return None
    lat_i, lon_i, _ = dims

    lat_values = await _coordinate(
        reader, meta.dimension_names[lat_i] or "", "latitude", "lat"
    )
    if lat_values is None:
        _LOGGER.warning(
            "WeatherNext: no latitude coordinate array in the store (tried the "
            "dimension name plus 'latitude'/'lat'); point forecasts need one"
        )
        return None

    lon_values = await _coordinate(
        reader, meta.dimension_names[lon_i] or "", "longitude", "lon"
    )
    if lon_values is None:
        _LOGGER.warning(
            "WeatherNext: no longitude coordinate array in the store (tried the "
            "dimension name plus 'longitude'/'lon'); point forecasts need one"
        )
        return None

    return lat_values, lon_values


async def _nearest_grid(
    reader: wnzarr.RemoteZarrV3, meta: wnzarr.ArrayMeta, lat: float, lon: float
) -> tuple[int, int] | None:
    """Nearest grid indices for a location, honouring WN3's 0-360 longitudes."""
    import numpy as np

    coordinates = await _lat_lon_coordinates(reader, meta)
    if coordinates is None:
        return None
    lat_values, lon_values = coordinates

    # Detect the longitude convention from the data instead of assuming: WN3
    # uses 0..360, but converting unconditionally would be wrong for a
    # -180..180 grid and would silently pick a far-away cell.
    target_lon = float(lon)
    finite = lon_values[np.isfinite(lon_values)]
    if finite.size and finite.min() >= -1e-6:
        target_lon = _lon360(lon)

    return (
        wnzarr.nearest_index(lat_values, lat),
        wnzarr.nearest_index(lon_values, target_lon),
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

    Reads Zarr v3 over HTTPS via :mod:`.wnzarr` — no zarr/obstore dependency.
    """
    if not token:
        return None

    reader = open_reader(session, init_dt, token)

    try:
        # Learn the grid layout and the nearest cell from any variable we know.
        probe = await _resolve(reader, "temperature_2m_mean", "temperature_2m")
        if probe is None:
            _LOGGER.warning(
                "WeatherNext: no known variables found in %s", _stats_prefix(init_dt)
            )
            return None
        grid = await _nearest_grid(reader, probe[1], lat, lon)
        if grid is None:
            return None
        lat_index, lon_index = grid

        result: dict[str, list[dict]] = {"hourly": [], "hourly_stats": {}}

        async def _series(name: str) -> Any:
            return await wnzarr.read_series(
                reader, name, lat_index, lon_index, 0, hours
            )

        # Station head (0.05°) — station-calibrated temperature + dew point.
        for var in WN_STATION_VARS:
            resolved = await _resolve(reader, f"{var}_mean", var)
            if resolved is None:
                continue
            vals = await _series(resolved[0])
            if vals is None:
                continue
            dst = "temperature" if var == "station_head_temperature_2m" else "dew_point"
            for i, v in enumerate(vals):
                if i >= len(result["hourly"]):
                    result["hourly"].append({})
                try:
                    if v != v or v < -900:  # NaN or WN3's missing sentinel
                        continue
                    result["hourly"][i][dst] = round(float(v) - 273.15, 1)
                except (TypeError, ValueError):
                    continue

        # 0.1° surface variables.
        for var in WN_SURFACE_VARS:
            dst_key = _surface_dst(var)
            if dst_key is None:
                continue
            resolved = await _resolve(reader, f"{var}_mean", var)
            if resolved is None:
                continue
            mean_vals = await _series(resolved[0])
            if mean_vals is None:
                continue
            for i, v in enumerate(mean_vals):
                if i >= len(result["hourly"]):
                    result["hourly"].append({})
                try:
                    if v != v or v < -900:
                        continue
                    result["hourly"][i][dst_key] = _surface_convert(var, float(v))
                except (TypeError, ValueError):
                    continue

            for suffix in ("p10", "p90"):
                stat_resolved = await _resolve(reader, f"{var}_{suffix}")
                if stat_resolved is None:
                    continue
                stat_vals = await _series(stat_resolved[0])
                if stat_vals is None:
                    continue
                bucket = result["hourly_stats"].setdefault(suffix, [])
                for i, v in enumerate(stat_vals):
                    if i >= len(bucket):
                        bucket.append({})
                    try:
                        if v != v or v < -900:
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
                entry["wind_direction_100m"] = round(
                    (270.0 - math.degrees(math.atan2(v100, u100))) % 360.0, 0
                )
        return result

    except (TimeoutError, wnzarr.WNZarrError, aiohttp.ClientError) as exc:
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
