from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta, datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    CONF_SCAN_INTERVAL,
    CONF_ENABLE_FORECAST,
    CONF_ENABLE_RADOLAN,
    CONF_ENABLE_ICON_EU,
    CONF_ENABLE_UV,
    CONF_ENABLE_WARNINGS,
    CONF_ENABLE_AIR_QUALITY,
    DEFAULT_SCAN_INTERVAL,
    DWD_WMS_RADAR_LAYER,
    DWD_WMS_RADAR_STYLE,
    DWD_WMS_VERSION,
    RADAR_BBOX_MERCATOR,
    RADAR_IMG_WIDTH,
    RADAR_IMG_HEIGHT,
    PAST_FRAMES,
    NOWCAST_FRAMES,
    FRAME_INTERVAL_MIN,
    frames_cache_dir,
    frames_url_prefix,
    safe_frame_filename,
    resolve_location_specs,
)
from .station_mapping import find_nearest_station, DWDStation
from .mosmix import fetch_mosmix_forecast
from .radolan import fetch_radolan_grid, get_radolan_value
from .iconeu import fetch_icon_eu_precip
from .openmeteo import fetch_openmeteo_weather, fetch_openmeteo_air_quality
from .warnings import (
    fetch_dwd_warnings,
    resolve_warnings_for_location,
    warning_level_from_warnings,
    warning_headline_from_warnings,
)

_LOGGER = logging.getLogger(__name__)


class RadarDataCoordinator(DataUpdateCoordinator):
    """Slow coordinator for radar frames and forecast data.

    Fetches radar composite PNGs, MOSMIX-S forecast, RADOLAN, ICON-EU,
    and UV index. Runs in background so sensor updates are not blocked.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: aiohttp.ClientSession,
        stations: list[DWDStation],
    ) -> None:
        self.entry = entry
        scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_radar",
            update_interval=timedelta(seconds=scan_interval),
        )
        self._session = session
        self._stations = stations
        self._cache_dir: Path = frames_cache_dir(hass.config.path(""), entry.entry_id)
        self._url_prefix: str = frames_url_prefix(entry.entry_id)
        self._last_frame_error: str | None = None
        self._cache_reprocessed = False
        self._radolan_grid = None
        self._radolan_last_update: float = 0
        self._mosmix_cache: dict[str, list[dict]] = {}
        self._mosmix_last_update: dict[str, float] = {}

        try:
            from PIL import Image
            self._pil_available = True
        except ImportError:
            self._pil_available = False

    async def _generate_radar_timestamps(self) -> dict[str, list[str]]:
        now = datetime.now(timezone.utc)
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
        except asyncio.TimeoutError:
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
        try:
            from PIL import Image
        except ImportError:
            return
        try:
            img = Image.open(path)
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            data_list = list(img.getdata())
            new_data = []
            NEUTRAL_TOL = 4
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
        cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
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
                        file_dt = datetime.strptime(f.stem, fmt).replace(tzinfo=timezone.utc)
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

    async def _fetch_mosmix(self, station_id: str) -> list[dict] | None:
        now_ts = datetime.now(timezone.utc).timestamp()
        last = self._mosmix_last_update.get(station_id, 0)
        if station_id in self._mosmix_cache and (now_ts - last) < 3600:
            return self._mosmix_cache[station_id]
        forecast = await fetch_mosmix_forecast(self._session, station_id)
        if forecast is not None:
            self._mosmix_cache[station_id] = forecast
            self._mosmix_last_update[station_id] = now_ts
        return forecast

    async def _fetch_radolan_for_location(self, lat: float, lon: float) -> float | None:
        now_ts = datetime.now(timezone.utc).timestamp()
        if self._radolan_grid is None or (now_ts - self._radolan_last_update) > 600:
            self._radolan_grid = await fetch_radolan_grid(self._session)
            self._radolan_last_update = now_ts
        return get_radolan_value(self._radolan_grid, lat, lon)

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            await self._reprocess_cache_once()

            location_specs = resolve_location_specs(self.hass, self.entry)

            enable_forecast = self.entry.options.get(CONF_ENABLE_FORECAST, True)
            enable_uv = self.entry.options.get(CONF_ENABLE_UV, True)
            enable_radolan = self.entry.options.get(CONF_ENABLE_RADOLAN, True)
            enable_icon_eu = self.entry.options.get(CONF_ENABLE_ICON_EU, True)
            enable_warnings = self.entry.options.get(CONF_ENABLE_WARNINGS, True)
            enable_air_quality = self.entry.options.get(CONF_ENABLE_AIR_QUALITY, True)

            # Start frame prefetch + MOSMIX-S in parallel (both are slow)
            frame_ts = await self._generate_radar_timestamps()
            frame_task = asyncio.create_task(self._prefetch_frames(frame_ts))

            async def _try_mosmix():
                if not enable_forecast:
                    return None
                unique_sids: set[str] = set()
                for _lk, _nm, _se, lat, lon, _sl in location_specs:
                    station = find_nearest_station(lat, lon, self._stations)
                    if station:
                        unique_sids.add(station.station_id)
                tasks = {sid: asyncio.create_task(self._fetch_mosmix(sid)) for sid in unique_sids}
                result: dict[str, list[dict]] = {}
                now_ts = datetime.now(timezone.utc).timestamp()
                for sid, task in tasks.items():
                    try:
                        forecast = await task
                        if forecast:
                            result[sid] = [fc for fc in forecast if fc.get("ts", 0) >= now_ts][:48]
                    except Exception:
                        continue
                return result if result else None

            mosmix_task = asyncio.create_task(_try_mosmix())

            # Evict old frames while waiting
            await self._evict_old_frames()
            frame_urls = await frame_task
            forecasts_by_station = await mosmix_task

            result: dict[str, Any] = {
                "radar_frames": frame_urls,
                "frame_error": self._last_frame_error,
            }
            if forecasts_by_station:
                result["forecasts_by_station"] = forecasts_by_station

            # UV, RADOLAN, ICON-EU, WARNINGS, AQ — all optional, run in parallel
            async def _try_uv():
                if not enable_uv or not location_specs:
                    return {}
                for _lk, _nm, _se, lat, lon, _sl in location_specs:
                    try:
                        om_data = await fetch_openmeteo_weather(self._session, lat, lon)
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

            async def _try_radolan():
                if not enable_radolan or not location_specs:
                    return {}
                for _lk, _nm, _se, lat, lon, _sl in location_specs:
                    try:
                        val = await self._fetch_radolan_for_location(lat, lon)
                        if val is not None:
                            return {"radolan_precipitation": val}
                    except Exception:
                        continue
                return {}

            async def _try_icon():
                if not enable_icon_eu or not location_specs:
                    return {}
                for _lk, _nm, _se, lat, lon, _sl in location_specs:
                    try:
                        icon_data = await fetch_icon_eu_precip(self._session, lat, lon)
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
                for _lk, _nm, _se, lat, lon, _sl in location_specs:
                    try:
                        aq_data = await fetch_openmeteo_air_quality(self._session, lat, lon)
                        if aq_data:
                            return aq_data
                    except Exception:
                        continue
                return {}

            uv_res, radolan_res, icon_res, warnings_res, aq_res = await asyncio.gather(
                _try_uv(), _try_radolan(), _try_icon(),
                _try_warnings(), _try_air_quality(),
                return_exceptions=True,
            )

            # Build per-location radar data for sensor fallback
            radar_locations: dict[str, dict] = {}
            for loc_key, loc_name, _se, lat, lon, _sl in location_specs:
                radar_locations[loc_key] = {}

                # Every location shares the same radolan_precipitation (flat key)
                if isinstance(radolan_res, dict):
                    rp = radolan_res.get("radolan_precipitation")
                    if rp is not None:
                        radar_locations[loc_key]["radolan_precipitation"] = rp

                # Every location shares the same AQ data (flat key, first location)
                if isinstance(aq_res, dict):
                    for aq_key in ("aqi_european", "aqi_us", "pm2_5", "pm10", "nitrogen_dioxide"):
                        val = aq_res.get(aq_key)
                        if val is not None:
                            radar_locations[loc_key][aq_key] = val

                # Warnings per location by name matching
                if isinstance(warnings_res, dict):
                    matching = resolve_warnings_for_location(warnings_res, loc_name)
                    radar_locations[loc_key]["warning_level"] = warning_level_from_warnings(matching)
                    headline = warning_headline_from_warnings(matching)
                    if headline:
                        radar_locations[loc_key]["warning_headline"] = headline
                    radar_locations[loc_key]["warning_count"] = len(matching)

            for loc_key in radar_locations:
                radar_locations[loc_key].setdefault("warning_level", 0)
                radar_locations[loc_key].setdefault("warning_count", 0)

            result["locations"] = radar_locations

            # Also keep flat keys for backward-compatible diagnostics
            if isinstance(uv_res, dict):
                result.update(uv_res)
            if isinstance(radolan_res, dict):
                result.update(radolan_res)
            if isinstance(icon_res, dict):
                result.update(icon_res)

            result["last_update"] = datetime.now(timezone.utc).isoformat()
            return result

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise UpdateFailed(f"Radar data update failed: {exc}") from exc
