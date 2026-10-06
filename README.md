# Rainradar

Rainradar is a [Home Assistant](https://www.home-assistant.io/) integration that brings the **Deutscher Wetterdienst (DWD)** rain radar and station data directly into your dashboard. It combines a Python backend with an embedded LitElement + Leaflet frontend card.

No API keys required — all data comes from free DWD sources. The optional
WeatherNext 3 and CAMS UV sources need their own accounts; see
[WeatherNext 3](#weathernext-3-optional-experimental).

## Features

- **Server-prefetched radar frames** — the integration downloads the full 1200×900 composite PNG for each timestamp server-side, caches it under `<config>/.storage/rainradar/frames/<entry_id>/`, and serves it through a static path. The card plays back by swapping a single `L.imageOverlay` URL, so no parallel tile requests hit the browser and the radar aligns to the OSM base by construction. PNGs are post-processed (R==G==B pixels → alpha=0) so the OSM basemap shows through outside the radar coverage and on "no rain" areas.
- **5-min radar loop** — 2h past + 2h nowcast (RADVOR), 48 frames at 5-min cadence. The ICON-D2 hourly forecast is **not** included — its 1h cadence couldn't replace the 5-min nowcast without losing resolution, and forecast precision degrades past ~2h.
- **Companion legend card** — `rainradar-legend-card` shows the full 15-band DWD Niederschlagsradar color ramp (trace / light / moderate / heavy / extreme / violent) with mm/h ranges. Add it via the Lovelace card picker.
- **Station sensors** — temperature, humidity, wind speed/direction, precipitation per configured location, sourced from the nearest DWD station via haversine matching
- **Device tracker support** — optionally follow one or more tracked devices for dynamic per-location data
- **Smart location matching** — nearest DWD station automatically picked from the merged TU + FF + RR + KL catalogs
- **Multiple locations** — configure any number of zones and/or device_trackers in the options flow
- **Animation controls** — play/pause, timeline scrubber, ½× / 1× / 2× speed, recentre, zoom

## Data sources

| Source | Data |
|--------|------|
| DWD GeoServer WMS (`maps.dwd.de`) | `Niederschlagsradar` composite precipitation PNGs (2h past + 2h nowcast), full-extent 1200×900, EPSG:3857, 5-min cadence |
| DWD CDC OpenData (`opendata.dwd.de`) | Hourly station observations: temperature, humidity, wind, precipitation |
| DWD station catalog | TU + FF + RR + KL station coordinates for nearest-station matching |

The radar composite is Germany-only, but the integration asks the WMS for a bbox that extends well beyond the German border (longitudes -2…22, latitudes 42…60) so storms rolling in from France / the Atlantic don't hit a hard "cut-off" edge. Outside the German composite coverage the WMS returns transparent pixels and the OSM basemap shows through.

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

## WeatherNext 3 (optional, experimental)

WeatherNext 3 is Google's global ensemble forecast model. It is off by default
because it needs a Google account that has been granted data access.

### 1. Request access

Submit the [WeatherNext Data Request form](https://developers.google.com/weathernext/guides/access-forecast)
with the **email address of the Google account you will sign in with** (your
`@gmail.com` or Workspace address). Approval typically takes 5–7 business days.
Access covers Cloud Storage, BigQuery and Earth Engine at once.

### 2. Create a Google OAuth client

Rainradar does not ship an OAuth client — you create your own, so no third party
holds a credential on your behalf.

1. Open the [Google Cloud console](https://console.cloud.google.com/) and create
   or select a project.
2. Make sure the project is selected in the console toolbar.
3. Open the [Auth Branding page](https://console.cloud.google.com/auth/branding).
   If it is unconfigured, click **Get started**:
   - **App Information** → app name (e.g. `Home Assistant`), support email = yours
   - **Audience** → **External**
   - **Contact Information** → your email
   - Accept the Google API Services User Data Policy and click **Continue** and **Create**
4. Still under **Branding**, set:
   - **Application home page**, **privacy policy link** and **terms of service link**
     → `https://home-assistant.io`
   - **Authorized domains** → add `home-assistant.io`
5. Under **Audience**, either:
   - **Publish app** (recommended — no expiry, usable by anyone), or
   - leave it in **Testing** and add your own Google account under **Test users**
     (testing-mode credentials expire every 7 days)
6. Under **Clients** → **Create client**:
   - **Application type**: **Web application**
   - Name it, e.g. `Home Assistant Client`
   - **Authorized redirect URIs** → add
     `https://YOUR-HA-URL/auth/external/callback`
     (e.g. `https://home.example.com/auth/external/callback`) and click **Create**

   > Use the URL you actually open Home Assistant with, including the port if
   > non-standard, and without a trailing slash. The redirect URI must match
   > **exactly** or Google answers `Error 400: redirect_uri_mismatch`.
   >
   > Rainradar sends **your instance's own** callback — the base of that URL is
   > taken from Home Assistant's `external_url`, falling back to `internal_url`.
   > It deliberately does *not* use `https://my.home-assistant.io/redirect/oauth`
   > (which is what most Google integrations use), because that requires your
   > instance to be linked to Nabu Casa to resolve the redirect home.
   >
   > **If `external_url` is not set**, set it in `configuration.yaml`
   > (`external_url: https://home.example.com`) and restart, otherwise the
   > redirect falls back to `internal_url` — fine on your LAN, but Google will
   > send the browser there, so it must be reachable from wherever you sign in.
   >
   > You can register more than one redirect URI. Adding
   > `https://my.home-assistant.io/redirect/oauth` as well is harmless and lets
   > you try the Nabu Casa route if you prefer.

7. Copy the **Client ID** and **Client secret** — the secret cannot be retrieved
   again after you close the dialog.

> Google may take up to a few minutes (occasionally up to five hours) before a new
> client becomes usable. The consent screen will warn that the app is unverified;
> click **Advanced** → **Go to Home Assistant (unsafe)** to continue. Only do this
> for a client you created yourself.

### 3. Add the credentials to Home Assistant

**Settings → Devices & Services → Application Credentials → Add**
- Domain: **Rainradar**
- Client ID / Client secret: the values from step 2

### 4. Sign in

Open the Rainradar integration's **Configure** dialog, expand
**Advanced / Experimental**, tick **Enable WeatherNext 3** and **Sign in with
Google**, then submit. Home Assistant opens Google's consent screen in a new
window ("Open website"). Approve the read-only Cloud Storage permission; the
window closes itself and the dialog returns.

The "WeatherNext account" field then shows the signed-in address. To switch
accounts or re-authorize, repeat step 4 from the options dialog.

### 5. Optional Python packages

Bulk Zarr reads need `zarr` and `obstore`. They are deliberately **not** manifest
requirements: `numcodecs` (a `zarr` dependency) ships no cp314 musllinux wheel, so
a hard requirement breaks `pip install` on Alpine-based installs. Install them
manually:

```sh
pip install "zarr>=3.0.0" "obstore>=0.6.0"
```

Without them every other Rainradar feature still works; WeatherNext 3 logs one
warning and stays dormant.

## Dashboard card

After adding the integration, add the Rainradar card to any dashboard:

1. **Edit dashboard → Add card → Rainradar**
2. The card shows the DWD radar map with animation controls

### Card configuration

| Field | Description |
|-------|-------------|
| **Center map on entity** | A `zone.*` or `device_tracker.*` entity to recentre the map on |
| **Center marker color** | Hex / rgb color for the centre marker (default `#d32f2f`) |
| **Secondary markers** | List of additional `zone.*` / `device_tracker.*` entities to render as markers on the map (without recentring). Each has its own color. The map's size is set via the dashboard layout, not this card's config. |

The card's size is set via the dashboard layout (Sections grid or Panel) — there is no per-card height option any more, because a hard pixel height interacts badly with cards below in the grid.

### Card controls

| Control | Description |
|---------|-------------|
| **Play / Pause** | Start/stop radar animation |
| **Timeline slider** | Scrub through individual frames |
| **Zoom +/−** | Zoom controls (also available via scroll wheel on the map) |
| **Re-centre** | Jump back to the configured centre entity |

The three map controls (recentre, zoom in, zoom out) share a single control surface in the top-right corner with internal hairline dividers.

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
| `sensor.rainradar_radar_frames` | Exposes `attributes.frames = {past, nowcast}`, each a list of `{ts, url}` objects served from the per-entry static path. State = total frame count (48 when all caches populated). |
| `sensor.rainradar_stations` | State = number of DWD stations loaded into memory. Attributes are empty (the full ~1000-entry catalog was dropped in 0.3.6 because it exceeded the recorder's 16 KiB attribute cap and the card no longer renders station dots). |
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
