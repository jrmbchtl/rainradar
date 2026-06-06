from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import zipfile
from datetime import timedelta, datetime, timezone
from pathlib import Path
from urllib.parse import quote

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
    DEFAULT_SCAN_INTERVAL,
    DWD_OPENDATA,
    DWD_WMS_BASE,
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
)
from .station_mapping import fetch_stations, find_nearest_station, DWDStation

_LOGGER = logging.getLogger(__name__)

CDC_BASE = f"{DWD_OPENDATA}/climate_environment/CDC/observations_germany/climate/hourly"
CDC_PRODUCTS = {
    "TU": ("air_temperature/recent", ["TT_TU", "RF_TU"], ["temperature", "humidity"]),
    "FF": ("wind/recent", ["F", "D"], ["wind_speed", "wind_direction"]),
    "RR": ("precipitation/recent", ["R1"], ["precipitation"]),
}

CONDITION_FROM_TEMP = [
    (20, "sunny"),
    (10, "partlycloudy"),
    (5, "cloudy"),
    (-10, "fog"),
    (-99, "fog"),
]


class RainradarCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.hass = hass
        self.locations: list[dict] = entry.options.get(CONF_LOCATIONS, [])
        scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

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
        # Track whether we've already walked the cache dir to neutralise
        # any frames that were downloaded before PIL was wired up. The
        # pass is idempotent but costs a few seconds per frame, so we run
        # it exactly once per coordinator instance.
        self._cache_reprocessed = False
        try:
            from PIL import Image  # noqa: F401

            self._pil_available = True
        except ImportError:
            self._pil_available = False
            _LOGGER.warning(
                "Rainradar: Pillow is not importable; frame PNGs will not "
                "be neutralized. Install Pillow (pip install Pillow) in the "
                "Home Assistant environment for transparent radar backgrounds."
            )

    @property
    def session(self) -> aiohttp.ClientSession:
        return self._session

    @staticmethod
    def _normalize_entity_list(value: object) -> list[str]:
        from .const import normalize_entity_list
        return normalize_entity_list(value)

    async def _get_zip_text(self, product: str, station_id: str) -> str | None:
        path, _, _ = CDC_PRODUCTS[product]
        url = f"{CDC_BASE}/{path}/stundenwerte_{product}_{station_id}_akt.zip"
        try:
            async with asyncio.timeout(15):
                async with self.session.get(url) as resp:
                    if resp.status != 200:
                        _LOGGER.debug("No %s data for station %s", product, station_id)
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
        except Exception as exc:
            _LOGGER.debug("Failed to fetch %s for %s: %s", product, station_id, exc)
            return None

    async def _fetch_obs(self, station_id: str) -> dict:
        result: dict = {}
        tasks = {}
        for prod in CDC_PRODUCTS:
            tasks[prod] = asyncio.create_task(self._get_zip_text(prod, station_id))

        for prod, task in tasks.items():
            text = await task
            if text is None:
                continue
            _, csv_cols, result_keys = CDC_PRODUCTS[prod]
            try:
                lines = text.strip().split("\n")
                if len(lines) < 2:
                    continue
                header = lines[0]
                data_lines = lines[1:]
                header_cols = [c.strip() for c in header.split(";")]
                idxs = [header_cols.index(c) for c in csv_cols]
                last = data_lines[-1].split(";")
                for idx_field, result_key in zip(idxs, result_keys):
                    if idx_field >= len(last):
                        continue
                    val = last[idx_field].strip()
                    if val and val not in ("-999", "999.0", "-999.0"):
                        try:
                            result[result_key] = round(float(val), 1)
                        except (ValueError, TypeError):
                            pass
            except Exception as exc:
                _LOGGER.debug("Parse error for %s: %s", prod, exc)

        if "wind_speed" in result:
            result["wind_speed"] = round(result["wind_speed"] * 3.6, 1)

        if "temperature" in result:
            t = result["temperature"]
            for threshold, cond in CONDITION_FROM_TEMP:
                if t >= threshold:
                    result["condition"] = cond
                    break
        return result

    def _generate_radar_timestamps(self) -> dict[str, list[str]]:
        # DWD WMS only serves frames on 5-minute boundaries. Arbitrary
        # seconds cause the WMS to return HTTP 200 with an
        # InvalidDimensionValue XML body, which silently broke fetching.
        # The ICON-D2 forecast layer (hourly, 14h) was removed in
        # 0.3.8: its 1-hour cadence couldn't replace the 5-min nowcast
        # without losing resolution, and starting it at top-of-current-
        # hour made the timeline "jump back" from ~now+2h to ~now+0h.
        # User feedback was that data past ~2h ahead is not precise
        # enough to be useful anyway, so the card now shows past+nowcast
        # only (48 frames, 2h past + 2h nowcast).
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
        ts = quote(timestamp, safe="")
        # transparent=true makes the WMS itself return alpha=0 for "no
        # data" pixels instead of an opaque white/grey background. The
        # PIL pass still flattens "no rain" pixels (which the WMS keeps
        # opaque) but the bulk of the bbox is now transparent server-side.
        return (
            f"{DWD_WMS_BASE}?service=WMS&version={DWD_WMS_VERSION}"
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
            # DWD returns HTTP 200 with an XML ServiceExceptionReport for
            # out-of-cadence timestamps. A real PNG starts with the 8-byte
            # signature 89 50 4E 47 0D 0A 1A 0A. Reject anything else.
            if len(data) < 8 or data[:8] != b"\x89PNG\r\n\x1a\n":
                snippet = data[:200].decode("utf-8", errors="replace")
                return (layer, timestamp, False, f"non-PNG response: {snippet[:120]}")
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
        # Post-process: the DWD WMS renders "no data" (outside the German
        # composite coverage) as opaque grey and "no rain" inside the
        # coverage as opaque white. A Leaflet imageOverlay would then
        # paint a grey rectangle + white cloud on top of the OSM basemap
        # (the user reports a "white/opaque overlay that is distracting").
        # A neutral pixel (R==G==B) is a good proxy for "no radar data
        # here" — recolor it to transparent so the basemap shows through.
        # Colored pixels (R!=G or G!=B) keep the DWD intensity ramp
        # (cyan→green→yellow→red→magenta→blue). PIL is a HA core dep
        # but we import lazily and skip silently if missing.
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
                "Rainradar: 0/%d frames fetched. First error: %s | failure breakdown: %s",
                total_requested,
                first_error,
                failure_counts,
            )
        elif total_ok < total_requested:
            self._last_frame_error = (
                f"{total_requested - total_ok}/{total_requested} frames failed; first: {first_error}"
            )
            _LOGGER.debug(
                "Rainradar: %d/%d frames fetched successfully. First failure: %s",
                total_ok,
                total_requested,
                first_error,
            )
        else:
            self._last_frame_error = None

        return result

    async def _evict_old_frames(self) -> None:
        await asyncio.to_thread(self._evict_old_frames_sync)

    async def _reprocess_cache_once(self) -> None:
        """Neutralize any pre-PIL frames left over from earlier versions.

        The pass is idempotent (already-transparent pixels stay transparent)
        so it's safe to run again, but each 1200x900 PNG takes ~1-2 s in
        pure-Python PIL — we run it at most once per coordinator instance.
        """
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
        except OSError as exc:
            _LOGGER.debug("Cache reprocess walk failed: %s", exc)
            return
        if count:
            _LOGGER.info(
                "Rainradar: reprocessed %d cached frame(s) to neutralize "
                "no-data pixels (one-shot, after PIL was wired up)",
                count,
            )

    def _evict_old_frames_sync(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
        if not self._cache_dir.exists():
            return
        try:
            layer_dirs = list(self._cache_dir.iterdir())
        except OSError as exc:
            _LOGGER.debug("Frame cache dir not iterable: %s", exc)
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
                # Filenames are produced by safe_frame_filename() and
                # look like 2026-06-06T16-20-00Z.png. Earlier revisions
                # only parsed the YYYY-MM-DD prefix, which made every
                # file dated "today" parse as today-midnight UTC. With a
                # 6h cutoff that meant *every* frame for the current day
                # got unlinked on the next coordinator update, producing
                # 404s on URLs the sensor had just advertised. Parse the
                # full timestamp; fall back to date-only for safety.
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
            # One-shot pass on the first update: any frames left over
            # from a pre-PIL version get neutralised so the OSM basemap
            # shows through (instead of the user seeing an opaque
            # white/grey overlay for the next 6 h until eviction runs).
            await self._reprocess_cache_once()

            if not self.stations:
                self.stations = await fetch_stations(self.session)
                _LOGGER.info("Loaded %d DWD stations", len(self.stations))

            station_map: dict[str, tuple[str, str, DWDStation]] = {}
            zone_entities = self._normalize_entity_list(self.entry.options.get(CONF_ZONES))
            if not zone_entities:
                for loc in self.locations:
                    lat = loc.get(CONF_LATITUDE)
                    lon = loc.get(CONF_LONGITUDE)
                    if lat is not None and lon is not None:
                        nearest = find_nearest_station(lat, lon, self.stations)
                        if nearest is not None:
                            loc_name = loc.get(CONF_NAME, "unknown")
                            station_map[loc_name] = (loc_name, "manual", nearest)

            for zone_entity in zone_entities:
                zone_state = self.hass.states.get(zone_entity)
                if zone_state is None:
                    continue
                zlat = zone_state.attributes.get(CONF_LATITUDE)
                zlon = zone_state.attributes.get(CONF_LONGITUDE)
                if zlat is None or zlon is None:
                    continue
                zone_name = zone_state.attributes.get("friendly_name", zone_entity)
                znearest = find_nearest_station(float(zlat), float(zlon), self.stations)
                if znearest is not None:
                    station_map[f"zone::{zone_entity}"] = (
                        zone_name,
                        zone_entity,
                        znearest,
                    )

            tracker_entities = self._normalize_entity_list(
                self.entry.options.get(CONF_DEVICE_TRACKERS)
            )
            if not tracker_entities:
                tracker_entities = self._normalize_entity_list(
                    self.entry.options.get(CONF_DEVICE_TRACKER)
                )

            for tracker_entity in tracker_entities:
                tracker_state = self.hass.states.get(tracker_entity)
                if tracker_state is not None:
                    tlat = tracker_state.attributes.get("latitude")
                    tlon = tracker_state.attributes.get("longitude")
                    if tlat is not None and tlon is not None:
                        tname = tracker_state.attributes.get("friendly_name", tracker_entity)
                        if not isinstance(tname, str):
                            tname = self.entry.options.get(
                                CONF_TRACKED_LOCATION_NAME,
                                tracker_entity,
                            )
                        tnearest = find_nearest_station(tlat, tlon, self.stations)
                        if tnearest is not None:
                            station_map[f"tracker::{tracker_entity}"] = (
                                tname,
                                tracker_entity,
                                tnearest,
                            )

            station_ids = list({entry[2].station_id for entry in station_map.values()})
            obs_tasks = {sid: asyncio.create_task(self._fetch_obs(sid)) for sid in station_ids}
            obs_results: dict[str, dict] = {}
            for sid, task in obs_tasks.items():
                obs_results[sid] = await task

            frame_ts = self._generate_radar_timestamps()
            frame_urls = await self._prefetch_frames(frame_ts)
            await self._evict_old_frames()

            result: dict[str, dict] = {}
            for loc_key, (loc_name, source_entity, station) in station_map.items():
                loc_data = obs_results.get(station.station_id, {})
                result[loc_key] = {
                    "location_name": loc_name,
                    "source_entity": source_entity,
                    "station_id": station.station_id,
                    "station_name": station.name,
                    "station_distance_km": station.distance_km,
                    **loc_data,
                }

            return {
                "locations": result,
                "radar_frames": frame_urls,
                "stations_count": len(self.stations),
                "last_update": datetime.now(timezone.utc).isoformat(),
                "frame_error": self._last_frame_error,
            }

        except Exception as exc:
            raise UpdateFailed(f"Failed to update DWD data: {exc}") from exc

    async def async_close(self):
        if not self._session.closed:
            await self._session.close()
