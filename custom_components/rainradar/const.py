from __future__ import annotations

from pathlib import Path

from homeassistant.const import __version__ as HA_VERSION  # noqa: F401

DOMAIN = "rainradar"

CONF_LOCATIONS = "locations"
CONF_NAME = "name"
CONF_LATITUDE = "latitude"
CONF_LONGITUDE = "longitude"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_DEVICE_TRACKER = "device_tracker"
CONF_ZONES = "zones"
CONF_DEVICE_TRACKERS = "device_trackers"
CONF_TRACKED_LOCATION_NAME = "tracked_location_name"
CONF_CENTER_ENTITY = "center_entity"

DEFAULT_SCAN_INTERVAL = 600

PAST_FRAMES = 24
NOWCAST_FRAMES = 24
FORECAST_FRAMES = 14
FRAME_INTERVAL_MIN = 5

# Germany geographic extent for the radar composite (WMS 1.1.1 EPSG:4326
# bbox order is lon_min,lat_min,lon_max,lat_max).
RADAR_BBOX = "5.7,47.27,15.0,55.05"
RADAR_IMG_WIDTH = 900
RADAR_IMG_HEIGHT = 900

DWD_WMS_BASE = "https://maps.dwd.de/geoserver/dwd/ows"
DWD_OPENDATA = "https://opendata.dwd.de"

DWD_WMS_RADAR_LAYER = "Niederschlagsradar"
DWD_WMS_RADAR_STYLE = "niederschlagsradar"
DWD_WMS_FORECAST_LAYER = "Icon-eu_reg00625_fd_sl_TOTPREC01H"
DWD_WMS_FORECAST_STYLE = "icon-eu_reg00625_fd_sl_totprec01h_lawa"
DWD_WMS_FORMAT = "image/png"
DWD_WMS_VERSION = "1.1.1"

INTEGRATION_VERSION = "0.3.0"

ATTR_TEMPERATURE = "temperature"
ATTR_HUMIDITY = "humidity"
ATTR_WIND_SPEED = "wind_speed"
ATTR_WIND_DIRECTION = "wind_direction"
ATTR_PRESSURE = "pressure"
ATTR_PRECIPITATION = "precipitation"
ATTR_CONDITION = "condition"

SENSOR_TYPES = {
    "temperature": {"unit": "°C", "icon": "mdi:thermometer"},
    "humidity": {"unit": "%", "icon": "mdi:water-percent"},
    "wind_speed": {"unit": "km/h", "icon": "mdi:weather-windy"},
    "wind_direction": {"unit": "°", "icon": "mdi:compass"},
    "precipitation": {"unit": "mm/h", "icon": "mdi:weather-rainy"},
    "condition": {"unit": None, "icon": "mdi:weather-cloudy"},
}


def frames_cache_dir(hass_config_path: str, entry_id: str) -> Path:
    """Return the per-entry cache directory for prefetched radar frames."""
    return Path(hass_config_path) / ".storage" / "rainradar" / "frames" / entry_id


def frames_url_prefix(entry_id: str) -> str:
    """Return the URL prefix under which prefetched frames are served."""
    return f"/rainradar/frames/{entry_id}"


def safe_frame_filename(timestamp: str) -> str:
    """Convert an ISO timestamp to a filesystem-safe PNG filename."""
    return f"{timestamp.replace(':', '-')}.png"


def normalize_entity_list(value: object) -> list[str]:
    """Coerce config-flow option values into a flat list of entity ids."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []
