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
CONF_ENABLE_FORECAST = "enable_forecast"
CONF_ENABLE_RADOLAN = "enable_radolan"
CONF_ENABLE_ICON_EU = "enable_icon_eu"
CONF_ENABLE_UV = "enable_uv"

DEFAULT_SCAN_INTERVAL = 600
FORECAST_SCAN_INTERVAL = 3600
RADOLAN_SCAN_INTERVAL = 600
ICON_SCAN_INTERVAL = 10800
UV_SCAN_INTERVAL = 3600

PAST_FRAMES = 24
NOWCAST_FRAMES = 24
FRAME_INTERVAL_MIN = 5

DWD_WMS_BASE = "https://maps.dwd.de/geoserver/dwd/ows"
DWD_OPENDATA = "https://opendata.dwd.de"
DWD_CDC_CLIMATE = f"{DWD_OPENDATA}/climate_environment/CDC/observations_germany/climate"
DWD_CDC_HOURLY = f"{DWD_CDC_CLIMATE}/hourly"
DWD_CDC_10MIN = f"{DWD_CDC_CLIMATE}/10_minutes"
DWD_CDC_DAILY = f"{DWD_CDC_CLIMATE}/daily"
DWD_MOSMIX_BASE = f"{DWD_OPENDATA}/weather/local_forecasts/mos/MOSMIX_S/all_stations/kml"
DWD_RADOLAN_BASE = f"{DWD_OPENDATA}/weather/radar/radolan"
DWD_ICON_EU_BASE = f"{DWD_OPENDATA}/weather/nwp/icon-eu/grib"
OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"

DWD_WMS_RADAR_LAYER = "Niederschlagsradar"
DWD_WMS_RADAR_STYLE = "niederschlagsradar"
DWD_WMS_FORMAT = "image/png"
DWD_WMS_VERSION = "1.1.1"

INTEGRATION_VERSION = "0.5.4"

ATTR_TEMPERATURE = "temperature"
ATTR_HUMIDITY = "humidity"
ATTR_WIND_SPEED = "wind_speed"
ATTR_WIND_DIRECTION = "wind_direction"
ATTR_WIND_GUST = "wind_gust"
ATTR_PRESSURE = "pressure"
ATTR_DEW_POINT = "dew_point"
ATTR_CLOUD_COVERAGE = "cloud_cover"
ATTR_PRECIPITATION = "precipitation"
ATTR_PRECIP_INTENSITY = "precip_intensity"
ATTR_PRECIP_PROBABILITY = "precip_probability"
ATTR_RAIN_RATE = "rain_rate"
ATTR_SNOW_RATE = "snow_rate"
ATTR_FRESH_SNOW = "fresh_snow"
ATTR_SNOW_DEPTH = "snow_depth"
ATTR_RAIN_24H = "rain_24h"
ATTR_SNOW_24H = "snow_24h"
ATTR_SOLAR_RADIATION = "solar_radiation"
ATTR_SUNSHINE_DURATION = "sunshine_duration"
ATTR_VISIBILITY = "visibility"
ATTR_WEATHER_CODE = "weather_code"
ATTR_APPARENT_TEMPERATURE = "apparent_temperature"
ATTR_UV_INDEX = "uv_index"
ATTR_UV_INDEX_MAX = "uv_index_max"
ATTR_RADOLAN_PRECIPITATION = "radolan_precipitation"
ATTR_CONDITION = "condition"
ATTR_STATION_NAME = "station_name"
ATTR_STATION_ID = "station_id"
ATTR_STATION_DISTANCE = "station_distance_km"
ATTR_SOURCE_ENTITY = "source_entity"
ATTR_LAST_UPDATE = "last_update"
ATTR_FRAME_ERROR = "frame_error"

SENSOR_TYPES = {
    "temperature": {
        "unit": "°C",
        "icon": "mdi:thermometer",
        "device_class": "temperature",
        "state_class": "measurement",
    },
    "humidity": {
        "unit": "%",
        "icon": "mdi:water-percent",
        "device_class": "humidity",
        "state_class": "measurement",
    },
    "pressure": {
        "unit": "hPa",
        "icon": "mdi:gauge",
        "device_class": "atmospheric_pressure",
        "state_class": "measurement",
    },
    "dew_point": {
        "unit": "°C",
        "icon": "mdi:water-thermometer",
        "device_class": "temperature",
        "state_class": "measurement",
    },
    "cloud_cover": {
        "unit": "%",
        "icon": "mdi:weather-cloudy",
        "device_class": None,
        "state_class": "measurement",
    },
    "wind_speed": {
        "unit": "km/h",
        "icon": "mdi:weather-windy",
        "device_class": "wind_speed",
        "state_class": "measurement",
    },
    "wind_direction": {
        "unit": "°",
        "icon": "mdi:compass",
        "device_class": "wind_direction",
        "state_class": "measurement_angle",
    },
    "wind_gust": {
        "unit": "km/h",
        "icon": "mdi:weather-windy-variant",
        "device_class": "wind_speed",
        "state_class": "measurement",
    },
    "precipitation": {
        "unit": "mm/h",
        "icon": "mdi:weather-rainy",
        "device_class": "precipitation_intensity",
        "state_class": "measurement",
    },
    "precip_intensity": {
        "unit": "mm/h",
        "icon": "mdi:weather-rainy",
        "device_class": "precipitation_intensity",
        "state_class": "measurement",
    },
    "precip_probability": {
        "unit": "%",
        "icon": "mdi:water-percent",
        "device_class": None,
        "state_class": "measurement",
    },
    "rain_rate": {
        "unit": "mm/h",
        "icon": "mdi:weather-rainy",
        "device_class": "precipitation_intensity",
        "state_class": "measurement",
    },
    "snow_rate": {
        "unit": "mm/h",
        "icon": "mdi:snowflake",
        "device_class": "precipitation_intensity",
        "state_class": "measurement",
    },
    "fresh_snow": {
        "unit": "cm",
        "icon": "mdi:snowflake-alert",
        "device_class": None,
        "state_class": "measurement",
    },
    "snow_depth": {
        "unit": "cm",
        "icon": "mdi:snowflake",
        "device_class": None,
        "state_class": "measurement",
    },
    "rain_24h": {
        "unit": "mm",
        "icon": "mdi:weather-rainy",
        "device_class": "precipitation",
        "state_class": "total",
    },
    "snow_24h": {
        "unit": "mm",
        "icon": "mdi:snowflake",
        "device_class": "precipitation",
        "state_class": "total",
    },
    "solar_radiation": {
        "unit": "W/m²",
        "icon": "mdi:white-balance-sunny",
        "device_class": "irradiance",
        "state_class": "measurement",
    },
    "sunshine_duration": {
        "unit": "h",
        "icon": "mdi:white-balance-sunny",
        "device_class": None,
        "state_class": "total_increasing",
    },
    "visibility": {
        "unit": "km",
        "icon": "mdi:eye",
        "device_class": "distance",
        "state_class": "measurement",
    },
    "weather_code": {
        "unit": None,
        "icon": "mdi:weather-cloudy",
        "device_class": None,
        "state_class": None,
    },
    "apparent_temperature": {
        "unit": "°C",
        "icon": "mdi:thermometer",
        "device_class": "temperature",
        "state_class": "measurement",
    },
    "uv_index": {
        "unit": None,
        "icon": "mdi:weather-sunny-alert",
        "device_class": None,
        "state_class": "measurement",
    },
    "uv_index_max": {
        "unit": None,
        "icon": "mdi:weather-sunny-alert",
        "device_class": None,
        "state_class": "measurement",
    },
    "radolan_precipitation": {
        "unit": "mm/h",
        "icon": "mdi:weather-rainy",
        "device_class": "precipitation_intensity",
        "state_class": "measurement",
    },
    "condition": {
        "unit": None,
        "icon": "mdi:weather-cloudy",
        "device_class": None,
        "state_class": None,
    },
}

# DWD 10-minute "now" product configs
CDC_10MIN_PRODUCTS = {
    "TU": ("air_temperature/now", ["TT_10", "RF_10"], ["temperature", "humidity"]),
    "FF": ("wind/now", ["FF_10", "DD_10"], ["wind_speed", "wind_direction"]),
    "RR": ("precipitation/now", ["RWS_10"], ["precipitation"]),
    "SO": ("solar/now", ["GS_10"], ["solar_radiation"]),
}

# DWD 10-minute "now" filename patterns
CDC_10MIN_FILENAMES = {
    "TU": "10minutenwerte_TU_{station_id}_now.zip",
    "FF": "10minutenwerte_wind_{station_id}_now.zip",
    "RR": "10minutenwerte_nieder_{station_id}_now.zip",
    "SO": "10minutenwerte_SOLAR_{station_id}_now.zip",
}

# DWD hourly "recent" product configs
CDC_HOURLY_PRODUCTS = {
    "TU": ("air_temperature/recent", ["TT_TU", "RF_TU"], ["temperature", "humidity"]),
    "FF": ("wind/recent", ["F", "D"], ["wind_speed", "wind_direction"]),
    "RR": ("precipitation/recent", ["R1"], ["precipitation"]),
    "DD": ("dew_point/recent", ["TD"], ["dew_point"]),
    "CO": ("cloudiness/recent", ["N"], ["cloud_cover"]),
}

# DWD daily product configs
CDC_DAILY_PRODUCTS = {
    "KL": ("kl/recent", ["PM", "TXK", "TNK", "SDK", "NM", "UPM"], [
        "pressure", "temp_max", "temp_min", "sunshine_duration",
        "cloud_cover", "humidity",
    ]),
    "WW": ("weather_phenomena/recent", ["WW"], ["weather_code"]),
    "EB": ("soil_temperature/recent", ["V_TE002M", "V_TE005M", "V_TE010M"], [
        "soil_temp_2cm", "soil_temp_5cm", "soil_temp_10cm",
    ]),
}

# DWD weather code to HA condition mapping
DWD_WW_TO_HA = {}
for _range, _cond in [
    ((1, 2), "sunny"),
    ((3, 8), "partlycloudy"),
    ((9, 29), "cloudy"),
    ((30, 49), "fog"),
    ((50, 59), "rainy"),
    ((60, 69), "rainy"),
    ((70, 79), "snowy"),
    ((80, 99), "lightning"),
]:
    for _code in range(_range[0], _range[1] + 1):
        DWD_WW_TO_HA[_code] = _cond

DWD_WW_TO_HA["exceptional"] = "exceptional"


def condition_from_dwd_ww(code: int | None) -> str:
    """Map DWD present weather code to HA condition string."""
    if code is None or code < 0:
        return "exceptional"
    return DWD_WW_TO_HA.get(code, "exceptional")


def condition_from_temp(temp: float) -> str:
    """Derive condition from temperature (fallback)."""
    if temp >= 20:
        return "sunny"
    if temp >= 10:
        return "partlycloudy"
    if temp >= 5:
        return "cloudy"
    if temp >= -10:
        return "fog"
    return "exceptional"


def apparent_temperature(temp_c: float, humidity_pct: float, wind_kmh: float) -> float:
    """Calculate apparent (feels-like) temperature using Steadman formula."""
    try:
        if temp_c is None or humidity_pct is None:
            return temp_c
        vapor_pressure = humidity_pct / 100 * 6.105 * math.exp(17.27 * temp_c / (237.7 + temp_c))
        wind = wind_kmh if wind_kmh is not None else 0
        return round(temp_c + 0.33 * vapor_pressure - 0.7 * wind - 4.0, 1)
    except (TypeError, ValueError, OverflowError):
        return temp_c


def frames_cache_dir(hass_config_path: str, entry_id: str) -> Path:
    """Return the per-entry cache directory for prefetched radar frames."""
    return Path(hass_config_path) / ".storage" / "rainradar" / entry_id


def entry_cache_dir(hass_config_path: str, entry_id: str) -> Path:
    """Return the per-entry cache directory for weather data."""
    return Path(hass_config_path) / ".storage" / "rainradar" / entry_id


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


def location_slug(name: str) -> str:
    """Convert a location name to a consistent entity-friendly slug."""
    return name.lower().replace(" ", "_").replace("-", "_").replace(".", "_")


def mercator_bbox(lon_min: float, lat_min: float, lon_max: float, lat_max: float) -> str:
    """Project a lon/lat bbox to Web Mercator (EPSG:3857) meters for WMS 1.1.1."""
    x_min = lon_min * 20037508.342789244 / 180.0
    x_max = lon_max * 20037508.342789244 / 180.0
    y_min = math.log(math.tan(math.pi / 4 + lat_min * math.pi / 360.0)) * 6378137.0
    y_max = math.log(math.tan(math.pi / 4 + lat_max * math.pi / 360.0)) * 6378137.0
    return f"{x_min:.2f},{y_min:.2f},{x_max:.2f},{y_max:.2f}"


RADAR_BBOX_LONLAT = (-2.0, 42.0, 22.0, 60.0)
RADAR_BBOX_MERCATOR = mercator_bbox(*RADAR_BBOX_LONLAT)
RADAR_IMG_WIDTH = 1200
RADAR_IMG_HEIGHT = 900
