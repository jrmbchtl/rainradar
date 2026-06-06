from __future__ import annotations

import math
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

DWD_WMS_BASE = "https://maps.dwd.de/geoserver/dwd/ows"
DWD_OPENDATA = "https://opendata.dwd.de"

DWD_WMS_RADAR_LAYER = "Niederschlagsradar"
DWD_WMS_RADAR_STYLE = "niederschlagsradar"
DWD_WMS_FORECAST_LAYER = "Icon-eu_reg00625_fd_sl_TOTPREC01H"
DWD_WMS_FORECAST_STYLE = "icon-eu_reg00625_fd_sl_totprec01h_lawa"
DWD_WMS_FORMAT = "image/png"
DWD_WMS_VERSION = "1.1.1"

INTEGRATION_VERSION = "0.3.5"

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
    # NOTE: must NOT end with a slash. aiohttp's StaticResource (used by
    # HA 2026.x's async_register_static_paths) asserts:
    #     prefix in ("", "/") or not prefix.endswith("/")
    # A trailing slash raises AssertionError at registration time and
    # the static handler never gets attached, so every frame request
    # 404s even when the file is on disk.
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


def mercator_bbox(lon_min: float, lat_min: float, lon_max: float, lat_max: float) -> str:
    """Project a lon/lat bbox to Web Mercator (EPSG:3857) meters for WMS 1.1.1.

    The DWD WMS rejects lat/lon degrees when `srs=EPSG:3857`; the bbox must
    be in projected meters. Computing this server-side keeps the radar PNG
    aligned to the EPSG:3857 OSM base layer in the Leaflet card, so the
    imageOverlay places the composite correctly without per-tile requests.
    """
    x_min = lon_min * 20037508.342789244 / 180.0
    x_max = lon_max * 20037508.342789244 / 180.0
    y_min = math.log(math.tan(math.pi / 4 + lat_min * math.pi / 360.0)) * 6378137.0
    y_max = math.log(math.tan(math.pi / 4 + lat_max * math.pi / 360.0)) * 6378137.0
    return f"{x_min:.2f},{y_min:.2f},{x_max:.2f},{y_max:.2f}"


# Geographic extent for the radar composite. The DWD composite is
# Germany-only, so the actual radar data ends at the German border,
# but we ask the WMS for a much larger area so the basemap is
# visible up to the overlay edge instead of hitting a hard "cut-off"
# right outside the German border (storms rolling in from France
# look broken otherwise). Outside Germany the WMS returns
# transparent pixels and the OSM base shows through.
#
# WMS 1.1.1 with `srs=EPSG:4326` expects bbox in lon,lat order; with
# `srs=EPSG:3857` it expects bbox in projected Mercator meters
# (x_min,y_min,x_max,y_max). Aspect ratio is kept ~3:2 to match.
RADAR_BBOX_LONLAT = (-2.0, 42.0, 22.0, 60.0)
RADAR_BBOX_MERCATOR = mercator_bbox(*RADAR_BBOX_LONLAT)
RADAR_IMG_WIDTH = 1200
RADAR_IMG_HEIGHT = 900
