import math
from datetime import datetime, timezone

import aiohttp
import logging

from .const import DWD_OPENDATA

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
)


class DWDStation:
    def __init__(self, station_id: str, name: str, lat: float, lon: float):
        self.station_id = station_id
        self.name = name
        self.lat = lat
        self.lon = lon
        self.distance_km = 0.0


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


def _parse_station_line(line: str) -> DWDStation | None:
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
        rest = parts[6].rsplit(maxsplit=2)
        name = rest[0].strip('" ') if rest else ""
        if not station_id or not name:
            return None
        return DWDStation(station_id, name, lat, lon)
    except (ValueError, IndexError):
        return None


async def fetch_stations(session: aiohttp.ClientSession) -> list[DWDStation]:
    by_id: dict[str, DWDStation] = {}
    for url in STATION_LIST_URLS:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    _LOGGER.debug("Station list %s returned %s", url, resp.status)
                    continue
                raw = await resp.read()
            text = raw.decode("latin-1", errors="replace")
            for line in text.split("\n")[2:]:
                station = _parse_station_line(line)
                if station is None:
                    continue
                by_id.setdefault(station.station_id, station)
        except Exception as exc:
            _LOGGER.warning("Failed to fetch stations from %s: %s", url, exc)
            continue
    stations = list(by_id.values())
    _LOGGER.info("Loaded %d active DWD stations", len(stations))
    return stations


def find_nearest_station(
    lat: float, lon: float, stations: list[DWDStation]
) -> DWDStation | None:
    if not stations:
        return None
    nearest = min(stations, key=lambda s: _haversine(lat, lon, s.lat, s.lon))
    nearest.distance_km = round(_haversine(lat, lon, nearest.lat, nearest.lon), 1)
    return nearest
