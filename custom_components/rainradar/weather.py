from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.weather import (
    WeatherEntity,
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
    location_slug,
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
    def native_temperature_feels_like(self):
        return self._loc_data().get("apparent_temperature")

    @property
    def native_humidity(self):
        return self._loc_data().get("humidity")

    @property
    def native_pressure(self):
        return self._loc_data().get("pressure")

    @property
    def native_wind_speed(self):
        return self._loc_data().get("wind_speed")

    @property
    def native_wind_bearing(self):
        return self._loc_data().get("wind_direction")

    @property
    def native_wind_gust_speed(self):
        return self._loc_data().get("wind_gust")

    @property
    def native_precipitation(self):
        return self._loc_data().get("precipitation")

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
    def native_cloud_cover(self):
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
    def forecast(self) -> Forecast | None:
        radar_coord = self.hass.data[DOMAIN].get(f"{self._entry.entry_id}_radar")
        if not radar_coord or not radar_coord.last_update_success or not radar_coord.data:
            return None
        loc_data = self._loc_data()
        station_id = loc_data.get("station_id")
        forecasts_by_station = radar_coord.data.get("forecasts_by_station", {})
        raw_forecast = None
        if station_id and station_id in forecasts_by_station:
            raw_forecast = forecasts_by_station[station_id]
        if not raw_forecast:
            return None

        result: Forecast = []
        for fc in raw_forecast:
            ts = fc.get("ts")
            if ts is None:
                continue
            entry: dict[str, Any] = {
                "datetime": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            }
            if "temperature" in fc:
                entry["temperature"] = fc["temperature"]
            if "temp_min" in fc:
                entry["templow"] = fc["temp_min"]
            if "temp_max" in fc:
                entry["temperature"] = fc.get("temperature", fc["temp_max"])
            if "precipitation" in fc:
                entry["precipitation"] = fc["precipitation"]
            if "precip_probability" in fc:
                entry["precipitation_probability"] = int(fc["precip_probability"])
            if "wind_speed" in fc:
                entry["wind_speed"] = fc["wind_speed"]
            if "wind_direction" in fc:
                entry["wind_bearing"] = fc["wind_direction"]
            if "wind_gust" in fc:
                entry["wind_gust"] = fc["wind_gust"]
            if "cloud_cover" in fc:
                entry["cloud_coverage"] = fc["cloud_cover"]
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
            result.append(entry)

        return result
