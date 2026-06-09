from __future__ import annotations

import math
from datetime import datetime, timezone

import aiohttp
import logging

from .const import DWD_OPENDATA, RADAR_BBOX_LONLAT

_LOGGER = logging.getLogger(__name__)

STATION_LIST_URLS = (
    f"{DWD_OPENDATA}/climate_environment/CDC/observations_germany/climate/hourly/"
    "air_temperature/recent/TU_Stundenwerte_Beschreibung_Stationen.txt",
    f"{DWD_OPENDATA}/climate_environment/CDC/observations_germany/climate/hourly/"
    "wind/recent/FF_Stundenwerte_Beschreibung_Stationen.txt",
    f"{DWD_OPENDATA}/climate_environment/CDC/observations_germany/climate/hourly/"
    "precipitation/recent/RR_Stundenwerte_Beschreibung_Stationen.txt",
    f"{DWD_OPENDATA}/climate_environment/CDC/observations_germany/climate/daily/"
    "kl/recent/KL_Tageswerte_Beschreibung_Stationen.txt",
    f"{DWD_OPENDATA}/climate_environment/CDC/observations_germany/climate/hourly/"
    "dew_point/recent/TD_Stundenwerte_Beschreibung_Stationen.txt",
    f"{DWD_OPENDATA}/climate_environment/CDC/observations_germany/climate/hourly/"
    "cloudiness/recent/N_Stundenwerte_Beschreibung_Stationen.txt",
    f"{DWD_OPENDATA}/climate_environment/CDC/observations_germany/climate/daily/"
    "weather_phenomena/recent/WW_Stundenwerte_Beschreibung_Stationen.txt",
    f"{DWD_OPENDATA}/climate_environment/CDC/observations_germany/climate/hourly/"
    "visibility/recent/VV_Stundenwerte_Beschreibung_Stationen.txt",
)

STATION_PRODUCT_MAP = {
    "TU_Stundenwerte_Beschreibung_Stationen.txt": {"temperature", "humidity", "wind_speed", "wind_direction", "precipitation"},
    "FF_Stundenwerte_Beschreibung_Stationen.txt": {"wind_speed", "wind_direction"},
    "RR_Stundenwerte_Beschreibung_Stationen.txt": {"precipitation"},
    "KL_Tageswerte_Beschreibung_Stationen.txt": {"pressure", "sunshine_duration", "cloud_cover"},
    "TD_Stundenwerte_Beschreibung_Stationen.txt": {"dew_point"},
    "N_Stundenwerte_Beschreibung_Stationen.txt": {"cloud_cover"},
    "WW_Stundenwerte_Beschreibung_Stationen.txt": {"weather_code"},
    "VV_Stundenwerte_Beschreibung_Stationen.txt": {"visibility"},
}

_GERMANY_LAT_MIN = RADAR_BBOX_LONLAT[1] - 0.5
_GERMANY_LAT_MAX = RADAR_BBOX_LONLAT[3] + 0.5
_GERMANY_LON_MIN = RADAR_BBOX_LONLAT[0] - 0.5
_GERMANY_LON_MAX = RADAR_BBOX_LONLAT[2] + 0.5


class DWDStation:
    def __init__(
        self,
        station_id: str,
        name: str,
        lat: float,
        lon: float,
        products: set[str] | None = None,
    ):
        self.station_id = station_id
        self.name = name
        self.lat = lat
        self.lon = lon
        self.distance_km = 0.0
        self.products = products or set()


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon1 - lon2)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _is_active(bis_datum: str) -> bool:
    try:
        end = int(bis_datum)
        if end >= 99990000:
            return True
        end_dt = datetime(
            year=end // 10000,
            month=(end // 100) % 100,
            day=end % 100,
            tzinfo=timezone.utc,
        )
        cutoff = datetime.now(timezone.utc)
        return (cutoff - end_dt).days < 60
    except (ValueError, OverflowError):
        return False


def _parse_station_line(line: str, products: set[str] | None = None) -> DWDStation | None:
    parts = line.split(maxsplit=6)
    if len(parts) < 7:
        return None
    try:
        station_id = parts[0]
        bis_datum = parts[2]
        if not _is_active(bis_datum):
            return None
        lat = float(parts[4].replace(",", "."))
        lon = float(parts[5].replace(",", "."))
        if not (_GERMANY_LAT_MIN <= lat <= _GERMANY_LAT_MAX and
                _GERMANY_LON_MIN <= lon <= _GERMANY_LON_MAX):
            return None
        rest = parts[6].rsplit(maxsplit=2)
        name = rest[0].strip('" ') if rest else ""
        if not station_id or not name:
            return None
        return DWDStation(station_id, name, lat, lon, products)
    except (ValueError, IndexError):
        return None


async def fetch_stations(session: aiohttp.ClientSession) -> list[DWDStation]:
    by_id: dict[str, DWDStation] = {}
    for url in STATION_LIST_URLS:
        filename = url.rsplit("/", 1)[-1]
        products = STATION_PRODUCT_MAP.get(filename, set())
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    _LOGGER.debug("Station list %s returned %s", url, resp.status)
                    continue
                raw = await resp.read()
            text = raw.decode("latin-1", errors="replace")
            for line in text.split("\n")[2:]:
                station = _parse_station_line(line, products)
                if station is None:
                    continue
                if station.station_id in by_id:
                    by_id[station.station_id].products |= products
                else:
                    by_id[station.station_id] = station
        except Exception as exc:
            _LOGGER.warning("Failed to fetch stations from %s: %s", url, exc)
            continue
    stations = list(by_id.values())
    _LOGGER.info("Loaded %d active DWD stations within Germany bbox", len(stations))
    return stations


def find_nearest_station(
    lat: float, lon: float, stations: list[DWDStation]
) -> DWDStation | None:
    if not stations:
        return None
    nearest = min(stations, key=lambda s: _haversine(lat, lon, s.lat, s.lon))
    nearest.distance_km = round(_haversine(lat, lon, nearest.lat, nearest.lon), 1)
    return nearest


def find_nearest_stations(
    lat: float, lon: float, stations: list[DWDStation], n: int = 3
) -> list[tuple[DWDStation, float]]:
    """Find the N nearest stations to a location, sorted by distance (closest first).

    Returns list of (station, distance_km) tuples. Does NOT mutate station.distance_km.
    """
    if not stations:
        return []
    with_dists = [(s, round(_haversine(lat, lon, s.lat, s.lon), 1)) for s in stations]
    with_dists.sort(key=lambda x: x[1])
    return with_dists[:n]


