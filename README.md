# Rainradar

Rainradar is a [Home Assistant](https://www.home-assistant.io/) integration that brings the **Deutscher Wetterdienst (DWD)** rain radar directly into your dashboard. It combines a full-featured Python backend with an embedded LitElement + Leaflet frontend card.

No API keys required — all data comes from free DWD sources.

## Features

- **Instant-playback radar map** — parallel tile loading for 48–64 frames, animation starts only when all tiles are ready
- **Dual mode** — 5-min interval (2h RADVOR nowcast) and 15-min interval (14h ICON-D2-RUC forecast)
- **Weather sensors** — temperature, humidity, wind speed/direction, pressure, precipitation, condition, alerts, sunshine per configured location
- **DWD weather stations** — ~200 station dots with live weather icons across Germany
- **Smart location matching** — nearest DWD station is automatically matched to each configured location via haversine distance
- **Unlimited locations** — add as many locations as you want in the config flow, with `zone.home` as default
- **MDI weather icons** — `mdi:weather-*` icons along with sensor values

## Data sources

| Source | Endpoint | Data |
|--------|----------|------|
| DWD Geoserver WMS | `maps.dwd.de/geoserver/dwd/wms` | RADOLAN (past), RADVOR (nowcast), ICON-D2 (forecast) precipitation tiles |
| DWD WarnWetter API | `app-prod-ws.warnwetter.de/v30/` | Station weather observations + weather alerts |
| DWD OpenData | `opendata.dwd.de` | Station catalog with coordinates |

All DWD data is Germany-focused — outside Germany no precipitation tiles are available.

## Installation

### HACS (recommended)

1. Ensure [HACS](https://hacs.xyz/) is installed in your Home Assistant
2. Add this repository as a custom repository in HACS:
   - URL: `https://github.com/jrmbchtl/rainradar`
   - Category: Integration
3. Search for "Rainradar" in HACS and install
4. Restart Home Assistant

### Manual

1. Copy the `custom_components/rainradar/` directory to your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for "Rainradar"
3. Add one or more locations (name, latitude, longitude) — `zone.home` is pre-filled as default
4. Optionally adjust the scan interval (default: 600 seconds)

## Dashboard card

After adding the integration, add the Rainradar card to any dashboard:

1. **Edit dashboard → Add card → Rainradar**
2. The card shows the DWD radar map with animation controls

### Card controls

| Control | Description |
|---------|-------------|
| **5-min / 15-min** | Toggle between nowcast (2h, 5-min steps) and forecast (14h, 15-min steps) |
| **Play / Pause** | Start/stop radar animation |
| **Timeline slider** | Scrub through individual frames |
| **Speed buttons** | ½×, 1×, 2× playback speed |
| **Location picker** | Switch between configured locations |
| **Zoom +/−** | Zoom controls (also available via scroll wheel) |
| **Re-center** | Jump back to active location |
| **Station markers** | DWD station dots with real-time weather icons and temperatures |

## Sensors

Each configured location creates the following sensors:

| Sensor | Unit | Icon |
|--------|------|------|
| Temperature | °C | `mdi:thermometer` |
| Humidity | % | `mdi:water-percent` |
| Wind speed | km/h | `mdi:weather-windy` |
| Wind direction | ° | `mdi:compass` |
| Pressure | hPa | `mdi:gauge` |
| Precipitation | mm/h | `mdi:weather-rainy` |
| Condition | — | `mdi:weather-cloudy` |
| Alerts | — | `mdi:alert` |
| Sunshine | min | `mdi:weather-sunny` |

## Development

```sh
# Install dev dependencies
npm install

# Build frontend card
npm run build

# Copy to HA config for local testing
cp -r custom_components/rainradar /path/to/ha/config/custom_components/
```

## License

MIT
