from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging
import os
from pathlib import Path
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    ATTR_TEMPERATURE_FORECAST,
    CONF_ENABLE_AIR_QUALITY,
    CONF_ENABLE_FORECAST,
    CONF_ENABLE_ICON_EU,
    CONF_ENABLE_UV,
    CONF_ENABLE_WARNINGS,
    CONF_ENABLE_WEATHERNEXT,
    CONF_ENABLE_WN_OVERLAY,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    DWD_WMS_RADAR_LAYER,
    DWD_WMS_RADAR_STYLE,
    DWD_WMS_VERSION,
    FRAME_INTERVAL_MIN,
    NOWCAST_FRAMES,
    PAST_FRAMES,
    RADAR_BBOX_MERCATOR,
    RADAR_IMG_HEIGHT,
    RADAR_IMG_WIDTH,
    frames_cache_dir,
    frames_url_prefix,
    latlon_to_radar_pixel,
    pixel_intensity,
    resolve_location_specs,
    safe_frame_filename,
)
from .iconeu import fetch_icon_eu_precip
from .mosmix import fetch_mosmix_forecasts, get_mosmix_station_ids
from .openmeteo import fetch_openmeteo_air_quality, fetch_openmeteo_weather
from .station_mapping import DWDStation, find_nearest_stations
from .warnings import (
    fetch_dwd_warnings,
    resolve_warnings_for_coordinates,
    warning_headline_from_warnings,
    warning_level_from_warnings,
)

_LOGGER = logging.getLogger(__name__)


class RadarDataCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Slow coordinator for radar frames and forecast data.

    Fetches radar composite PNGs, MOSMIX-S forecast, ICON-EU,
    and UV index. Runs in background so sensor updates are not blocked.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        stations: list[DWDStation],
    ) -> None:
        self.entry = entry
        scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_radar",
            update_interval=timedelta(seconds=scan_interval),
        )
        self._stations = stations
        self._health_state = True
        self._last_warning_ts: float = 0
        self._cache_dir: Path = frames_cache_dir(hass.config.path(""), entry.entry_id)
        self._url_prefix: str = frames_url_prefix(entry.entry_id)
        self._last_frame_error: str | None = None
        self._cache_reprocessed = False
        self._wn_frames_enabled = entry.options.get(CONF_ENABLE_WN_OVERLAY, False) and (
            entry.options.get(CONF_ENABLE_WEATHERNEXT, False)
        )
        # WN3 credentials for the global precipitation overlay, read lazily so that a
        # sign-in performed after setup is picked up without a restart.
        self._wn_credentials: dict = {}
        try:
            from importlib.util import find_spec

            self._pil_available = find_spec("PIL") is not None
        except (ImportError, ValueError):
            self._pil_available = False

    @property
    def _session(self) -> aiohttp.ClientSession:
        """Return the shared aiohttp client session."""
        return aiohttp_client.async_get_clientsession(self.hass)

    async def _async_wn_credentials(self) -> dict:
        """Return the stored WeatherNext credentials.

        Re-read when absent so a sign-in performed after setup is picked up;
        a reauth reloads the entry, which rebuilds this coordinator anyway.
        """
        if not self._wn_credentials:
            from .credentials import async_get_credentials

            self._wn_credentials = await async_get_credentials(
                self.hass, self.entry.entry_id
            )
        return self._wn_credentials

    @property
    def health_state(self) -> bool:
        return self._health_state

    async def _generate_radar_timestamps(self) -> dict[str, list[str]]:
        now = datetime.now(UTC)
        now_radar = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
        radar: list[str] = []
        for i in range(PAST_FRAMES, 0, -1):
            radar.append(
                (now_radar - timedelta(minutes=FRAME_INTERVAL_MIN * i)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            )
        for i in range(NOWCAST_FRAMES):
            radar.append(
                (now_radar + timedelta(minutes=FRAME_INTERVAL_MIN * i)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            )
        return {"past": radar[:PAST_FRAMES], "nowcast": radar[PAST_FRAMES:]}

    def _wms_url(self, layer: str, style: str, timestamp: str) -> str:
        from urllib.parse import quote
        ts = quote(timestamp, safe="")
        return (
            f"https://maps.dwd.de/geoserver/dwd/ows?service=WMS&version={DWD_WMS_VERSION}"
            f"&request=GetMap&layers={layer}&styles={style}"
            f"&bbox={RADAR_BBOX_MERCATOR}&width={RADAR_IMG_WIDTH}&height={RADAR_IMG_HEIGHT}"
            f"&format=image/png&srs=EPSG:3857&time={ts}&transparent=true"
        )

    def _frame_path(self, layer: str, timestamp: str) -> Path:
        return self._cache_dir / layer / safe_frame_filename(timestamp)

    def _frame_url(self, layer: str, timestamp: str) -> str:
        return f"{self._url_prefix}/{layer}/{safe_frame_filename(timestamp)}"

    async def _download_frame(
        self, layer: str, style: str, timestamp: str, sem: asyncio.Semaphore
    ) -> tuple[str, str, bool, str | None]:
        path = self._frame_path(layer, timestamp)
        if path.is_file():
            return (layer, timestamp, True, None)
        url = self._wms_url(layer, style, timestamp)
        try:
            async with sem:
                async with asyncio.timeout(30):
                    async with self._session.get(url) as resp:
                        if resp.status != 200:
                            return (layer, timestamp, False, f"HTTP {resp.status}")
                        data = await resp.read()
            if len(data) < 8 or data[:8] != b"\x89PNG\r\n\x1a\n":
                snippet = data[:200].decode("utf-8", errors="replace")
                return (layer, timestamp, False, f"non-PNG: {snippet[:120]}")
            await asyncio.to_thread(self._write_frame, path, data)
            return (layer, timestamp, True, None)
        except TimeoutError:
            return (layer, timestamp, False, "timeout after 30s")
        except Exception as exc:
            return (layer, timestamp, False, f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _write_frame(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        RadarDataCoordinator._neutralize_png_inplace(path)

    @staticmethod
    def _neutralize_png_inplace(path: Path) -> None:
        """Flip R==G==B pixels (DWD "no data" grey / "no rain" white) to alpha=0.

        Numpy vectorized: ~50 MB / 0.05 s per 1200x900 frame (the previous
        getdata() loop was ~220 MB / 5 s — a major memory spike with 4
        concurrent frame jobs).
        """
        try:
            from PIL import Image
        except ImportError:
            return
        try:
            img = Image.open(path)
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            NEUTRAL_TOL = 4
            try:
                import numpy as np

                arr = np.array(img)  # writable RGBA copy (~4.3 MB)
                r = arr[..., 0].astype(np.int16)
                g = arr[..., 1].astype(np.int16)
                b = arr[..., 2].astype(np.int16)
                neutral = (
                    (np.abs(r - g) <= NEUTRAL_TOL)
                    & (np.abs(g - b) <= NEUTRAL_TOL)
                    & (np.abs(r - b) <= NEUTRAL_TOL)
                )
                arr[..., 3] = np.where(neutral, 0, 255)
                Image.fromarray(arr, "RGBA").save(path, "PNG", optimize=True)
            except ImportError:
                # numpy unavailable — fall back to the per-pixel loop.
                data_list = list(img.getdata())
                new_data = []
                for r, g, b, _a in data_list:
                    if (
                        abs(r - g) <= NEUTRAL_TOL
                        and abs(g - b) <= NEUTRAL_TOL
                        and abs(r - b) <= NEUTRAL_TOL
                    ):
                        new_data.append((r, g, b, 0))
                    else:
                        new_data.append((r, g, b, 255))
                img.putdata(new_data)
                img.save(path, "PNG", optimize=True)
        except Exception as exc:
            _LOGGER.debug("PIL neutralize failed for %s: %s", path, exc)

    async def _prefetch_frames(self, frames: dict[str, list[str]]) -> dict[str, list[dict]]:
        sem = asyncio.Semaphore(4)
        tasks = []
        for ts in frames.get("past", []):
            tasks.append(
                self._download_frame(DWD_WMS_RADAR_LAYER, DWD_WMS_RADAR_STYLE, ts, sem)
            )
        for ts in frames.get("nowcast", []):
            tasks.append(
                self._download_frame(DWD_WMS_RADAR_LAYER, DWD_WMS_RADAR_STYLE, ts, sem)
            )
        results = await asyncio.gather(*tasks, return_exceptions=True)

        failure_counts: dict[str, int] = {}
        first_error: str | None = None
        for r in results:
            if isinstance(r, BaseException):
                if first_error is None:
                    first_error = f"{type(r).__name__}: {r}"
                failure_counts["exception"] = failure_counts.get("exception", 0) + 1
                continue
            _, _, ok, err = r
            if not ok and err:
                failure_counts[err] = failure_counts.get(err, 0) + 1
                if first_error is None:
                    first_error = err

        result: dict[str, list[dict]] = {}
        for kind in ("past", "nowcast"):
            entries = []
            for ts in frames.get(kind, []):
                path = self._frame_path(DWD_WMS_RADAR_LAYER, ts)
                if path.is_file():
                    entries.append(
                        {"ts": ts, "url": self._frame_url(DWD_WMS_RADAR_LAYER, ts)}
                    )
            result[kind] = entries

        total_requested = sum(len(frames.get(k, [])) for k in ("past", "nowcast"))
        total_ok = sum(len(result.get(k, [])) for k in ("past", "nowcast"))
        if total_requested > 0 and total_ok == 0:
            self._last_frame_error = first_error or "all frame fetches failed"
            _LOGGER.warning(
                "Rainradar: 0/%d frames fetched. First error: %s",
                total_requested, first_error,
            )
        elif total_ok < total_requested:
            self._last_frame_error = (
                f"{total_requested - total_ok}/{total_requested} frames failed; first: {first_error}"
            )
        else:
            self._last_frame_error = None

        return result

    async def _evict_old_frames(self) -> None:
        await asyncio.to_thread(self._evict_old_frames_sync)

    def _evict_old_frames_sync(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(hours=6)
        if not self._cache_dir.exists():
            return
        try:
            layer_dirs = list(self._cache_dir.iterdir())
        except OSError:
            return
        for layer_dir in layer_dirs:
            if not layer_dir.is_dir():
                continue
            try:
                files = list(layer_dir.iterdir())
            except OSError:
                continue
            for f in files:
                if not f.is_file() or not f.name.endswith(".png"):
                    continue
                file_dt = None
                for fmt in ("%Y-%m-%dT%H-%M-%SZ", "%Y-%m-%dT%H-%M-%S", "%Y-%m-%d"):
                    try:
                        file_dt = datetime.strptime(f.stem, fmt).replace(tzinfo=UTC)
                        break
                    except ValueError:
                        continue
                if file_dt is None:
                    continue
                if file_dt < cutoff:
                    try:
                        f.unlink()
                    except OSError:
                        pass

    async def _reprocess_cache_once(self) -> None:
        if self._cache_reprocessed or not self._pil_available:
            self._cache_reprocessed = True
            return
        self._cache_reprocessed = True
        await asyncio.to_thread(self._reprocess_cache_sync)

    def _reprocess_cache_sync(self) -> None:
        if not self._cache_dir.exists():
            return
        count = 0
        try:
            for layer_dir in self._cache_dir.iterdir():
                if not layer_dir.is_dir():
                    continue
                for f in layer_dir.iterdir():
                    if f.is_file() and f.name.endswith(".png"):
                        RadarDataCoordinator._neutralize_png_inplace(f)
                        count += 1
        except OSError:
            return
        if count:
            _LOGGER.info("Rainradar: reprocessed %d cached frames", count)

    async def _generate_wn_frames(self) -> dict[str, list[dict]] | None:
        """Render WN3 global precipitation frames (48h @ 1h steps).

        Reads the statistics bucket directly over HTTPS (see :mod:`.wnzarr`).
        Renders lazily so an overlay failure never blocks DWD frames.
        """
        if not self._wn_frames_enabled:
            return None
        from . import weathernext as wn

        runtime = self.entry.runtime_data
        wn_coord = getattr(runtime, "weathernext_coordinator", None) if runtime else None
        if wn_coord is None or not wn_coord.last_update_success or not wn_coord.data:
            return None

        import numpy as np

        from . import wnframes, wnzarr
        from .wnauth import get_access_token

        init_iso = wn_coord.data.get("init_time")
        if not init_iso:
            return None
        init_dt = datetime.fromisoformat(init_iso)

        token = await get_access_token(await self._async_wn_credentials(), self._session)
        if not token:
            return None

        reader = wn.open_reader(self._session, init_dt, token)
        # Precipitation candidates, best first.
        resolved = None
        for candidate in ("experimental_tp_1hr_mean", "imerg_tp_1hr_mean"):
            resolved = await wn._resolve(reader, candidate)
            if resolved is not None:
                break
        if resolved is None:
            _LOGGER.debug("WN3 overlay: no precipitation variable in the store")
            return None
        var = resolved[0]

        frames: list[dict] = []
        now_ts = datetime.now(UTC).timestamp()
        for step in range(48):
            ts = (init_dt + timedelta(hours=step + 1)).timestamp()
            if ts < now_ts - 3600:
                continue
            stamp = datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
            path = self._cache_dir / "weathernext" / (safe_frame_filename(stamp))
            if not path.is_file():
                try:
                    grid = await wnzarr.read_plane(reader, var, lead_index=step)
                except (TimeoutError, wnzarr.WNZarrError, aiohttp.ClientError) as exc:
                    _LOGGER.debug("WN3 overlay frame %s skipped: %s", stamp, exc)
                    continue
                if grid is None or grid.ndim != 2:
                    continue
                # m → mm/h, then render off the event loop: a global 0.1° grid
                # is ~26 MB and the colour ramp is pure numpy/PIL CPU work.
                vals = np.nan_to_num(grid * 1000.0, nan=-1.0)
                await asyncio.to_thread(
                    wnframes.save_frame_png,
                    path,
                    await asyncio.to_thread(wnframes.grid_to_rgba, vals),
                )
            frames.append(
                {"ts": stamp, "url": f"{self._url_prefix}/weathernext/{path.name}"}
            )
        return {"nowcast": frames, "past": [], "layer": "weathernext"}


    @staticmethod
    def _interpolate_hourly_to_4h(
        entries: list[dict],
        now_ts: float,
    ) -> list[dict]:
        """Linearly interpolate hourly {ts, temperature} entries to 15-min steps for 4h."""
        if not entries:
            return []
        sorted_fc = sorted(
            (f for f in entries if f.get("temperature") is not None),
            key=lambda x: x["ts"],
        )
        end_ts = now_ts + 4 * 3600
        result = []
        step = 15 * 60
        for slot_start in range(int(now_ts), int(end_ts), step):
            slot_ts = float(slot_start)
            left = None
            right = None
            for f in sorted_fc:
                ts = f["ts"]
                temp = f["temperature"]
                if ts <= slot_ts:
                    left = (ts, temp)
                if ts >= slot_ts and right is None:
                    right = (ts, temp)
            if left is not None and right is not None and right[0] != left[0]:
                frac = (slot_ts - left[0]) / (right[0] - left[0])
                t = round(left[1] + (right[1] - left[1]) * frac, 1)
                result.append({"time": slot_ts, "temperature": t})
        return result

    async def _async_update_data(self) -> dict[str, Any]:
        old_data = self.data
        try:
            await self._reprocess_cache_once()

            location_specs = resolve_location_specs(self.hass, self.entry)

            enable_forecast = self.entry.options.get(CONF_ENABLE_FORECAST, True)
            enable_uv = self.entry.options.get(CONF_ENABLE_UV, True)
            enable_icon_eu = self.entry.options.get(CONF_ENABLE_ICON_EU, True)
            enable_warnings = self.entry.options.get(CONF_ENABLE_WARNINGS, True)
            enable_air_quality = self.entry.options.get(CONF_ENABLE_AIR_QUALITY, True)

            # Start frame prefetch + MOSMIX-S in parallel (both are slow)
            frame_ts = await self._generate_radar_timestamps()
            frame_task = asyncio.create_task(self._prefetch_frames(frame_ts))

            async def _try_mosmix():
                if not enable_forecast or not location_specs:
                    return None
                now_ts = datetime.now(UTC).timestamp()

                # Collect the candidate MOSMIX stations for all locations, then
                # fetch them in ONE batched streaming request per hour.
                mosmix_ids = get_mosmix_station_ids()
                wanted_by_location: dict[str, list[str]] = {}
                if mosmix_ids:
                    for loc in location_specs:
                        candidates = [
                            s.station_id
                            for s, _d in find_nearest_stations(
                                loc.latitude, loc.longitude, self._stations, n=20
                            )
                            if s.station_id in mosmix_ids
                        ][:3]
                        if candidates:
                            wanted_by_location[loc.loc_key] = candidates
                elif self._stations:
                    # Cache cold: warm it with the nearest stations of all
                    # locations, then re-filter against the parsed index.
                    warm: set[str] = set()
                    for loc in location_specs:
                        nearest = find_nearest_stations(
                            loc.latitude, loc.longitude, self._stations, n=1
                        )
                        if nearest:
                            warm.add(nearest[0][0].station_id)
                    if warm:
                        await fetch_mosmix_forecasts(self._session, warm)
                    mosmix_ids = get_mosmix_station_ids()
                    if mosmix_ids:
                        for loc in location_specs:
                            candidates = [
                                s.station_id
                                for s, _d in find_nearest_stations(
                                    loc.latitude, loc.longitude, self._stations, n=20
                                )
                                if s.station_id in mosmix_ids
                            ][:3]
                            if candidates:
                                wanted_by_location[loc.loc_key] = candidates

                all_ids: set[str] = set()
                for ids in wanted_by_location.values():
                    all_ids.update(ids)
                if not all_ids:
                    return None
                forecasts = await fetch_mosmix_forecasts(self._session, all_ids)

                mosmix_result: dict[str, list[dict]] = {}
                for loc_key, ids in wanted_by_location.items():
                    for sid in ids:
                        forecast = forecasts.get(sid)
                        if forecast:
                            mosmix_result[loc_key] = [
                                fc for fc in forecast if fc.get("ts", 0) >= now_ts
                            ]
                            break
                return mosmix_result or None

            async def _try_om_temp_forecast():
                if not enable_forecast or not location_specs:
                    return None
                result: dict[str, list[dict]] = {}
                now_ts = datetime.now(UTC).timestamp()
                for loc in location_specs:
                    try:
                        om_data = await fetch_openmeteo_weather(self._session, loc.latitude, loc.longitude)
                        if om_data and "hourly" in om_data:
                            temps = [
                                {"ts": h["ts"], "temperature": h["temperature"]}
                                for h in om_data["hourly"]
                                if h.get("ts") is not None and h.get("temperature") is not None
                            ]
                            if len(temps) >= 2:
                                result[loc.loc_key] = self._interpolate_hourly_to_4h(temps, now_ts)
                                break
                    except Exception:
                        continue
                return result or None

            mosmix_task = asyncio.create_task(_try_mosmix())
            om_temp_task = asyncio.create_task(_try_om_temp_forecast())
            wn_frames_task = asyncio.create_task(self._generate_wn_frames())

            await self._evict_old_frames()
            frame_urls = await frame_task
            mosmix_by_location = await mosmix_task
            temp_forecast_4h = await om_temp_task
            wn_frames = await wn_frames_task

            result: dict[str, Any] = {
                "radar_frames": frame_urls,
                "frame_error": self._last_frame_error,
            }
            if wn_frames:
                result["wn_frames"] = wn_frames
            if mosmix_by_location:
                result["mosmix_by_location"] = mosmix_by_location

            # UV, ICON-EU, WARNINGS, AQ — all optional, run in parallel
            async def _try_uv():
                if not enable_uv or not location_specs:
                    return {}
                for loc in location_specs:
                    try:
                        om_data = await fetch_openmeteo_weather(self._session, loc.latitude, loc.longitude)
                        if om_data:
                            r = {}
                            if "uv_index" in om_data:
                                r["uv_index"] = om_data["uv_index"]
                            if "uv_index_max" in om_data:
                                r["uv_index_max"] = om_data["uv_index_max"]
                            return r
                    except Exception:
                        continue
                return {}

            async def _try_icon():
                if not enable_icon_eu or not location_specs:
                    return {}
                for loc in location_specs:
                    try:
                        icon_data = await fetch_icon_eu_precip(self._session, loc.latitude, loc.longitude)
                        if icon_data:
                            return {"icon_eu": icon_data}
                    except Exception:
                        continue
                return {}

            async def _try_warnings():
                if not enable_warnings:
                    return None
                try:
                    return await fetch_dwd_warnings(self._session)
                except Exception:
                    return None

            async def _try_air_quality():
                if not enable_air_quality or not location_specs:
                    return {}
                for loc in location_specs:
                    try:
                        aq_data = await fetch_openmeteo_air_quality(self._session, loc.latitude, loc.longitude)
                        if aq_data:
                            return aq_data
                    except Exception:
                        continue
                return {}

            uv_res, icon_res, warnings_res, aq_res = await asyncio.gather(
                _try_uv(), _try_icon(),
                _try_warnings(), _try_air_quality(),
                return_exceptions=True,
            )

            # Build per-location radar data for sensor fallback
            radar_locations: dict[str, dict] = {}
            for loc in location_specs:
                radar_locations[loc.loc_key] = {}

                # Every location shares the same AQ data (flat key, first location)
                if isinstance(aq_res, dict):
                    for aq_key in ("aqi_european", "aqi_us", "pm2_5", "pm10", "nitrogen_dioxide"):
                        val = aq_res.get(aq_key)
                        if val is not None:
                            radar_locations[loc.loc_key][aq_key] = val

                # Warnings per location by coordinate matching (point-in-polygon)
                if isinstance(warnings_res, list):
                    matching = resolve_warnings_for_coordinates(warnings_res, loc.latitude, loc.longitude)
                    radar_locations[loc.loc_key]["warning_level"] = warning_level_from_warnings(matching)
                    headline = warning_headline_from_warnings(matching)
                    if headline:
                        radar_locations[loc.loc_key]["warning_headline"] = headline
                    radar_locations[loc.loc_key]["warning_count"] = len(matching)

                # Rain slots + rain_2h_total from nowcast radar frames
                if self._pil_available and frame_urls and frame_urls.get("nowcast"):
                    col, row = latlon_to_radar_pixel(loc.latitude, loc.longitude)

                    def _batch_read_pixels(
                        paths: list[tuple[str, Path]], col: int, row: int
                    ) -> list[tuple[str, tuple[int, int, int, int] | None]]:
                        from PIL import Image
                        results: list[tuple[str, tuple[int, int, int, int] | None]] = []
                        for ts_str, path in paths:
                            if path.is_file():
                                try:
                                    px = Image.open(path).convert("RGBA").getpixel((col, row))
                                    results.append((ts_str, px))
                                except Exception:
                                    results.append((ts_str, None))
                            else:
                                results.append((ts_str, None))
                        return results

                    frame_paths = []
                    for frame in frame_urls["nowcast"]:
                        ts_str = frame.get("ts", "")
                        path = self._frame_path(DWD_WMS_RADAR_LAYER, ts_str)
                        frame_paths.append((ts_str, path))

                    batch_results = await asyncio.to_thread(
                        _batch_read_pixels, frame_paths, col, row
                    )

                    slots: list[dict[str, float]] = []
                    slot_start: float | None = None
                    total_mm = 0.0

                    def _frame_ts(t: str) -> float:
                        return datetime.strptime(
                            t.replace("Z", "").replace(":", "-")[:16],
                            "%Y-%m-%dT%H-%M",
                        ).replace(tzinfo=UTC).timestamp()

                    def _close_slot(end: float, slots: list[dict[str, float]] = slots) -> None:
                        nonlocal slot_start
                        if slot_start is not None:
                            slots.append({"start": slot_start, "end": end})
                            slot_start = None

                    for ts_str, px in batch_results:
                        if not ts_str:
                            _close_slot(0)
                            continue
                        if px is None:
                            _close_slot(_frame_ts(ts_str))
                            continue
                        r, g, b, a = px
                        if a > 0:
                            ft = _frame_ts(ts_str)
                            if slot_start is None:
                                slot_start = ft
                            total_mm += pixel_intensity(r, g, b) * (FRAME_INTERVAL_MIN / 60.0)
                        else:
                            _close_slot(_frame_ts(ts_str))
                    if frame_urls["nowcast"]:
                        last_ts = frame_urls["nowcast"][-1].get("ts", "")
                        _close_slot(_frame_ts(last_ts) + FRAME_INTERVAL_MIN * 60 if last_ts else 0)
                    radar_locations[loc.loc_key]["rain_slots"] = slots
                    radar_locations[loc.loc_key]["rain_2h_total"] = round(total_mm, 2)
                else:
                    radar_locations[loc.loc_key]["rain_slots"] = []
                    radar_locations[loc.loc_key]["rain_2h_total"] = 0

                # 4h temperature forecast from Open-Meteo hourly interpolation
                if temp_forecast_4h and loc.loc_key in temp_forecast_4h:
                    radar_locations[loc.loc_key][ATTR_TEMPERATURE_FORECAST] = temp_forecast_4h[loc.loc_key]

            for loc_key in radar_locations:
                radar_locations[loc_key].setdefault("warning_level", 0)
                radar_locations[loc_key].setdefault("warning_count", 0)

            result["locations"] = radar_locations

            # Also keep flat keys for backward-compatible diagnostics
            if isinstance(uv_res, dict):
                result.update(uv_res)
            if isinstance(icon_res, dict):
                result.update(icon_res)

            result["last_update"] = datetime.now(UTC).isoformat()
            return result

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._health_state = False
            now_ts = datetime.now(UTC).timestamp()
            if now_ts - self._last_warning_ts > 900:
                _LOGGER.warning("Radar data update failed: %s", exc)
                self._last_warning_ts = now_ts
            if old_data is not None:
                return old_data
            raise UpdateFailed(f"Radar data update failed: {exc}") from exc
        else:
            self._health_state = True
