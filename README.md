# Rainradar

Rainradar is a [Home Assistant](https://www.home-assistant.io/) integration that brings the **Deutscher Wetterdienst (DWD)** rain radar and station data directly into your dashboard. It combines a Python backend with an embedded LitElement + Leaflet frontend card.

No API keys required — all data comes from free DWD sources.

## Features

- **Server-prefetched radar frames** — the integration downloads the full 900×900 composite PNG for each timestamp server-side, caches it under `<config>/.storage/rainradar/frames/<entry_id>/`, and serves it through a static path. The card plays back by swapping a single `L.imageOverlay` URL, so no parallel tile requests hit the browser and the radar aligns to the OSM base by construction.
- **Dual mode** — 5-min interval (2h RADVOR nowcast) and 15-min interval (14h ICON-D2 forecast)
- **Station sensors** — temperature, humidity, wind speed/direction, precipitation per configured location, sourced from the nearest DWD station via haversine matching
- **Device tracker support** — optionally follow one or more tracked devices for dynamic per-location data
- **DWD station dots** — ~200 DWD weather stations overlaid on the map with tooltips (no live per-station data is fetched — that would mean ~200 zips × 3 products per update)
- **Smart location matching** — nearest DWD station automatically picked from the merged TU + FF + RR + KL catalogs
- **Multiple locations** — configure any number of zones and/or device_trackers in the options flow
- **Animation controls** — play/pause, timeline scrubber, ½× / 1× / 2× speed, mode toggle, recentre, zoom

## Data sources

| Source | Data |
|--------|------|
| DWD GeoServer WMS (`maps.dwd.de`) | Composite precipitation PNGs (past 2h, 2h nowcast, 14h forecast), full-extent 900×900, EPSG:4326 |
| DWD CDC OpenData (`opendata.dwd.de`) | Hourly station observations: temperature, humidity, wind, precipitation |
| DWD station catalog | TU + FF + RR + KL station coordinates for nearest-station matching |

All DWD data is Germany-focused — outside Germany no precipitation tiles are available.

## Installation

### HACS (recommended)

1. Ensure [HACS](https://hacs.xyz/) is installed in your Home Assistant
2. Add this repository as a custom repository in HACS:
   - URL: `https://github.com/jorim/rainradar`
   - Category: Integration
3. Search for "Rainradar" in HACS and install
4. Restart Home Assistant

### Manual

1. Copy the `custom_components/rainradar/` directory to your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for "Rainradar"
3. Pick one or more **zones** to track (defaults to `zone.home` if it exists)
4. Optionally pick one or more **device trackers** for dynamic per-location data
5. Adjust the scan interval (default: 600 seconds, range 60–3600)

## Dashboard card

After adding the integration, add the Rainradar card to any dashboard:

1. **Edit dashboard → Add card → Rainradar**
2. The card shows the DWD radar map with animation controls

### Card configuration

| Field | Description |
|-------|-------------|
| **Radar mode** | `5-min` (2h nowcast) or `15-min` (14h forecast) |
| **Center map on entity** | A `zone.*` or `device_tracker.*` entity to recentre the map on |
| **Widget height** | 320 / 420 / 560 / 720 px |

### Card controls

| Control | Description |
|---------|-------------|
| **5-min / 15-min** | Toggle between nowcast (2h, 5-min steps) and forecast (14h, 15-min steps) |
| **Play / Pause** | Start/stop radar animation |
| **Timeline slider** | Scrub through individual frames |
| **Speed buttons** | ½×, 1×, 2× playback speed |
| **Zoom +/−** | Zoom controls (also available via scroll wheel on the map) |
| **Re-centre** | Jump back to the configured centre entity |
| **Station dots** | DWD station locations across Germany (tooltip with station name) |

## Sensors

Each configured location creates the following sensors. The `frames` and `stations` sensors are integration-level (not per-location).

### Per-location sensors

| Sensor | Unit | Icon |
|--------|------|------|
| Temperature | °C | `mdi:thermometer` |
| Humidity | % | `mdi:water-percent` |
| Wind speed | km/h | `mdi:weather-windy` |
| Wind direction | ° | `mdi:compass` |
| Precipitation | mm/h | `mdi:weather-rainy` |
| Condition | — | `mdi:weather-cloudy` |

### Integration-level sensors

| Sensor | Purpose |
|--------|---------|
| `sensor.rainradar_radar_frames` | Exposes `attributes.frames = {past, nowcast, forecast}`, each a list of `{ts, url}` objects served from the per-entry static path. |
| `sensor.rainradar_stations` | Exposes `attributes.stations = [{id, name, lat, lon}, ...]` for the DWD station dots. |
| `sensor.rainradar_<location_name>` | One per configured location with the station snapshot and metadata. |

## Weather entity

A `weather.rainradar_<location_name>` entity is exposed per location. Forecast is **not** populated — ICON-D2 forecast parsing is not implemented in this integration. The current condition is derived from the latest precipitation observation.

## Development

```sh
# Install dev dependencies (the prebuild hook does this for you)
npm install

# Build frontend card (outputs custom_components/rainradar/frontend/dist/rainradar-card.js)
npm run build

# Watch mode for iterative development
npm run watch

# Copy to HA config for local testing
cp -r custom_components/rainradar /path/to/ha/config/custom_components/
# restart HA, then hard-reload browser (Lovelace caches card JS aggressively)
```

## License

MIT
