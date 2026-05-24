import { LitElement, html, css, nothing } from "lit";
import L from "leaflet";

const DWD_WMS = "https://maps.dwd.de/geoserver/dwd/ows";
const RADAR_LAYER = "Niederschlagsradar";
const FORECAST_LAYER = "Icon-eu_reg00625_fd_sl_TOTPREC01H";

const OSM_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const OSM_ATTR = "&copy; <a href='https://openstreetmap.org'>OSM</a>";

const DEFAULT_CENTER = [51.1657, 10.4515];
const DEFAULT_ZOOM = 7;
const FRAME_MS = 150;
const LOAD_TIMEOUT_MS = 15000;
const DATA_RETRY_MS = 3000;

class RainradarCard extends LitElement {
  static properties = {
    hass: { type: Object },
    config: { type: Object },
    _frames: { state: true },
    _currentIndex: { state: true },
    _playing: { state: true },
    _loading: { state: true },
    _loadedFrames: { state: true },
    _totalFrames: { state: true },
    _speed: { state: true },
    _timeLabel: { state: true },
    _showingNoData: { state: true },
  };

  constructor() {
    super();
    this._frames = [];
    this._currentIndex = 0;
    this._playing = false;
    this._loading = true;
    this._loadedFrames = new Set();
    this._totalFrames = 0;
    this._speed = 1;
    this._tileLayers = [];
    this._map = null;
    this._osmLayer = null;
    this._markers = [];
    this._stationMarkers = [];
    this._timer = null;
    this._retryTimer = null;
    this._timeLabel = "";
    this._lastRadarUpdate = null;
    this._centerMarker = null;
    this._showingNoData = false;
  }

  static styles = css`
    :host {
      display: block;
      position: relative;
      width: 100%;
      height: var(--rainradar-card-height, 420px);
      overflow: hidden;
      box-sizing: border-box;
    }

    #map {
      position: relative;
      width: 100%;
      height: 100%;
      min-height: 300px;
      border-radius: var(--ha-card-border-radius, 12px);
      overflow: hidden;
    }

    .controls {
      position: absolute;
      inset: 0;
      pointer-events: none;
      z-index: 1000;
    }
  `;

  static getConfigElement() {
    return document.createElement("rainradar-card-editor");
  }

  static getStubConfig() {
    return {
      mode: "5min",
      center_entity: "zone.home",
      height: 420,
    };
  }

  setConfig(config) {
    if (!config) throw new Error("Invalid configuration");
    this.config = { ...RainradarCard.getStubConfig(), ...config };
  }

  getLayoutOptions() {
    return {
      grid_columns: "auto",
      grid_rows: "auto",
      grid_columns_max: 4,
    };
  }

  _getFrameKey(i) {
    return this._frames[i];
  }

  _getEntityLatLon(entityId) {
    const state = this.hass?.states?.[entityId];
    const attrs = state?.attributes;
    if (!attrs) return null;
    const lat = attrs.latitude ?? attrs.lat;
    const lon = attrs.longitude ?? attrs.lon ?? attrs.lng;
    if (lat == null || lon == null) return null;
    const name = attrs.friendly_name || state?.name || entityId;
    return { lat: Number(lat), lon: Number(lon), name };
  }

  _getConfiguredCenter() {
    const preferred = this.config.center_entity || "zone.home";
    return this._getEntityLatLon(preferred)
      || this._getEntityLatLon("zone.home")
      || null;
  }

  _getStateAttributes(entityIds) {
    for (const entityId of entityIds) {
      const state = this.hass?.states?.[entityId];
      if (state?.attributes) {
        return state.attributes;
      }
    }
    return null;
  }

  _isForecast(frame) {
    const now = new Date();
    const ft = new Date(frame);
    return ft > now;
  }

  _buildFrames() {
    this._playing = false;
    this._clearTimer();
    this._clearRetryTimer();
    this._clearTileLayers();
    this._frames = [];
    this._loadedFrames = new Set();
    this._tileLayers = [];
    this._currentIndex = 0;
    this._loading = true;
    this._totalFrames = 0;

    const radarData = this._getStateAttributes([
      "sensor.rainradar_radar_frames",
      "sensor.rainradar_frames",
      "sensor.radar_frames",
      "rainradar.radar_frames",
    ]);
    this._lastRadarUpdate = radarData?.last_update || null;
    if (!radarData) {
      this._loading = true;
      this._showingNoData = true;
      this._timeLabel = "Waiting for radar data...";
      this._retryTimer = setTimeout(() => {
        if (this._map) {
          this._buildFrames();
        }
      }, DATA_RETRY_MS);
      this.requestUpdate();
      return;
    }
    this._showingNoData = false;

    const past = radarData.past || [];
    const nowcast = radarData.nowcast || [];
    const forecast = radarData.forecast || [];

    if (this.config.mode === "15min") {
      this._frames = [...past, ...nowcast, ...forecast];
    } else {
      this._frames = [...past, ...nowcast];
    }

    if (this._frames.length === 0) {
      this._loading = false;
      this._timeLabel = "No frames";
      this.requestUpdate();
      return;
    }

    this._totalFrames = this._frames.length;

    this._frames.forEach((time, i) => {
      const isFc = this._isForecast(time);
      const layerName = isFc ? FORECAST_LAYER : RADAR_LAYER;
      const styleName = isFc ? "icon-eu_reg00625_fd_sl_totprec01h_lawa" : "niederschlagsradar";

      const layer = L.tileLayer.wms(DWD_WMS, {
        layers: layerName,
        styles: styleName,
        time: time,
        format: "image/png",
        transparent: true,
        version: "1.3.0",
        crs: "EPSG:4326",
        opacity: 0,
        maxZoom: 14,
        minZoom: 4,
      });

      const loadTimeout = setTimeout(() => {
        this._loadedFrames.add(i);
        this._checkLoadComplete();
      }, LOAD_TIMEOUT_MS);

      layer.on("load", () => {
        clearTimeout(loadTimeout);
        this._loadedFrames.add(i);
        this._checkLoadComplete();
      });

      layer.on("tileerror", () => {
        clearTimeout(loadTimeout);
        setTimeout(() => {
          this._loadedFrames.add(i);
          this._checkLoadComplete();
        }, 500);
      });

      this._tileLayers.push(layer);
    });

    this.requestUpdate();
  }

  _clearRetryTimer() {
    if (this._retryTimer) {
      clearTimeout(this._retryTimer);
      this._retryTimer = null;
    }
  }

  _checkLoadComplete() {
    const threshold = Math.ceil(this._totalFrames * 0.85);
    if (this._loadedFrames.size >= threshold && this._loading) {
      this._loading = false;
      this._fadeIn();
      this.requestUpdate();
    }
  }

  _fadeIn() {
    this._tileLayers.forEach((layer) => {
      if (this._map) this._map.addLayer(layer);
    });
    this._tileLayers.forEach((layer, i) => {
      if (i !== 0 && this._map) this._map.removeLayer(layer);
    });
    if (this._frames.length > 0) {
      this._showFrame(0);
    }
  }

  _showFrame(idx) {
    if (!this._map) return;
    const target = this._tileLayers[idx];
    if (!target) return;

    this._tileLayers.forEach((layer, i) => {
      if (i === idx) {
        if (!this._map.hasLayer(layer)) {
          this._map.addLayer(layer);
        }
        layer.setOpacity(0.7);
      } else {
        if (this._map.hasLayer(layer)) {
          this._map.removeLayer(layer);
        }
      }
    });

    this._currentIndex = idx;
    const ts = this._frames[idx];
    if (ts) {
      const d = new Date(ts);
      this._timeLabel = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }
    this.requestUpdate();
  }

  _togglePlay() {
    this._playing = !this._playing;
    if (this._playing) {
      this._tick();
    } else {
      this._clearTimer();
    }
    this.requestUpdate();
  }

  _tick() {
    if (!this._playing) return;
    const next = (this._currentIndex + 1) % this._frames.length;
    this._showFrame(next);
    this._timer = setTimeout(() => this._tick(), FRAME_MS / this._speed);
  }

  _clearTimer() {
    if (this._timer) {
      clearTimeout(this._timer);
      this._timer = null;
    }
  }

  _setSpeed(speed) {
    this._speed = speed;
    if (this._playing) {
      this._clearTimer();
      this._tick();
    }
    this.requestUpdate();
  }

  _onSlider(e) {
    const idx = parseInt(e.target.value);
    this._showFrame(idx);
  }

  _clearTileLayers() {
    this._tileLayers.forEach((layer) => {
      if (this._map) this._map.removeLayer(layer);
      layer.off();
    });
    this._tileLayers = [];
  }

  _recenter() {
    const center = this._getConfiguredCenter();
    const lat = center?.lat ?? DEFAULT_CENTER[0];
    const lon = center?.lon ?? DEFAULT_CENTER[1];
    this._map?.setView([lat, lon], DEFAULT_ZOOM + 1);
  }

  _updateCenterMarker() {
    if (!this._map) return;
    if (this._centerMarker) {
      this._map.removeLayer(this._centerMarker);
      this._centerMarker = null;
    }

    const center = this._getConfiguredCenter();
    if (!center) return;

    const icon = L.divIcon({
      html: `<div style="display:flex;flex-direction:column;align-items:center;gap:2px;color:var(--primary-color,#03a9f4);text-shadow:0 0 4px rgba(255,255,255,0.95);font-weight:700;"><ha-icon icon="mdi:map-marker-radius" style="font-size:24px;"></ha-icon><div style="font-size:11px;background:rgba(255,255,255,0.88);padding:2px 6px;border-radius:999px;white-space:nowrap;">${center.name}</div></div>`,
      className: "",
      iconSize: [64, 64],
      iconAnchor: [32, 56],
    });
    this._centerMarker = L.marker([center.lat, center.lon], { icon }).addTo(this._map);
  }

  _updateStationMarkers() {
    this._stationMarkers.forEach((m) => this._map?.removeLayer(m));
    this._stationMarkers = [];

    const stations = this._getStateAttributes([
      "sensor.rainradar_stations",
      "sensor.rainradar_stations_2",
      "sensor.stations",
      "rainradar.stations",
    ])?.stations;
    if (!stations || !this._map) return;

    const bounds = this._map.getBounds();
    stations.forEach((s) => {
      const lat = Array.isArray(s) ? s[0] : s.lat;
      const lon = Array.isArray(s) ? s[1] : s.lon;
      const iconName = Array.isArray(s) ? s[2] : s.icon;
      const temp = Array.isArray(s) ? s[3] : s.temp;
      if (!lat || !lon) return;
      if (!bounds.contains([lat, lon])) return;

      const icon = L.divIcon({
        html: `<div style="text-align:center;font-size:10px;color:#333;text-shadow:0 0 4px rgba(255,255,255,0.9)"><div style="font-size:18px">${iconName || "⬤"}</div><div>${temp || ""}</div></div>`,
        className: "",
        iconSize: [28, 28],
        iconAnchor: [14, 14],
      });

      const marker = L.marker([lat, lon], { icon }).addTo(this._map);
      this._stationMarkers.push(marker);
    });
  }

  _loadLeafletCSS() {
    if (document.querySelector("#rr-leaflet-css")) return;
    const link = document.createElement("link");
    link.id = "rr-leaflet-css";
    link.rel = "stylesheet";
    link.href = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css";
    document.head.appendChild(link);
  }

  firstUpdated() {
    this._loadLeafletCSS();
    requestAnimationFrame(() => this._initMap());
  }

  _initMap() {
    const container = this.shadowRoot?.getElementById("map");
    if (!container) return;

    const center = this._getConfiguredCenter();
    const initialCenter = center ? [center.lat, center.lon] : DEFAULT_CENTER;
    const zoom = center ? DEFAULT_ZOOM + 2 : DEFAULT_ZOOM;

    this._map = L.map(container, {
      center: initialCenter,
      zoom,
      zoomControl: false,
      attributionControl: true,
    });

    this._osmLayer = L.tileLayer(OSM_URL, {
      attribution: OSM_ATTR,
      maxZoom: 18,
      referrerPolicy: "origin",
    }).addTo(this._map);

    this._map.on("moveend", () => this._updateStationMarkers());
    this._map.whenReady(() => {
      this._updateCenterMarker();
      this._buildFrames();
    });
  }

  updated(changed) {
    if (changed.has("hass") && this._map) {
      const radarData = this._getStateAttributes([
        "sensor.rainradar_radar_frames",
        "sensor.rainradar_frames",
        "sensor.radar_frames",
        "rainradar.radar_frames",
      ]);
      const radarUpdate = radarData?.last_update || null;
      if (!this._frames.length || (radarUpdate && radarUpdate !== this._lastRadarUpdate)) {
        this._buildFrames();
      } else {
        this._updateCenterMarker();
        this._updateStationMarkers();
      }
    }

    if (changed.has("config") && this._map) {
      const height = Number(this.config?.height || 420);
      this.style.setProperty("--rainradar-card-height", `${height}px`);
      this._updateCenterMarker();
      this._recenter();
    }
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this._clearTimer();
    this._clearRetryTimer();
    if (this._map) {
      this._map.remove();
      this._map = null;
    }
  }

  render() {
    const pct = this._totalFrames > 0
      ? Math.round((this._loadedFrames.size / this._totalFrames) * 100)
      : 0;
    const maxIdx = Math.max(0, this._frames.length - 1);
    const height = Number(this.config?.height || 420);

    this.style.setProperty("--rainradar-card-height", `${height}px`);

    return html`
      <div class="controls">
        <div id="map"></div>
      </div>

      ${this._loading ? html`
        <div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:2000;background:var(--ha-card-background,#fff);padding:16px 24px;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,0.2);text-align:center;">
          <div>${this._showingNoData ? "Waiting for radar data..." : "Loading radar data..."}</div>
          <div style="font-size:12px;margin-top:4px;">${pct}% (${this._loadedFrames.size}/${this._totalFrames})</div>
        </div>
      ` : nothing}

      <div style="position:absolute;inset:0;z-index:1100;pointer-events:none;">
        <div style="position:absolute;top:8px;left:8px;display:flex;gap:4px;pointer-events:none;">
        <div style="display:flex;gap:2px;background:var(--ha-card-background,#fff);border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,0.15);overflow:hidden;pointer-events:auto;font-size:12px;">
          <button class="${this.config.mode === "5min" ? "active" : ""}"
            style="padding:6px 12px;border:none;cursor:pointer;background:${this.config.mode === "5min" ? "var(--primary-color,#03a9f4)" : "transparent"};color:${this.config.mode === "5min" ? "#fff" : "var(--primary-text-color,#333)"};font-weight:${this.config.mode === "5min" ? "600" : "400"}"
            @click=${() => { this.config.mode = "5min"; this._buildFrames(); }}>
            5-min
          </button>
          <button class="${this.config.mode === "15min" ? "active" : ""}"
            style="padding:6px 12px;border:none;cursor:pointer;background:${this.config.mode === "15min" ? "var(--primary-color,#03a9f4)" : "transparent"};color:${this.config.mode === "15min" ? "#fff" : "var(--primary-text-color,#333)"};font-weight:${this.config.mode === "15min" ? "600" : "400"}"
            @click=${() => { this.config.mode = "15min"; this._buildFrames(); }}>
            15-min
          </button>
        </div>
        </div>

        <div style="position:absolute;top:48px;right:8px;display:flex;flex-direction:column;gap:2px;pointer-events:auto;">
        <button style="background:var(--ha-card-background,#fff);border:none;border-radius:8px;padding:8px 12px;cursor:pointer;box-shadow:0 1px 4px rgba(0,0,0,0.15);font-size:16px;font-weight:700;color:var(--primary-text-color,#333);line-height:1;"
          @click=${() => this._map?.zoomIn()} title="Zoom in">+</button>
        <button style="background:var(--ha-card-background,#fff);border:none;border-radius:8px;padding:8px 12px;cursor:pointer;box-shadow:0 1px 4px rgba(0,0,0,0.15);font-size:16px;font-weight:700;color:var(--primary-text-color,#333);line-height:1;"
          @click=${() => this._map?.zoomOut()} title="Zoom out">−</button>
        </div>

        <button style="position:absolute;top:8px;right:8px;pointer-events:auto;background:var(--ha-card-background,#fff);border:none;border-radius:8px;padding:8px;cursor:pointer;box-shadow:0 1px 4px rgba(0,0,0,0.15);display:flex;align-items:center;justify-content:center;color:var(--primary-text-color,#333);font-size:18px;"
        @click=${this._recenter} title="Recenter to home">
        <ha-icon icon="mdi:crosshairs-gps"></ha-icon>
      </button>

        <div style="position:absolute;bottom:16px;left:50%;transform:translateX(-50%);display:flex;align-items:center;gap:6px;background:var(--ha-card-background,#fff);padding:6px 14px;border-radius:24px;box-shadow:0 2px 8px rgba(0,0,0,0.2);font-size:13px;pointer-events:auto;">
        <button style="background:none;border:none;cursor:pointer;padding:4px 6px;border-radius:4px;display:flex;align-items:center;color:var(--primary-text-color,#333);font-size:16px;"
          @click=${this._togglePlay} title=${this._playing ? "Pause" : "Play"}>
          <ha-icon icon=${this._playing ? "mdi:pause" : "mdi:play"}></ha-icon>
        </button>

        <input type="range" min="0" max=${maxIdx} .value=${this._currentIndex}
          @input=${this._onSlider}
          style="width:100px;height:4px;-webkit-appearance:none;background:var(--secondary-text-color,#999);border-radius:2px;outline:none;cursor:pointer;" />

        <span style="font-size:11px;color:var(--secondary-text-color,#666);min-width:70px;text-align:center;">${this._timeLabel}</span>

        <button style="background:none;border:none;cursor:pointer;padding:2px 5px;border-radius:4px;color:var(--primary-text-color,#333);font-size:11px;font-weight:${this._speed === 0.5 ? "700" : "400"};color:${this._speed === 0.5 ? "var(--primary-color,#03a9f4)" : "inherit"}"
          @click=${() => this._setSpeed(0.5)}>½×</button>
        <button style="background:none;border:none;cursor:pointer;padding:2px 5px;border-radius:4px;color:var(--primary-text-color,#333);font-size:11px;font-weight:${this._speed === 1 ? "700" : "400"};color:${this._speed === 1 ? "var(--primary-color,#03a9f4)" : "inherit"}"
          @click=${() => this._setSpeed(1)}>1×</button>
        <button style="background:none;border:none;cursor:pointer;padding:2px 5px;border-radius:4px;color:var(--primary-text-color,#333);font-size:11px;font-weight:${this._speed === 2 ? "700" : "400"};color:${this._speed === 2 ? "var(--primary-color,#03a9f4)" : "inherit"}"
          @click=${() => this._setSpeed(2)}>2×</button>
        </div>
      </div>

      <div style="position:absolute;bottom:76px;right:8px;z-index:1100;background:rgba(255,255,255,0.9);padding:4px 8px;border-radius:6px;font-size:10px;box-shadow:0 1px 4px rgba(0,0,0,0.15);line-height:1.5;pointer-events:auto;">
        <div style="display:flex;align-items:center;gap:3px;"><span style="width:10px;height:10px;border-radius:2px;background:#00ff00;display:inline-block;"></span> light</div>
        <div style="display:flex;align-items:center;gap:3px;"><span style="width:10px;height:10px;border-radius:2px;background:#00aaff;display:inline-block;"></span> moderate</div>
        <div style="display:flex;align-items:center;gap:3px;"><span style="width:10px;height:10px;border-radius:2px;background:#ff0000;display:inline-block;"></span> heavy</div>
        <div style="display:flex;align-items:center;gap:3px;"><span style="width:10px;height:10px;border-radius:2px;background:#ff00ff;display:inline-block;"></span> extreme</div>
      </div>
    `;
  }
}

customElements.define("rainradar-card", RainradarCard);

class RainradarCardEditor extends LitElement {
  static properties = {
    hass: { type: Object },
    config: { type: Object },
  };

  setConfig(config) {
    this.config = { ...RainradarCard.getStubConfig(), ...config };
  }

  _handleChange() {
    const ev = new CustomEvent("config-changed", {
      detail: { config: this.config },
      bubbles: true,
      composed: true,
    });
    this.dispatchEvent(ev);
  }

  _modeChanged(e) {
    this.config.mode = e.target.value;
    this._handleChange();
  }

  _defaultLocChanged(e) {
    this.config.default_location = e.target.value;
    this._handleChange();
  }

  render() {
    if (!this.hass || !this.config) return nothing;

    return html`
      <ha-form
        .hass=${this.hass}
        .data=${this.config}
        .schema=${[
          {
            name: "mode",
            label: "Radar mode",
            selector: { select: { options: [
              { value: "5min", label: "5-min (2h nowcast)" },
              { value: "15min", label: "15-min (14h forecast)" },
            ]}},
          },
          {
            name: "center_entity",
            label: "Center map on entity",
            selector: { entity: { domain: ["zone", "device_tracker"] } },
          },
          {
            name: "height",
            label: "Widget height",
            selector: { select: { options: [
              { value: 320, label: "320 px" },
              { value: 420, label: "420 px" },
              { value: 560, label: "560 px" },
              { value: 720, label: "720 px" },
            ]}},
          },
        ]}
        .computeLabel=${(s) => s.label}
        @value-changed=${this._handleChange}
      ></ha-form>
    `;
  }
}

customElements.define("rainradar-card-editor", RainradarCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "rainradar-card",
  name: "Rainradar",
  description: "DWD rain radar map with animation for Germany",
  preview: true,
});

export { RainradarCard };
