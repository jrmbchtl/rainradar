from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
from homeassistant.core import HomeAssistant, callback
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
        self._radar_refresh_count = 0
        self._mosmix_cache: list[dict] | None = None
        self._attr_unique_id = f"{DOMAIN}_weather_{slug}"
        self._attr_name = loc_name
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{entry.entry_id}_{slug}")},
            "name": f"Rainradar {loc_name}",
            "manufacturer": "DWD",
            "model": "Weather Station",
            "sw_version": INTEGRATION_VERSION,
        }

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        radar_coord = self._radar_coord
        if radar_coord:
            self.async_on_remove(
                radar_coord.async_add_listener(self._radar_update_listener)
            )

    @callback
    def _radar_update_listener(self):
        self._radar_refresh_count += 1
        mosmix = self._mosmix_forecast()
        if mosmix:
            self._mosmix_cache = mosmix
        self.async_write_ha_state()

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
    def extra_state_attributes(self) -> dict[str, Any] | None:
        loc_data = self._loc_data()
        attrs = {
            "station_name": loc_data.get("station_name"),
            "station_distance_km": loc_data.get("station_distance_km"),
            "station_id": loc_data.get("station_id"),
            "source_entity": loc_data.get("source_entity"),
            "radar_refresh_count": self._radar_refresh_count,
        }
        return {k: v for k, v in attrs.items() if v is not None}

    @property
    def _radar_coord(self):
        return self.hass.data[DOMAIN].get(f"{self._entry.entry_id}_radar")

    def _mosmix_forecast(self):
        radar_coord = self._radar_coord
        if not radar_coord or not radar_coord.last_update_success or not radar_coord.data:
            return None
        return radar_coord.data.get("mosmix_by_location", {}).get(self._loc_key)

    def _merged_hourly(self) -> list[dict] | None:
        om = self._om_hourly_forecast()
        mosmix = self._mosmix_cache or self._mosmix_forecast()
        if not mosmix:
            return om
        if not om:
            return mosmix
        by_ts = {fc["ts"]: fc for fc in mosmix}
        result = []
        for fc in om:
            if fc["ts"] in by_ts:
                result.append(by_ts[fc["ts"]])
                del by_ts[fc["ts"]]
            else:
                result.append(fc)
        for ts in sorted(by_ts):
            result.append(by_ts[ts])
        return result

    def _om_daily_forecast(self):
        return self._loc_data().get("forecast_daily")

    def _om_hourly_forecast(self):
        return self._loc_data().get("forecast_hourly")

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

    def _fc_list(self, raw: list[dict], is_daytime: bool | None = None) -> list[Forecast]:
        result = []
        for fc in raw:
            entry = self._fc_to_forecast(fc, is_daytime)
            if entry:
                result.append(entry)
        return result

    async def async_forecast_daily(self) -> list[Forecast] | None:
        om_daily = self._om_daily_forecast()
        if om_daily:
            return self._fc_list(om_daily)
        mosmix = self._mosmix_forecast()
        if not mosmix:
            return []
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
        merged = self._merged_hourly()
        if merged:
            return self._fc_list(merged)
        return []

    async def async_forecast_twice_daily(self) -> list[Forecast] | None:
        merged = self._merged_hourly()
        if merged:
            return self._twice_daily_from_hourly(merged)
        return []

    def _twice_daily_from_hourly(self, hourly: list[dict]) -> list[Forecast]:
        periods: dict[str, dict] = {}
        for fc in hourly:
            dt = datetime.fromtimestamp(fc["ts"], tz=timezone.utc)
            hour = dt.hour
            if hour < 6:
                day_key = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
                is_day = False
            else:
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
