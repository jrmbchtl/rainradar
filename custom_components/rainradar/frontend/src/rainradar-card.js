import { LitElement, html, css, nothing } from "lit";
import L from "leaflet";

const CARD_VERSION = "0.3.0-rc2";
const OSM_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const OSM_ATTR = "&copy; <a href='https://openstreetmap.org'>OSM</a>";

const DEFAULT_CENTER = [51.1657, 10.4515];
const DEFAULT_ZOOM = 7;
const DEFAULT_HOME_ZOOM = 9;
const FRAME_MS = 150;
const DATA_RETRY_MS = 3000;

const RADAR_BOUNDS = [
  [47.27, 5.7],
  [55.05, 15.0],
];

// A slightly padded max-bounds rectangle so panning the map stops at the
// edges of Germany instead of sliding into the Atlantic.
const MAX_BOUNDS = [
  [44.0, 3.0],
  [58.0, 17.5],
];

const _isDebugFromUrl = () => {
  try {
    if (typeof window === "undefined") return false;
    const qs = window.location?.search || "";
    if (qs.includes("debug=1") || qs.includes("rainradar_debug=1")) return true;
    if (window.localStorage?.getItem("rainradar_debug") === "1") return true;
  } catch (e) {
    // ignore (e.g. cross-origin or no location)
  }
  return false;
};

const _dlog = (tag, ...args) => {
  // Console log with a consistent prefix and tag. Opt in via ?debug=1
  // (URL or localStorage["rainradar_debug"] = "1").
  if (typeof window === "undefined" || !window.__RAINRADAR_DEBUG) return;
  try {
    // eslint-disable-next-line no-console
    console.log(`%c[rainradar ${tag}]`, "color:#03a9f4;font-weight:600", ...args);
  } catch (e) {
    // ignore
  }
};

const _dwarn = (tag, ...args) => {
  // Always-on warn-level log so we leave a trace in the console even when
  // ?debug=1 is not set. Used sparingly for things the user must see
  // (e.g. the module being loaded, the version).
  try {
    // eslint-disable-next-line no-console
    console.warn(`[rainradar ${tag}]`, ...args);
  } catch (e) {
    // ignore
  }
};

if (typeof window !== "undefined") {
  window.__RAINRADAR_DEBUG = _isDebugFromUrl();
  // Always-on breadcrumb so we can confirm the new bundle loaded.
  _dwarn("load", `rainradar-card.js v${CARD_VERSION} loaded; debug=${window.__RAINRADAR_DEBUG}`);
}

class RainradarCard extends LitElement {
  static properties = {
    hass: { type: Object },
    config: { type: Object },
    _frames: { state: true },
    _currentIndex: { state: true },
    _playing: { state: true },
    _showingNoData: { state: true },
    _speed: { state: true },
    _timeLabel: { state: true },
    _isPreview: { state: true },
  };

  constructor() {
    super();
    this._frames = [];
    this._currentIndex = 0;
    this._playing = false;
    this._showingNoData = true;
    this._speed = 1;
    this._timeLabel = "";
    this._isPreview = false;
    this._map = null;
    this._osmLayer = null;
    this._overlay = null;
    this._centerMarker = null;
    this._stationMarkers = [];
    this._timer = null;
    this._retryTimer = null;
    this._resizeObserver = null;
    this._lastFramesSignature = null;
    this._lastCenterKey = null;
    this._hassRef = null;
    this._debug = _isDebugFromUrl();
    this._diagOpen = false;
    this._mapSize = null;
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
      z-index: 0;
    }

    #map .leaflet-pane {
      position: absolute;
      left: 0;
      top: 0;
    }

    .controls {
      position: absolute;
      inset: 0;
      pointer-events: none;
      z-index: 1100;
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
    if (!config) config = {};
    const merged = { ...RainradarCard.getStubConfig(), ...config };
    const oldConfig = this.config;
    this.config = merged;
    _dlog("setConfig", merged);
    if (oldConfig && this._map) {
      if (oldConfig.mode !== merged.mode) {
        this._buildFrames();
      }
      if (oldConfig.center_entity !== merged.center_entity ||
          oldConfig.default_location !== merged.default_location) {
        this._lastCenterKey = null;
        this._updateCenterMarker();
        this._recenter();
      }
      if (oldConfig.height !== merged.height) {
        this.style.setProperty("--rainradar-card-height", `${Number(merged.height || 420)}px`);
      }
    }
  }

  getLayoutOptions() {
    return {
      grid_columns: 4,
      grid_rows: 2,
      grid_columns_max: 4,
    };
  }

  getCardSize() {
    return 4;
  }

  _getEntityLatLon(entityId) {
    const state = this.hass?.states?.[entityId];
    const attrs = state?.attributes;
    if (!attrs) return null;
    const lat = attrs.latitude ?? attrs.lat;
    const lon = attrs.longitude ?? attrs.lon ?? attrs.lng;
    if (lat == null || lon == null) return null;
    const name = attrs.friendly_name || state?.name || entityId;
    return { lat: Number(lat), lon: Number(lon), name, id: entityId };
  }

  _getConfiguredCenter() {
    const preferred =
      this.config?.center_entity ||
      this.config?.default_location ||
      "zone.home";
    return (
      this._getEntityLatLon(preferred) ||
      this._getEntityLatLon("zone.home") ||
      null
    );
  }

  _getFramesData() {
    const attrs = this._getFramesAttributes();
    if (!attrs) return null;
    const frames = attrs.frames || attrs;
    const past = Array.isArray(frames.past) ? frames.past : [];
    const nowcast = Array.isArray(frames.nowcast) ? frames.nowcast : [];
    const forecast = Array.isArray(frames.forecast) ? frames.forecast : [];
    return {
      past,
      nowcast,
      forecast,
      lastUpdate: attrs.last_update || null,
      frameError: attrs.frame_error || null,
    };
  }

  _getFramesAttributes() {
    if (this.hass?.states?.["sensor.rainradar_radar_frames"]?.attributes) {
      return this.hass.states["sensor.rainradar_radar_frames"].attributes;
    }
    for (const id of Object.keys(this.hass?.states || {})) {
      const lid = id.toLowerCase();
      if (lid.includes("rainradar") && lid.includes("radar_frames")) {
        const attrs = this.hass.states[id]?.attributes;
        if (attrs) return attrs;
      }
    }
    return null;
  }

  _getStationsData() {
    if (this.hass?.states?.["sensor.rainradar_stations"]?.attributes) {
      return this.hass.states["sensor.rainradar_stations"].attributes.stations || [];
    }
    for (const id of Object.keys(this.hass?.states || {})) {
      const lid = id.toLowerCase();
      if (lid.includes("rainradar") && lid.includes("station")) {
        const attrs = this.hass.states[id]?.attributes;
        if (attrs?.stations) return attrs.stations;
      }
    }
    return [];
  }

  _framesSignature(frames) {
    return `${frames.lastUpdate || ""}|${frames.past.length}|${frames.nowcast.length}|${frames.forecast.length}`;
  }

  _buildFrames() {
    this._clearTimer();
    this._clearRetryTimer();
    this._frames = [];
    this._currentIndex = 0;
    this._showingNoData = true;

    const data = this._getFramesData();
    if (!data) {
      this._timeLabel = "Waiting for radar data...";
      _dlog("frames", "no sensor data yet");
      this._retryTimer = setTimeout(() => {
        if (this._map) this._buildFrames();
      }, DATA_RETRY_MS);
      this.requestUpdate();
      return;
    }

    const past = data.past;
    const nowcast = data.nowcast;
    const forecast = data.forecast;
    _dlog("frames", "sensor has", past.length, "past,", nowcast.length, "nowcast,", forecast.length, "forecast",
      data.frameError ? `error="${data.frameError}"` : "");

    this._frames =
      this.config.mode === "15min"
        ? [...past, ...nowcast, ...forecast]
        : [...past, ...nowcast];

    if (!this._frames.length) {
      this._timeLabel = data.frameError
        ? `No frames: ${data.frameError}`
        : "No frames available — waiting for next update";
      this._showingNoData = true;
      this._retryTimer = setTimeout(() => {
        if (this._map) this._buildFrames();
      }, DATA_RETRY_MS);
      this.requestUpdate();
      return;
    }

    this._showingNoData = false;
    this._lastFramesSignature = this._framesSignature(data);

    if (!this._map) {
      _dlog("frames", "frames ready but no map yet");
      this.requestUpdate();
      return;
    }

    if (!this._overlay) {
      _dlog("overlay", "create", { url: this._frames[0].url, bounds: RADAR_BOUNDS });
      this._overlay = L.imageOverlay(
        this._frames[0].url,
        RADAR_BOUNDS,
        { opacity: 0.7, interactive: false, crossOrigin: false }
      ).addTo(this._map);
      this._overlay.on("load", () => _dlog("overlay", "image loaded", this._overlay?._url));
      this._overlay.on("error", (ev) => _dlog("overlay", "image error", ev));
    } else {
      _dlog("overlay", "setUrl", this._frames[0].url);
      this._overlay.setUrl(this._frames[0].url);
    }

    this._showFrame(0);
    this.requestUpdate();
  }

  _showFrame(idx) {
    if (!this._map || !this._frames.length) return;
    const frame = this._frames[idx];
    if (!frame) return;

    if (this._overlay) {
      this._overlay.setUrl(frame.url);
    }
    this._currentIndex = idx;
    try {
      const d = new Date(frame.ts);
      this._timeLabel = d.toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch (e) {
      this._timeLabel = "";
    }
    this._preloadAdjacent(idx);
    this.requestUpdate();
  }

  _preloadAdjacent(idx) {
    if (!this._frames.length || !this._preloaded) return;
    const n = this._frames.length;
    for (const delta of [-1, 1, -2, 2]) {
      const i = ((idx + delta) % n + n) % n;
      const url = this._frames[i]?.url;
      if (url && !this._preloaded.has(url)) {
        this._preloaded.add(url);
        const img = new Image();
        img.src = url;
      }
    }
  }

  _togglePlay() {
    if (!this._frames.length) return;
    this._playing = !this._playing;
    if (this._playing) {
      this._tick();
    } else {
      this._clearTimer();
    }
    this.requestUpdate();
  }

  _tick() {
    if (!this._playing || !this._frames.length) return;
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

  _clearRetryTimer() {
    if (this._retryTimer) {
      clearTimeout(this._retryTimer);
      this._retryTimer = null;
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

  _setMode(mode) {
    if (this.config.mode === mode) return;
    this.config = { ...this.config, mode };
    this._buildFrames();
    this._dispatchConfigChange();
  }

  _dispatchConfigChange() {
    this.dispatchEvent(
      new CustomEvent("config-changed", {
        detail: { config: this.config },
        bubbles: true,
        composed: true,
      })
    );
  }

  _onSlider(e) {
    const idx = parseInt(e.target.value, 10);
    if (Number.isFinite(idx)) this._showFrame(idx);
  }

  _recenter() {
    if (!this._map) return;
    const center = this._getConfiguredCenter();
    const lat = center?.lat ?? DEFAULT_CENTER[0];
    const lon = center?.lon ?? DEFAULT_CENTER[1];
    // Use setView (not flyTo) so the recenter is instantaneous and
    // does not trigger the "home marker jumps while user is panning"
    // perception from a 500ms tween.
    _dlog("recenter", "->", lat, lon, "zoom", DEFAULT_HOME_ZOOM);
    this._map.setView([lat, lon], DEFAULT_HOME_ZOOM, { animate: false });
  }

  _updateCenterMarker() {
    if (!this._map) return;
    const center = this._getConfiguredCenter();
    const key = center ? `${center.id}|${center.lat}|${center.lon}` : "none";
    if (key === this._lastCenterKey) return;
    this._lastCenterKey = key;

    if (this._centerMarker) {
      this._map.removeLayer(this._centerMarker);
      this._centerMarker = null;
    }
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
    if (!this._map) return;
    this._stationMarkers.forEach((m) => this._map.removeLayer(m));
    this._stationMarkers = [];

    const stations = this._getStationsData();
    if (!stations.length) return;

    const bounds = this._map.getBounds();
    const sensorState = this.hass?.states?.["sensor.rainradar_stations"];
    const nameMap = sensorState?.attributes?.station_names || {};
    stations.forEach((s, i) => {
      if (!s || typeof s.lat !== "number" || typeof s.lon !== "number") return;
      if (!bounds.contains([s.lat, s.lon])) return;
      const label = nameMap[String(i)] || s.name || `${s.lat.toFixed(2)},${s.lon.toFixed(2)}`;
      const marker = L.circleMarker([s.lat, s.lon], {
        radius: 3,
        color: "#03a9f4",
        fillColor: "#03a9f4",
        fillOpacity: 0.6,
        weight: 1,
      })
        .bindTooltip(label, { direction: "top", offset: [0, -4] })
        .addTo(this._map);
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

  connectedCallback() {
    super.connectedCallback();
    this._debug = _isDebugFromUrl();
    if (this._debug) {
      window.__RAINRADAR_DEBUG = true;
      _dlog("connect", "v" + CARD_VERSION, "preview=" + this._isInPreviewContext());
    }
    // Synchronously detect the card-picker preview context. If we are inside
    // one, do absolutely nothing: no CSS load, no ResizeObserver, no
    // queueMicrotask, no Leaflet. The picker wraps the element in an
    // `until()` directive that resolves only after `_renderCardElement`
    // settles; any async work we schedule can keep the spinner spinning
    // forever and trip the picker's `parentElement.shadowRoot is null`
    // race when it tears the preview down.
    this._isPreview = this._isInPreviewContext();
    if (this._isPreview) {
      return;
    }
    // Real dashboard: defer map init so the host layout settles first.
    if (!this._map) {
      queueMicrotask(() => this._initMap());
    }
  }

  firstUpdated() {
    if (this._isPreview) {
      return;
    }
    this.style.setProperty(
      "--rainradar-card-height",
      `${Number(this.config?.height || 420)}px`
    );
    this._preloaded = new Set();
    try {
      this._loadLeafletCSS();
    } catch (e) {
      // ignore CSS load failure
    }
    try {
      this._resizeObserver = new ResizeObserver(() => {
        if (this._map) {
          setTimeout(() => this._map.invalidateSize(false), 80);
        } else if (!this._isPreview) {
          this._initMap();
        }
      });
      this._resizeObserver.observe(this);
    } catch (e) {
      // ResizeObserver not supported
    }
  }

  _isInPreviewContext() {
    let el = this.parentElement;
    while (el) {
      const tag = (el.tagName || "").toLowerCase();
      if (
        tag === "hui-card-picker" ||
        tag === "hui-dialog-create-card" ||
        tag === "hui-card-preview"
      ) {
        return true;
      }
      el = el.parentElement;
    }
    return false;
  }

  _initMap() {
    if (this._map) return;
    if (this._isPreview) return;
    const container = this.shadowRoot?.getElementById("map");
    if (!container) {
      _dlog("init", "no #map container, aborting");
      return;
    }
    const rect = container.getBoundingClientRect();
    if (rect.width < 200 || rect.height < 100) {
      _dlog("init", "container too small, waiting", { w: rect.width, h: rect.height });
      return;
    }

    const center = this._getConfiguredCenter();
    const initialCenter = center ? [center.lat, center.lon] : DEFAULT_CENTER;
    const zoom = center ? DEFAULT_HOME_ZOOM : DEFAULT_ZOOM;
    _dlog("init", "creating map", { center: initialCenter, zoom, size: rect });

    try {
      this._map = L.map(container, {
        crs: L.CRS.EPSG3857,
        center: initialCenter,
        zoom,
        minZoom: 5,
        maxZoom: 12,
        zoomControl: false,
        attributionControl: true,
        scrollWheelZoom: "center",
        // worldCopyJump was removed: it caused the center marker to
        // "jump" when zooming near the (irrelevant) world boundary and
        // Germany is fully within a single world instance anyway.
        maxBounds: MAX_BOUNDS,
        maxBoundsViscosity: 0.8,
      });
    } catch (e) {
      this._map = null;
      _dlog("init", "Leaflet map init failed", e);
      return;
    }

    try {
      this._osmLayer = L.tileLayer(OSM_URL, {
        attribution: OSM_ATTR,
        maxZoom: 18,
        referrerPolicy: "origin",
      }).addTo(this._map);
      this._osmLayer.on("tileerror", (ev) => _dlog("osm", "tile error", ev?.tile?.src));
      this._osmLayer.on("tileload", () => { /* noop, just to keep ref */ });
    } catch (e) {
      _dlog("init", "OSM layer init failed", e);
    }

    try {
      this._map.on("moveend zoomend resize", () => {
        if (!this._map) return;
        const c = this._map.getCenter();
        this._mapSize = this._map.getSize();
        _dlog("map", `moveend/zoomend center=(${c.lat.toFixed(3)},${c.lng.toFixed(3)}) zoom=${this._map.getZoom()} size=${this._mapSize.x}x${this._mapSize.y}`);
        this._updateStationMarkers();
      });
      this._map.whenReady(() => {
        _dlog("map", "whenReady");
        this._updateCenterMarker();
        this._updateStationMarkers();
        this._buildFrames();
        if (this._map) {
          this._map.attributionControl.addAttribution("DWD");
          setTimeout(() => {
            try {
              this._map.invalidateSize(false);
              _dlog("map", "invalidateSize done", this._map.getSize());
            } catch (e) {
              // ignore
            }
          }, 200);
        }
      });
    } catch (e) {
      _dlog("init", "map whenReady setup failed", e);
    }
  }

  updated(changed) {
    if (this._isPreview) return;
    if (changed.has("hass") && this.hass && this.hass !== this._hassRef) {
      this._hassRef = this.hass;
      const data = this._getFramesData();
      if (!data) {
        this._buildFrames();
      } else if (this._framesSignature(data) !== this._lastFramesSignature) {
        this._buildFrames();
      } else {
        this._updateCenterMarker();
        this._updateStationMarkers();
      }
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
    this._overlay = null;
    this._centerMarker = null;
    this._stationMarkers = [];
    if (this._resizeObserver) {
      try {
        this._resizeObserver.disconnect();
      } catch (e) {}
      this._resizeObserver = null;
    }
    // Reset preview flag so a subsequent connect to a real dashboard goes
    // through the normal init path.
    this._isPreview = false;
  }

  render() {
    if (this._isPreview) {
      return html`
        <div
          style="display:flex;align-items:center;justify-content:center;width:100%;height:100%;min-height:300px;color:var(--secondary-text-color,#666);font-size:14px;font-family:var(--paper-font-body1_-_font-family);"
        >
          Rainradar
        </div>
      `;
    }
    const maxIdx = Math.max(0, this._frames.length - 1);
    const sensorAttrs = this._getFramesAttributes() || {};
    const sensorState = this.hass?.states?.["sensor.rainradar_radar_frames"]?.state
      ?? Object.values(this.hass?.states || {}).find(s => (s?.entity_id || "").includes("rainradar") && (s?.entity_id || "").includes("radar_frames"))?.state
      ?? "—";
    const mapInfo = this._map
      ? (() => {
          const c = this._map.getCenter();
          return {
            center: `${c.lat.toFixed(3)}, ${c.lng.toFixed(3)}`,
            zoom: this._map.getZoom(),
            size: this._map.getSize(),
            bounds: this._map.getBounds().toBBoxString(),
          };
        })()
      : null;
    const diagText = [
      `version: ${CARD_VERSION}`,
      `frames: ${this._frames.length} / max ${maxIdx}`,
      `mode: ${this.config?.mode ?? "5min"}`,
      `center_entity: ${this.config?.center_entity || this.config?.default_location || "zone.home"}`,
      `sensor state: ${sensorState}`,
      `frame_error: ${sensorAttrs.frame_error || "none"}`,
      `last_update: ${sensorAttrs.last_update || "—"}`,
      mapInfo
        ? `map center: ${mapInfo.center} zoom: ${mapInfo.zoom} size: ${mapInfo.size.x}x${mapInfo.size.y}`
        : "map: not initialised",
      `?debug=1 in URL enables verbose console logs`,
    ].join("\n");

    return html`
      <div id="map"></div>

      ${this._showingNoData
        ? html`
            <div
              style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:2000;background:var(--ha-card-background,#fff);padding:16px 24px;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,0.2);text-align:center;max-width:80%;"
            >
              <div>${this._timeLabel || "Loading radar data..."}</div>
            </div>
          `
        : nothing}

      ${this._diagOpen
        ? html`
            <pre
              style="position:absolute;left:8px;bottom:80px;z-index:1200;background:rgba(0,0,0,0.78);color:#e0e0e0;padding:10px 12px;border-radius:8px;font-size:11px;line-height:1.4;max-width:380px;max-height:240px;overflow:auto;pointer-events:auto;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;"
              @click=${(e) => e.stopPropagation()}
            >${diagText}</pre>
          `
        : nothing}

      <div class="controls">
        <div
          style="position:absolute;top:8px;left:8px;display:flex;gap:2px;background:var(--ha-card-background,#fff);border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,0.15);overflow:hidden;pointer-events:auto;font-size:12px;"
        >
          <button
            style="padding:6px 12px;border:none;cursor:pointer;background:${this
              .config.mode === "5min"
              ? "var(--primary-color,#03a9f4)"
              : "transparent"};color:${this.config.mode === "5min"
              ? "#fff"
              : "var(--primary-text-color,#333)"};font-weight:${this.config.mode ===
            "5min"
              ? "600"
              : "400"}"
            @click=${() => this._setMode("5min")}
          >
            5-min
          </button>
          <button
            style="padding:6px 12px;border:none;cursor:pointer;background:${this
              .config.mode === "15min"
              ? "var(--primary-color,#03a9f4)"
              : "transparent"};color:${this.config.mode === "15min"
              ? "#fff"
              : "var(--primary-text-color,#333)"};font-weight:${this.config.mode ===
            "15min"
              ? "600"
              : "400"}"
            @click=${() => this._setMode("15min")}
          >
            15-min
          </button>
        </div>

        <div
          style="position:absolute;top:48px;right:8px;display:flex;flex-direction:column;gap:2px;pointer-events:auto;"
        >
          <button
            style="background:var(--ha-card-background,#fff);border:none;border-radius:8px;padding:8px 12px;cursor:pointer;box-shadow:0 1px 4px rgba(0,0,0,0.15);font-size:16px;font-weight:700;color:var(--primary-text-color,#333);line-height:1;"
            @click=${() => this._map?.zoomIn()}
            title="Zoom in"
          >
            +
          </button>
          <button
            style="background:var(--ha-card-background,#fff);border:none;border-radius:8px;padding:8px 12px;cursor:pointer;box-shadow:0 1px 4px rgba(0,0,0,0.15);font-size:16px;font-weight:700;color:var(--primary-text-color,#333);line-height:1;"
            @click=${() => this._map?.zoomOut()}
            title="Zoom out"
          >
            −
          </button>
        </div>

        <button
          style="position:absolute;top:8px;right:8px;pointer-events:auto;background:var(--ha-card-background,#fff);border:none;border-radius:8px;padding:8px;cursor:pointer;box-shadow:0 1px 4px rgba(0,0,0,0.15);display:flex;align-items:center;justify-content:center;color:var(--primary-text-color,#333);font-size:18px;"
          @click=${this._recenter}
          title="Recenter to home"
        >
          <ha-icon icon="mdi:crosshairs-gps"></ha-icon>
        </button>

        <button
          style="position:absolute;top:48px;left:8px;pointer-events:auto;background:var(--ha-card-background,#fff);border:none;border-radius:8px;padding:6px 8px;cursor:pointer;box-shadow:0 1px 4px rgba(0,0,0,0.15);display:flex;align-items:center;justify-content:center;color:var(--primary-text-color,#333);font-size:11px;font-weight:600;line-height:1;opacity:0.85;"
          @click=${() => { this._diagOpen = !this._diagOpen; this.requestUpdate(); }}
          title="Toggle diagnostic panel"
        >
          <ha-icon icon="mdi:information-outline" style="font-size:14px;margin-right:2px;"></ha-icon>i
        </button>

        <div
          style="position:absolute;bottom:16px;left:50%;transform:translateX(-50%);display:flex;align-items:center;gap:6px;background:var(--ha-card-background,#fff);padding:6px 14px;border-radius:24px;box-shadow:0 2px 8px rgba(0,0,0,0.2);font-size:13px;pointer-events:auto;"
        >
          <button
            style="background:none;border:none;cursor:pointer;padding:4px 6px;border-radius:4px;display:flex;align-items:center;color:var(--primary-text-color,#333);font-size:16px;"
            @click=${this._togglePlay}
            title=${this._playing ? "Pause" : "Play"}
          >
            <ha-icon icon=${this._playing ? "mdi:pause" : "mdi:play"}></ha-icon>
          </button>

          <input
            type="range"
            min="0"
            max=${maxIdx}
            .value=${this._currentIndex}
            @input=${this._onSlider}
            style="width:100px;height:4px;-webkit-appearance:none;background:var(--secondary-text-color,#999);border-radius:2px;outline:none;cursor:pointer;"
          />

          <span
            style="font-size:11px;color:var(--secondary-text-color,#666);min-width:70px;text-align:center;"
            >${this._timeLabel}</span
          >

          ${[0.5, 1, 2].map(
            (s) => html`
              <button
                style="background:none;border:none;cursor:pointer;padding:2px 5px;border-radius:4px;font-size:11px;font-weight:${this
                  ._speed === s
                  ? "700"
                  : "400"};color:${this._speed === s
                  ? "var(--primary-color,#03a9f4)"
                  : "var(--primary-text-color,#333)"}"
                @click=${() => this._setSpeed(s)}
              >
                ${s === 1 ? "1×" : s === 0.5 ? "½×" : "2×"}
              </button>
            `
          )}
        </div>
      </div>

      <div
        style="position:absolute;bottom:76px;right:8px;z-index:1100;background:rgba(255,255,255,0.9);padding:4px 8px;border-radius:6px;font-size:10px;box-shadow:0 1px 4px rgba(0,0,0,0.15);line-height:1.5;pointer-events:auto;"
      >
        <div style="font-weight:600;margin-bottom:2px;">DWD precipitation</div>
        <div style="display:flex;align-items:center;gap:3px;">
          <span style="width:10px;height:10px;border-radius:2px;background:#00ff00;display:inline-block;"></span> light
        </div>
        <div style="display:flex;align-items:center;gap:3px;">
          <span style="width:10px;height:10px;border-radius:2px;background:#00aaff;display:inline-block;"></span> moderate
        </div>
        <div style="display:flex;align-items:center;gap:3px;">
          <span style="width:10px;height:10px;border-radius:2px;background:#ff0000;display:inline-block;"></span> heavy
        </div>
        <div style="display:flex;align-items:center;gap:3px;">
          <span style="width:10px;height:10px;border-radius:2px;background:#ff00ff;display:inline-block;"></span> extreme
        </div>
      </div>
    `;
  }
}

try {
  customElements.define("rainradar-card", RainradarCard);
} catch (e) {}

class RainradarCardEditor extends LitElement {
  static properties = {
    hass: { type: Object },
    config: { type: Object },
  };

  setConfig(config) {
    this.config = { ...RainradarCard.getStubConfig(), ...config };
  }

  _handleChange() {
    this.dispatchEvent(
      new CustomEvent("config-changed", {
        detail: { config: this.config },
        bubbles: true,
        composed: true,
      })
    );
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
            selector: {
              select: {
                options: [
                  { value: "5min", label: "5-min (2h nowcast)" },
                  { value: "15min", label: "15-min (14h forecast)" },
                ],
              },
            },
          },
          {
            name: "center_entity",
            label: "Center map on entity",
            selector: { entity: { domain: ["zone", "device_tracker"] } },
          },
          {
            name: "height",
            label: "Widget height",
            selector: {
              select: {
                options: [
                  { value: 320, label: "320 px" },
                  { value: 420, label: "420 px" },
                  { value: 560, label: "560 px" },
                  { value: 720, label: "720 px" },
                ],
              },
            },
          },
        ]}
        .computeLabel=${(s) => s.label}
        @value-changed=${(e) => {
          this.config = { ...this.config, ...e.detail.value };
          this._handleChange();
        }}
      ></ha-form>
    `;
  }
}

try {
  customElements.define("rainradar-card-editor", RainradarCardEditor);
} catch (e) {}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "rainradar-card",
  name: "Rainradar",
  description: "DWD rain radar map with animation for Germany",
  preview: true,
});

export { RainradarCard };
