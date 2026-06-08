from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import zipfile
from datetime import timedelta, datetime, timezone
from pathlib import Path

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    CONF_LOCATIONS,
    CONF_NAME,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_SCAN_INTERVAL,
    CONF_DEVICE_TRACKER,
    CONF_DEVICE_TRACKERS,
    CONF_ZONES,
    CONF_TRACKED_LOCATION_NAME,
    CONF_ENABLE_FORECAST,
    CONF_ENABLE_RADOLAN,
    CONF_ENABLE_ICON_EU,
    CONF_ENABLE_UV,
    DEFAULT_SCAN_INTERVAL,
    DWD_OPENDATA,
    DWD_CDC_HOURLY,
    DWD_CDC_10MIN,
    DWD_CDC_DAILY,
    RADAR_BBOX_MERCATOR,
    RADAR_IMG_WIDTH,
    RADAR_IMG_HEIGHT,
    PAST_FRAMES,
    NOWCAST_FRAMES,
    FRAME_INTERVAL_MIN,
    DWD_WMS_RADAR_LAYER,
    DWD_WMS_RADAR_STYLE,
    DWD_WMS_VERSION,
    frames_cache_dir,
    frames_url_prefix,
    safe_frame_filename,
    normalize_entity_list,
    apparent_temperature,
    condition_from_dwd_ww,
    condition_from_temp,
    CDC_10MIN_PRODUCTS,
    CDC_10MIN_FILENAMES,
    CDC_HOURLY_PRODUCTS,
    CDC_DAILY_PRODUCTS,
)
from .station_mapping import fetch_stations, find_nearest_station, DWDStation
from .mosmix import fetch_mosmix_forecast
from .radolan import fetch_radolan_grid, get_radolan_value
from .iconeu import fetch_icon_eu_precip
from .openmeteo import fetch_uv_index

_LOGGER = logging.getLogger(__name__)


class RainradarCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.hass = hass
        self.locations: list[dict] = entry.options.get(CONF_LOCATIONS, [])
        scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        self.enable_forecast = entry.options.get(CONF_ENABLE_FORECAST, True)
        self.enable_radolan = entry.options.get(CONF_ENABLE_RADOLAN, True)
        self.enable_icon_eu = entry.options.get(CONF_ENABLE_ICON_EU, True)
        self.enable_uv = entry.options.get(CONF_ENABLE_UV, True)

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )

        self.stations: list[DWDStation] = []
        self._session = aiohttp.ClientSession()
        self._cache_dir: Path = frames_cache_dir(hass.config.path(""), entry.entry_id)
        self._url_prefix: str = frames_url_prefix(entry.entry_id)
        self._last_frame_error: str | None = None
        self._cache_reprocessed = False
        self._radolan_grid = None
        self._radolan_last_update: float = 0
        self._mosmix_cache: dict[str, list[dict]] = {}
        self._mosmix_last_update: float = 0

        try:
            from PIL import Image
            self._pil_available = True
        except ImportError:
            self._pil_available = False

    @property
    def session(self) -> aiohttp.ClientSession:
        return self._session

    @staticmethod
    def _normalize_entity_list(value: object) -> list[str]:
        from .const import normalize_entity_list
        return normalize_entity_list(value)

    async def _get_zip_text(self, base_url: str, path: str, filename: str) -> str | None:
        url = f"{base_url}/{path}/{filename}"
        try:
            async with asyncio.timeout(15):
                async with self.session.get(url) as resp:
                    if resp.status != 200:
                        return None
                    raw = await resp.read()
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                for name in zf.namelist():
                    if name.startswith("produkt_"):
                        raw_text = zf.read(name)
                        for enc in ("utf-8", "latin-1"):
                            try:
                                return raw_text.decode(enc)
                            except (UnicodeDecodeError, UnicodeEncodeError):
                                continue
                        return raw_text.decode("utf-8", errors="replace")
            return None
        except Exception:
            return None

    async def _fetch_product(
        self, station_id: str, product: str, source: str = "auto"
    ) -> tuple[list[str], list[str], str | None]:
        """Fetch a CDC product. source: auto/10min/hourly."""
        if source == "auto" or source == "10min":
            if product in CDC_10MIN_PRODUCTS:
                path_10, cols_10, keys_10 = CDC_10MIN_PRODUCTS[product]
                filename = CDC_10MIN_FILENAMES[product].format(station_id=station_id)
                text = await self._get_zip_text(DWD_CDC_10MIN, path_10, filename)
                if text is not None:
                    return cols_10, keys_10, text

        if source == "auto" or source == "hourly":
            if product in CDC_HOURLY_PRODUCTS:
                path_h, cols_h, keys_h = CDC_HOURLY_PRODUCTS[product]
                filename = f"stundenwerte_{product}_{station_id}_akt.zip"
                text = await self._get_zip_text(DWD_CDC_HOURLY, path_h, filename)
                if text is not None:
                    return cols_h, keys_h, text

        return [], [], None

    async def _fetch_obs(self, station_id: str) -> dict:
        result: dict = {}

        for product in ("TU", "FF", "RR"):
            cols, keys, text = await self._fetch_product(station_id, product)
            if text is None:
                continue
            try:
                lines = text.strip().split("\n")
                if len(lines) < 2:
                    continue
                header = lines[0]
                data_lines = lines[1:]
                header_cols = [c.strip() for c in header.split(";")]
                idxs = [header_cols.index(c) for c in cols if c in header_cols]
                used_cols = [c for c in cols if c in header_cols]
                used_keys = keys[:len(idxs)]
                last = data_lines[-1].split(";")
                for idx_field, result_key in zip(idxs, used_keys):
                    if idx_field >= len(last):
                        continue
                    val = last[idx_field].strip()
                    if val and val not in ("-999", "999.0", "-999.0"):
                        try:
                            result[result_key] = round(float(val), 1)
                        except (ValueError, TypeError):
                            pass
            except Exception as exc:
                _LOGGER.debug("Parse error for %s: %s", product, exc)

        for product in ("DD", "CO"):
            if product not in CDC_HOURLY_PRODUCTS:
                continue
            path_h, cols_h, keys_h = CDC_HOURLY_PRODUCTS[product]
            filename = f"stundenwerte_{product}_{station_id}_akt.zip"
            text = await self._get_zip_text(DWD_CDC_HOURLY, path_h, filename)
            if text is None:
                continue
            try:
                lines = text.strip().split("\n")
                if len(lines) < 2:
                    continue
                header = lines[0]
                header_cols = [c.strip() for c in header.split(";")]
                for col, key in zip(cols_h, keys_h):
                    if col in header_cols:
                        idx = header_cols.index(col)
                        last = lines[-1].split(";")
                        if idx < len(last):
                            val = last[idx].strip()
                            if val and val not in ("-999", "999.0", "-999.0"):
                                try:
                                    result[key] = round(float(val), 1)
                                except (ValueError, TypeError):
                                    pass
            except Exception as exc:
                _LOGGER.debug("Parse error for %s: %s", product, exc)

        if "wind_speed" in result:
            result["wind_speed"] = round(result["wind_speed"] * 3.6, 1)
        if "precipitation" in result:
            result["precip_intensity"] = result["precipitation"]

        if "temperature" in result:
            t = result["temperature"]
            result["condition"] = condition_from_temp(t)
            if "humidity" in result and "wind_speed" in result:
                result["apparent_temperature"] = apparent_temperature(
                    t, result["humidity"], result["wind_speed"]
                )

        return result

    async def _fetch_daily_data(self, station_id: str) -> dict:
        result: dict = {}
        for product, (path, cols, keys) in CDC_DAILY_PRODUCTS.items():
            filename = f"tageswerte_{product}_{station_id}_akt.zip"
            text = await self._get_zip_text(DWD_CDC_DAILY, path, filename)
            if text is None:
                continue
            try:
                lines = text.strip().split("\n")
                if len(lines) < 2:
                    continue
                header = lines[0]
                header_cols = [c.strip() for c in header.split(";")]
                last = lines[-1].split(";")
                for col, key in zip(cols, keys):
                    if col in header_cols:
                        idx = header_cols.index(col)
                        if idx < len(last):
                            val = last[idx].strip()
                            if val and val not in ("-999", "999.0", "-999.0"):
                                try:
                                    result[key] = round(float(val), 1)
                                except (ValueError, TypeError):
                                    pass
            except Exception as exc:
                _LOGGER.debug("Daily parse error for %s: %s", product, exc)
        return result

    async def _fetch_mosmix(self, station_id: str) -> list[dict] | None:
        now_ts = datetime.now(timezone.utc).timestamp()
        if station_id in self._mosmix_cache and (now_ts - self._mosmix_last_update) < 3600:
            return self._mosmix_cache[station_id]
        forecast = await fetch_mosmix_forecast(self._session, station_id)
        if forecast is not None:
            self._mosmix_cache[station_id] = forecast
            self._mosmix_last_update = now_ts
        return forecast

    async def _fetch_radolan_for_location(self, lat: float, lon: float) -> float | None:
        now_ts = datetime.now(timezone.utc).timestamp()
        if self._radolan_grid is None or (now_ts - self._radolan_last_update) > 600:
            self._radolan_grid = await fetch_radolan_grid(self._session)
            self._radolan_last_update = now_ts
        return get_radolan_value(self._radolan_grid, lat, lon)

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
                    async with self.session.get(url) as resp:
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
        RainradarCoordinator._neutralize_png_inplace(path)

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
            data = list(img.getdata())
            new_data = []
            NEUTRAL_TOL = 4
            for r, g, b, _a in data:
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
                        RainradarCoordinator._neutralize_png_inplace(f)
                        count += 1
        except OSError:
            return
        if count:
            _LOGGER.info("Rainradar: reprocessed %d cached frames", count)

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

    async def _async_update_data(self) -> dict:
        try:
            await self._reprocess_cache_once()

            if not self.stations:
                self.stations = await fetch_stations(self.session)

            zone_entities = self._normalize_entity_list(self.entry.options.get(CONF_ZONES))
            tracker_entities = self._normalize_entity_list(
                self.entry.options.get(CONF_DEVICE_TRACKERS)
            )
            if not tracker_entities:
                tracker_entities = self._normalize_entity_list(
                    self.entry.options.get(CONF_DEVICE_TRACKER)
                )

            location_specs: list[tuple[str, str, str, float, float]] = []

            for loc in self.locations:
                lat = loc.get(CONF_LATITUDE)
                lon = loc.get(CONF_LONGITUDE)
                name = loc.get(CONF_NAME, "unknown")
                if lat is not None and lon is not None:
                    location_specs.append((f"loc::{name}", name, "manual", float(lat), float(lon)))

            for zone_entity in zone_entities:
                zone_state = self.hass.states.get(zone_entity)
                if zone_state is None:
                    continue
                zlat = zone_state.attributes.get(CONF_LATITUDE)
                zlon = zone_state.attributes.get(CONF_LONGITUDE)
                if zlat is None or zlon is None:
                    continue
                zname = zone_state.attributes.get("friendly_name", zone_entity)
                location_specs.append(
                    (f"zone::{zone_entity}", zname, zone_entity, float(zlat), float(zlon))
                )

            for tracker_entity in tracker_entities:
                tracker_state = self.hass.states.get(tracker_entity)
                if tracker_state is not None:
                    tlat = tracker_state.attributes.get("latitude")
                    tlon = tracker_state.attributes.get("longitude")
                    if tlat is not None and tlon is not None:
                        tname = tracker_state.attributes.get("friendly_name", tracker_entity)
                        location_specs.append(
                            (f"tracker::{tracker_entity}", tname, tracker_entity, float(tlat), float(tlon))
                        )

            result_locations: dict[str, dict] = {}
            all_station_ids: set[str] = set()

            for loc_key, loc_name, source_entity, lat, lon in location_specs:
                station = find_nearest_station(lat, lon, self.stations)
                if station is None:
                    _LOGGER.warning("No station found for %s", loc_name)
                    continue
                all_station_ids.add(station.station_id)
                result_locations[loc_key] = {
                    "location_name": loc_name,
                    "source_entity": source_entity,
                    "station_id": station.station_id,
                    "station_name": station.name,
                    "station_distance_km": station.distance_km,
                    "latitude": lat,
                    "longitude": lon,
                }

            obs_tasks = {
                sid: asyncio.create_task(self._fetch_obs(sid))
                for sid in all_station_ids
            }
            daily_tasks = {
                sid: asyncio.create_task(self._fetch_daily_data(sid))
                for sid in all_station_ids
            }
            obs_results: dict[str, dict] = {}
            daily_results: dict[str, dict] = {}
            for sid, task in obs_tasks.items():
                obs_results[sid] = await task
            for sid, task in daily_tasks.items():
                daily_results[sid] = await task

            frame_ts = await self._generate_radar_timestamps()
            frame_urls = await self._prefetch_frames(frame_ts)
            await self._evict_old_frames()

            radolan_tasks: dict[str, asyncio.Task] = {}
            icon_tasks: dict[str, asyncio.Task] = {}
            uv_tasks: dict[str, asyncio.Task] = {}
            mosmix_tasks: dict[str, asyncio.Task] = {}

            if self.enable_radolan:
                for loc_key, _, _, lat, lon in location_specs:
                    radolan_tasks[loc_key] = asyncio.create_task(
                        self._fetch_radolan_for_location(lat, lon)
                    )

            if self.enable_icon_eu:
                for loc_key, _, _, lat, lon in location_specs:
                    icon_tasks[loc_key] = asyncio.create_task(
                        fetch_icon_eu_precip(self._session, lat, lon)
                    )

            if self.enable_uv:
                for loc_key, _, _, lat, lon in location_specs:
                    uv_tasks[loc_key] = asyncio.create_task(
                        fetch_uv_index(self._session, lat, lon)
                    )

            station_ids_by_loc: dict[str, str] = {}
            for loc_key, _, _, lat, lon in location_specs:
                station = find_nearest_station(lat, lon, self.stations)
                if station:
                    station_ids_by_loc[loc_key] = station.station_id

            if self.enable_forecast:
                unique_sids = set(station_ids_by_loc.values())
                for sid in unique_sids:
                    mosmix_tasks[sid] = asyncio.create_task(self._fetch_mosmix(sid))

            radolan_results: dict[str, float | None] = {}
            for loc_key, task in radolan_tasks.items():
                try:
                    radolan_results[loc_key] = await task
                except Exception:
                    radolan_results[loc_key] = None

            icon_results: dict[str, dict | None] = {}
            for loc_key, task in icon_tasks.items():
                try:
                    icon_results[loc_key] = await task
                except Exception:
                    icon_results[loc_key] = None

            uv_results: dict[str, dict | None] = {}
            for loc_key, task in uv_tasks.items():
                try:
                    uv_results[loc_key] = await task
                except Exception:
                    uv_results[loc_key] = None

            mosmix_results: dict[str, list[dict] | None] = {}
            for sid, task in mosmix_tasks.items():
                try:
                    mosmix_results[sid] = await task
                except Exception:
                    mosmix_results[sid] = None

            for loc_key in result_locations:
                sid = station_ids_by_loc.get(loc_key)
                loc = result_locations[loc_key]
                lat = loc["latitude"]
                lon = loc["longitude"]

                if sid and sid in obs_results:
                    loc.update(obs_results[sid])
                if sid and sid in daily_results:
                    for k, v in daily_results[sid].items():
                        if k not in loc or loc[k] is None:
                            loc[k] = v
                if "weather_code" in loc and loc["weather_code"] is not None:
                    loc["condition"] = condition_from_dwd_ww(int(loc["weather_code"]))

                if loc_key in radolan_results and radolan_results[loc_key] is not None:
                    loc["radolan_precipitation"] = radolan_results[loc_key]

                icon_data = icon_results.get(loc_key)
                if icon_data:
                    loc.update(icon_data)

                uv_data = uv_results.get(loc_key)
                if uv_data:
                    loc.update(uv_data)

                if sid and sid in mosmix_results:
                    forecast = mosmix_results[sid]
                    if forecast:
                        now_ts = datetime.now(timezone.utc).timestamp()
                        loc["forecast"] = [
                            fc for fc in forecast
                            if fc.get("ts", 0) >= now_ts
                        ][:48]

            return {
                "locations": result_locations,
                "radar_frames": frame_urls,
                "stations_count": len(self.stations),
                "last_update": datetime.now(timezone.utc).isoformat(),
                "frame_error": self._last_frame_error,
            }

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise UpdateFailed(f"Failed to update DWD data: {exc}") from exc

    async def async_close(self):
        if not self._session.closed:
            await self._session.close()
