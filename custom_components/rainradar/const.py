DOMAIN = "rainradar"

CONF_LOCATIONS = "locations"
CONF_NAME = "name"
CONF_LATITUDE = "latitude"
CONF_LONGITUDE = "longitude"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_DEVICE_TRACKER = "device_tracker"
CONF_TRACKED_LOCATION_NAME = "tracked_location_name"

DEFAULT_SCAN_INTERVAL = 600

DWD_WMS_BASE = "https://maps.dwd.de/geoserver/dwd/ows"
DWD_OPENDATA = "https://opendata.dwd.de"

DWD_WMS_RADAR = "Niederschlagsradar"
DWD_WMS_FORECAST = "Icon-eu_reg00625_fd_sl_TOTPREC01H"

DWD_WMS_FORMAT = "image/png"
DWD_WMS_VERSION = "1.3.0"

ATTR_TEMPERATURE = "temperature"
ATTR_HUMIDITY = "humidity"
ATTR_WIND_SPEED = "wind_speed"
ATTR_WIND_DIRECTION = "wind_direction"
ATTR_PRESSURE = "pressure"
ATTR_PRECIPITATION = "precipitation"
ATTR_CONDITION = "condition"
ATTR_ALERTS = "alerts"
ATTR_SUNSHINE = "sunshine"

SENSOR_TYPES = {
    "temperature": {"unit": "°C", "icon": "mdi:thermometer"},
    "humidity": {"unit": "%", "icon": "mdi:water-percent"},
    "wind_speed": {"unit": "km/h", "icon": "mdi:weather-windy"},
    "wind_direction": {"unit": "°", "icon": "mdi:compass"},
    "pressure": {"unit": "hPa", "icon": "mdi:gauge"},
    "precipitation": {"unit": "mm/h", "icon": "mdi:weather-rainy"},
    "condition": {"unit": None, "icon": "mdi:weather-cloudy"},
    "alerts": {"unit": None, "icon": "mdi:alert"},
    "sunshine": {"unit": "min", "icon": "mdi:weather-sunny"},
}
