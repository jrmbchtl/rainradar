from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.weather import (
    WeatherEntity,
    WeatherEntityFeature,
    Forecast,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfLength,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    INTEGRATION_VERSION,
    resolve_condition,
    resolve_location_specs,
)
from .weather_coordinator import WeatherDataCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    weather_coordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[WeatherEntity] = []

    location_specs = resolve_location_specs(hass, entry)
    for loc_key, loc_name, _source_entity, _lat, _lon, slug in location_specs:
        entities.append(
            RainradarWeatherEntity(
                weather_coordinator,
                entry,
                loc_key,
                loc_name,
                slug,
            )
        )

    async_add_entities(entities)


class RainradarWeatherEntity(CoordinatorEntity, WeatherEntity):
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_pressure_unit = UnitOfPressure.HPA
    _attr_native_wind_speed_unit = UnitOfSpeed.KILOMETERS_PER_HOUR
    _attr_native_visibility_unit = UnitOfLength.KILOMETERS
    _attr_has_entity_name = True
    _attr_supported_features = (
        WeatherEntityFeature.FORECAST_DAILY
        | WeatherEntityFeature.FORECAST_HOURLY
        | WeatherEntityFeature.FORECAST_TWICE_DAILY
    )

    def __init__(
        self,
        coordinator: WeatherDataCoordinator,
        entry: ConfigEntry,
        loc_key: str,
        loc_name: str,
        slug: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._loc_key = loc_key
        self._loc_name = loc_name
        self._slug = slug
        self._attr_unique_id = f"{DOMAIN}_weather_{slug}"
        self._attr_name = loc_name
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{entry.entry_id}_{slug}")},
            "name": f"Rainradar {loc_name}",
            "manufacturer": "DWD",
            "model": "Weather Station",
            "sw_version": INTEGRATION_VERSION,
        }

    @property
    def available(self) -> bool:
        if not (self.coordinator.last_update_success and self.coordinator.data):
            return False
        return bool(
            self.coordinator.data.get("locations", {}).get(self._loc_key, {}).get("temperature")
        )

    def _loc_data(self) -> dict:
        return self.coordinator.data.get("locations", {}).get(self._loc_key, {})

    @property
    def condition(self):
        loc = self._loc_data()
        cond = loc.get("condition")
        if cond:
            return cond
        code = loc.get("weather_code")
        if code is not None and isinstance(code, (int, float)):
            from .const import condition_from_dwd_ww
            return condition_from_dwd_ww(int(code))
        return None

    @property
    def native_temperature(self):
        return self._loc_data().get("temperature")

    @property
    def native_apparent_temperature(self):
        return self._loc_data().get("apparent_temperature")

    @property
    def humidity(self):
        return self._loc_data().get("humidity")

    @property
    def native_pressure(self):
        return self._loc_data().get("pressure")

    @property
    def native_wind_speed(self):
        return self._loc_data().get("wind_speed")

    @property
    def wind_bearing(self):
        return self._loc_data().get("wind_direction")

    @property
    def native_wind_gust_speed(self):
        return self._loc_data().get("wind_gust")

    @property
    def native_visibility(self):
        return self._loc_data().get("visibility")

    @property
    def native_dew_point(self):
        return self._loc_data().get("dew_point")

    @property
    def native_uv_index(self):
        return self._loc_data().get("uv_index")

    @property
    def cloud_coverage(self):
        return self._loc_data().get("cloud_cover")

    @property
    def native_precipitation_unit(self):
        return "mm/h"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        loc_data = self._loc_data()
        attrs = {
            "station_name": loc_data.get("station_name"),
            "station_distance_km": loc_data.get("station_distance_km"),
            "station_id": loc_data.get("station_id"),
            "source_entity": loc_data.get("source_entity"),
        }
        return {k: v for k, v in attrs.items() if v is not None}

    @property
    def _radar_coord(self):
        return self.hass.data[DOMAIN].get(f"{self._entry.entry_id}_radar")

    def _mosmix_forecast(self):
        radar_coord = self._radar_coord
        if not radar_coord or not radar_coord.last_update_success or not radar_coord.data:
            return None
        station_id = self._loc_data().get("station_id")
        if not station_id:
            return None
        fb = radar_coord.data.get("forecasts_by_station", {})
        return fb.get(station_id)

    def _om_daily_forecast(self):
        return self._loc_data().get("forecast_daily")

    def _fc_to_forecast(self, fc: dict, is_daytime: bool | None = None) -> dict | None:
        ts = fc.get("ts")
        if ts is None:
            return None
        entry: dict[str, Any] = {
            "datetime": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
        }
        if is_daytime is not None:
            entry["is_daytime"] = is_daytime
        if "temperature" in fc:
            entry["native_temperature"] = fc["temperature"]
        if "temp_min" in fc:
            entry["native_templow"] = fc["temp_min"]
        elif "templow" in fc:
            entry["native_templow"] = fc["templow"]
        if "temp_max" in fc:
            entry["native_temperature"] = fc.get("temperature", fc["temp_max"])
        if "precipitation" in fc:
            entry["native_precipitation"] = fc["precipitation"]
        if "precip_probability" in fc:
            entry["precipitation_probability"] = int(fc["precip_probability"])
        if "wind_speed" in fc:
            entry["native_wind_speed"] = fc["wind_speed"]
        if "wind_direction" in fc:
            entry["wind_bearing"] = fc["wind_direction"]
        if "wind_gust" in fc:
            entry["native_wind_gust_speed"] = fc["wind_gust"]
        if "cloud_cover" in fc:
            entry["cloud_coverage"] = fc["cloud_cover"]
        if "humidity" in fc:
            entry["humidity"] = fc["humidity"]
        if "uv_index" in fc:
            entry["uv_index"] = fc["uv_index"]
        if "weather_code" in fc:
            try:
                entry["condition"] = resolve_condition(
                    dwd_ww=int(fc["weather_code"]),
                    mosmix_ww=None,
                    openmeteo_wmo=None,
                    cloud_cover=fc.get("cloud_cover"),
                    precipitation=fc.get("precipitation"),
                    temperature=fc.get("temperature"),
                )
            except (ValueError, TypeError):
                pass
        return entry

    async def async_forecast_daily(self) -> list[Forecast] | None:
        om_daily = self._om_daily_forecast()
        if om_daily:
            result: list[Forecast] = []
            for fc in om_daily:
                entry = self._fc_to_forecast(fc)
                if entry:
                    result.append(entry)
            return result
        mosmix = self._mosmix_forecast()
        if not mosmix:
            return None
        daily: dict[str, dict] = {}
        for fc in mosmix:
            dt = datetime.fromtimestamp(fc["ts"], tz=timezone.utc)
            day_key = dt.strftime("%Y-%m-%d")
            if day_key not in daily:
                daily[day_key] = {"ts": fc["ts"], "temp_min": 99, "temp_max": -99}
            d = daily[day_key]
            for k in ("temperature", "precipitation", "precip_probability",
                      "wind_speed", "wind_direction", "wind_gust",
                      "cloud_cover", "humidity", "uv_index", "weather_code"):
                if k in fc and (k not in d or d.get(k) is None):
                    d[k] = fc[k]
            t = fc.get("temperature")
            if t is not None:
                if t < d["temp_min"]:
                    d["temp_min"] = t
                if t > d["temp_max"]:
                    d["temp_max"] = t
        result = []
        for day_key in sorted(daily):
            d = daily[day_key]
            d["ts"] = datetime.strptime(day_key + "T12:00:00+00:00",
                                         "%Y-%m-%dT%H:%M:%S%z").timestamp()
            entry = self._fc_to_forecast(d)
            if entry:
                result.append(entry)
        return result

    async def async_forecast_hourly(self) -> list[Forecast] | None:
        mosmix = self._mosmix_forecast()
        if not mosmix:
            return None
        result = []
        for fc in mosmix:
            entry = self._fc_to_forecast(fc)
            if entry:
                result.append(entry)
        return result

    async def async_forecast_twice_daily(self) -> list[Forecast] | None:
        mosmix = self._mosmix_forecast()
        if not mosmix:
            return None
        periods: dict[str, dict] = {}
        for fc in mosmix:
            dt = datetime.fromtimestamp(fc["ts"], tz=timezone.utc)
            hour = dt.hour
            day_key = dt.strftime("%Y-%m-%d")
            is_day = 6 <= hour < 18
            period_key = f"{day_key}_{'day' if is_day else 'night'}"
            if period_key not in periods:
                periods[period_key] = {"ts": fc["ts"], "is_daytime": is_day,
                                       "count": 0, "temp_sum": 0}
            p = periods[period_key]
            p["count"] += 1
            t = fc.get("temperature")
            if t is not None:
                p["temp_sum"] += t
                if "temp_min" not in p or t < p["temp_min"]:
                    p["temp_min"] = t
                if "temp_max" not in p or t > p["temp_max"]:
                    p["temp_max"] = t
            for k in ("precipitation", "precip_probability",
                      "wind_speed", "wind_direction", "wind_gust",
                      "cloud_cover", "humidity", "uv_index", "weather_code"):
                if k in fc and (k not in p or p.get(k) is None):
                    p[k] = fc[k]
        result = []
        for period_key in sorted(periods):
            p = periods[period_key]
            if p["count"] > 0:
                p["temperature"] = round(p["temp_sum"] / p["count"], 1)
            entry = self._fc_to_forecast(p, is_daytime=p["is_daytime"])
            if entry:
                result.append(entry)
        return result
