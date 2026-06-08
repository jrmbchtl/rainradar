import { LitElement, html, css, nothing } from "lit";
import L from "leaflet";

const CARD_VERSION = "0.5.0";
const OSM_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const OSM_ATTR = "&copy; <a href='https://openstreetmap.org'>OSM</a>";

const DEFAULT_CENTER = [51.1657, 10.4515];
const DEFAULT_ZOOM = 7;
const DEFAULT_HOME_ZOOM = 9;
const FRAME_MS = 150;
const DATA_RETRY_MS = 3000;

// Must match RADAR_BBOX_LONLAT in const.py. The DWD composite is
// Germany-only, so we ask the WMS for a much larger area (see
// const.py comment) — the basemap is then visible all the way to the
// overlay edge instead of stopping at the German border.
const RADAR_BOUNDS = [
  [42.0, -2.0],
  [60.0, 22.0],
];

// World-wide bounds: the DWD composite itself only covers Germany,
// but the user wants the basemap to extend further (e.g. to follow
// storms rolling in from France / the Atlantic). The overlay image
// is anchored to RADAR_BOUNDS inside that, so the radar data still
// only renders over Germany while the rest of the map stays visible.
const MAX_BOUNDS = [
  [-85.0, -180.0],
  [85.0, 180.0],
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
    _timeLabel: { state: true },
    _isPreview: { state: true },
  };

  constructor() {
    super();
    this._frames = [];
    this._currentIndex = 0;
    this._playing = false;
    this._showingNoData = true;
    this._timeLabel = "";
    this._isPreview = false;
    this._map = null;
    this._osmLayer = null;
    this._overlay = null;
    this._centerMarker = null;
    this._secondaryMarkers = [];
    this._stationMarkers = [];
    this._timer = null;
    this._retryTimer = null;
    this._resizeObserver = null;
    this._lastFramesSignature = null;
    this._lastCenterKey = null;
    this._hassRef = null;
    this._debug = _isDebugFromUrl();
    this._mapSize = null;
  }

  static styles = css`
    :host {
      display: block;
      position: relative;
      width: 100%;
      height: 100%;
      min-height: 300px;
      overflow: hidden;
      box-sizing: border-box;
      font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
    }

    #map {
      position: relative;
      width: 100%;
      height: 100%;
      min-height: 300px;
      border-radius: var(--ha-card-border-radius, 12px);
      overflow: hidden;
      z-index: 0;
      background: #ddd;
    }

    /*
     * Leaflet CSS is normally loaded from unpkg, but document-level
     * <link> stylesheets do NOT cascade into a Shadow DOM. Without these
     * rules the panes stack at the top-left, the attribution control
     * floats unstyled, and the map only "partially" renders. The
     * critical subset of leaflet.css is duplicated here.
     */
    #map .leaflet-pane,
    #map .leaflet-tile,
    #map .leaflet-marker-icon,
    #map .leaflet-marker-shadow,
    #map .leaflet-tile-container,
    #map .leaflet-pane > svg,
    #map .leaflet-pane > canvas,
    #map .leaflet-zoom-box,
    #map .leaflet-image-layer,
    #map .leaflet-layer {
      position: absolute;
      left: 0;
      top: 0;
    }
    #map .leaflet-container { overflow: hidden; }
    #map .leaflet-tile-pane    { z-index: 2; }
    #map .leaflet-overlay-pane { z-index: 4; pointer-events: none; }
    #map .leaflet-shadow-pane  { z-index: 5; }
    #map .leaflet-marker-pane  { z-index: 6; }
    #map .leaflet-tooltip-pane { z-index: 7; }
    #map .leaflet-popup-pane   { z-index: 8; }
    #map .leaflet-control { z-index: 800; pointer-events: auto; }
    #map .leaflet-top,
    #map .leaflet-bottom {
      z-index: 1000;
      pointer-events: none;
      position: absolute;
    }
    #map .leaflet-top { top: 0; }
    #map .leaflet-right { right: 0; position: absolute; }
    #map .leaflet-bottom { bottom: 0; }
    #map .leaflet-left { left: 0; position: absolute; }
    #map .leaflet-control {
      float: left;
      clear: both;
      pointer-events: auto;
    }
    #map .leaflet-right .leaflet-control { float: right; }
    #map .leaflet-top .leaflet-control { margin-top: 10px; }
    #map .leaflet-bottom .leaflet-control { margin-bottom: 10px; }
    #map .leaflet-left .leaflet-control { margin-left: 10px; }
    #map .leaflet-right .leaflet-control { margin-right: 10px; }
    #map .leaflet-control-attribution {
      background: rgba(255, 255, 255, 0.85);
      margin: 0;
      padding: 0 5px;
      color: #333;
      font: 11px/1.5 "Helvetica Neue", Arial, Helvetica, sans-serif;
      box-sizing: border-box;
    }
    #map .leaflet-control-attribution a {
      text-decoration: none;
      color: #0078A8;
    }
    #map .leaflet-control-attribution a:hover { text-decoration: underline; }
    #map .leaflet-control-zoom {
      position: absolute;
      top: 0;
      left: 0;
    }
    #map .leaflet-control-zoom a {
      background-color: rgba(255, 255, 255, 0.85);
      border-bottom: 1px solid #ccc;
      width: 22px;
      height: 22px;
      line-height: 22px;
      display: block;
      text-align: center;
      text-decoration: none;
      color: black;
      font: bold 18px "Helvetica Neue", Arial, Helvetica, sans-serif;
    }
    #map .leaflet-control-zoom a:hover { background-color: #fff; }
    #map .leaflet-control-zoom a:first-child {
      border-top-left-radius: 4px;
      border-top-right-radius: 4px;
    }
    #map .leaflet-control-zoom a:last-child {
      border-bottom-left-radius: 4px;
      border-bottom-right-radius: 4px;
      border-bottom: none;
    }
    #map .leaflet-tooltip {
      position: absolute;
      padding: 4px 8px;
      background-color: white;
      border: 1px solid #ccc;
      border-radius: 4px;
      white-space: nowrap;
      box-shadow: 0 1px 3px rgba(0,0,0,0.2);
      font: 12px/1.4 "Helvetica Neue", Arial, sans-serif;
      color: #333;
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
      center_entity: "zone.home",
      marker_color: "#d32f2f",
      secondary_markers: [],
    };
  }

  setConfig(config) {
    if (!config) config = {};
    // Deprecation warning: the `mode` option (5min/15min) was removed
    // in 0.3.8. ICON-D2 hourly forecast is no longer fetched; the card
    // shows past + 2h nowcast at 5-min resolution. Old configs with
    // `mode: "15min"` would otherwise silently behave like 5min.
    if (config && config.mode) {
      _dwarn(
        "setConfig",
        `Card config "mode=${config.mode}" is deprecated and ignored ` +
        `(removed in v0.3.8). Use the "rainradar-legend-card" for the ` +
        `DWD color legend.`
      );
    }
    // Deprecation warning: the `height` option was removed in 0.3.9
    // because a hard pixel height interacts badly with the Lovelace
    // grid layout (cards below get pushed around when this card
    // resizes). Size the card via the layout (grid_rows / panel
    // settings), not via per-card config.
    if (config && config.height) {
      _dwarn(
        "setConfig",
        `Card config "height=${config.height}" is deprecated and ignored ` +
        `(removed in v0.3.9). Control the card size from the dashboard ` +
        `layout instead.`
      );
    }
    const { mode: _ignoredMode, height: _ignoredHeight, ...rest } = config || {};
    const merged = { ...RainradarCard.getStubConfig(), ...rest };
    const oldConfig = this.config;
    this.config = merged;
    _dlog("setConfig", merged);
    if (oldConfig && this._map) {
      if (oldConfig.center_entity !== merged.center_entity ||
          oldConfig.default_location !== merged.default_location) {
        this._lastCenterKey = null;
        this._updateCenterMarker();
        this._recenter();
      }
      if (oldConfig.marker_color !== merged.marker_color) {
        this._lastCenterKey = null;
        this._updateCenterMarker();
      }
      if (oldConfig.secondary_markers !== merged.secondary_markers) {
        this._updateSecondaryMarkers();
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
    return 8;
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
    return {
      past,
      nowcast,
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
    return `${frames.lastUpdate || ""}|${frames.past.length}|${frames.nowcast.length}`;
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
    _dlog("frames", "sensor has", past.length, "past,", nowcast.length, "nowcast",
      data.frameError ? `error="${data.frameError}"` : "");

    this._frames = [...past, ...nowcast];

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

    this._rotateFromNow();

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

  _nowIndex() {
    const now = Date.now();
    let best = 0;
    let bestDiff = Infinity;
    for (let i = 0; i < this._frames.length; i++) {
      const diff = Math.abs(this._frames[i].ts - now);
      if (diff < bestDiff) {
        bestDiff = diff;
        best = i;
      }
    }
    return best;
  }

  _rotateFromNow() {
    const nowIdx = this._nowIndex();
    if (nowIdx === 0 || nowIdx >= this._frames.length) return;
    this._frames = [...this._frames.slice(nowIdx), ...this._frames.slice(0, nowIdx)];
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
    this._timer = setTimeout(() => this._tick(), FRAME_MS);
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

  _sanitizeColor(value, fallback) {
    if (typeof value !== "string") return fallback;
    const trimmed = value.trim();
    // Basic CSS color sanitization: accept hex, rgb()/rgba(), or a
    // short named-color token. Reject anything that could break out of
    // the inline style attribute.
    if (/^(#[0-9a-fA-F]{3,8}|rgba?\([0-9.,\s]+\)|[a-zA-Z]{3,32})$/.test(trimmed)) {
      return trimmed;
    }
    return fallback;
  }

  _makeMarkerIcon(color) {
    return L.divIcon({
      html: `<ha-icon icon="mdi:map-marker" style="font-size:30px;line-height:30px;color:${color};filter:drop-shadow(0 1px 2px rgba(0,0,0,0.4));display:block;"></ha-icon>`,
      className: "",
      // The mdi:map-marker teardrop tip sits at the bottom-center of a
      // 30x30 div (a few px of internal padding). Anchor the tip on
      // the marker's lat/lon.
      iconSize: [30, 30],
      iconAnchor: [15, 28],
    });
  }

  _updateCenterMarker() {
    if (!this._map) return;
    const center = this._getConfiguredCenter();
    const color = this._sanitizeColor(this.config?.marker_color, "#d32f2f");
    const key = center
      ? `${center.id}|${center.lat}|${center.lon}|${color}`
      : `none|${color}`;
    if (key === this._lastCenterKey) return;
    this._lastCenterKey = key;

    if (this._centerMarker) {
      this._map.removeLayer(this._centerMarker);
      this._centerMarker = null;
    }
    if (!center) return;

    this._centerMarker = L.marker([center.lat, center.lon], {
      icon: this._makeMarkerIcon(color),
    }).addTo(this._map);
  }

  _updateSecondaryMarkers() {
    if (!this._map) return;
    this._secondaryMarkers.forEach((m) => this._map.removeLayer(m));
    this._secondaryMarkers = [];
    const list = Array.isArray(this.config?.secondary_markers)
      ? this.config.secondary_markers
      : [];
    for (const entry of list) {
      if (!entry || typeof entry !== "object") continue;
      const entityId = entry.entity;
      if (!entityId || typeof entityId !== "string") continue;
      const pos = this._getEntityLatLon(entityId);
      if (!pos) continue;
      const color = this._sanitizeColor(entry.color, "#03a9f4");
      const marker = L.marker([pos.lat, pos.lon], {
        icon: this._makeMarkerIcon(color),
      }).addTo(this._map);
      this._secondaryMarkers.push(marker);
    }
  }

  _updateStationMarkers() {
    // Station dots removed: they had no weather data attached and were
    // confusing users who expected a temperature / icon on each dot.
    // The per-location sensors still expose the nearest-station readings.
    if (!this._map) return;
    this._stationMarkers.forEach((m) => this._map.removeLayer(m));
    this._stationMarkers = [];
  }

  _loadLeafletCSS() {
    // No-op: Leaflet's stylesheet cannot cross the Shadow DOM boundary.
    // The critical rules are duplicated in `static styles` instead.
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
        this._updateSecondaryMarkers();
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
        this._updateSecondaryMarkers();
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
    this._secondaryMarkers = [];
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

    return html`
      <div id="map"></div>

      ${this._showingNoData
        ? html`
            <div
              style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:2000;background:rgba(20,20,20,0.95);color:#fff;padding:20px 26px;border-radius:12px;box-shadow:0 4px 16px rgba(0,0,0,0.5);text-align:center;max-width:90%;min-width:240px;pointer-events:auto;font-family:var(--paper-font-body1_-_font-family,Roboto,sans-serif);border:1px solid rgba(255,255,255,0.08);"
            >
              <ha-icon
                icon="mdi:radar"
                style="font-size:36px;color:#03a9f4;--mdc-icon-size:36px;"
              ></ha-icon>
              <div style="font-size:16px;font-weight:600;margin-top:10px;">
                ${sensorAttrs.frame_error ? "Radar fetch failed" : "Loading radar frames…"}
              </div>
              <div style="font-size:12px;margin-top:6px;opacity:0.9;line-height:1.4;">
                ${sensorAttrs.frame_error
                  ? sensorAttrs.frame_error
                  : "Prefetching DWD composite PNGs in the background. This usually takes < 60 s on first refresh."}
              </div>
              <div style="font-size:10px;margin-top:10px;opacity:0.6;">
                card v${CARD_VERSION} · last update ${sensorAttrs.last_update || "—"}
              </div>
            </div>
          `
        : nothing}

      <div class="controls">
        <div
          style="position:absolute;top:8px;right:8px;display:flex;flex-direction:column;gap:0;pointer-events:auto;align-items:stretch;background:rgba(20,20,20,0.92);border-radius:10px;box-shadow:0 2px 6px rgba(0,0,0,0.5);overflow:hidden;border:1px solid rgba(255,255,255,0.08);min-width:38px;"
        >
          <button
            style="background:transparent;border:none;cursor:pointer;padding:8px 10px;color:#fff;font-size:18px;display:flex;align-items:center;justify-content:center;line-height:1;"
            @click=${this._recenter}
            title="Recenter to home"
          >
            <ha-icon icon="mdi:crosshairs-gps"></ha-icon>
          </button>
          <button
            style="background:transparent;border:none;cursor:pointer;padding:7px 12px;color:#fff;font-size:16px;font-weight:700;line-height:1;border-top:1px solid rgba(255,255,255,0.08);"
            @click=${() => this._map?.zoomIn()}
            title="Zoom in"
          >
            +
          </button>
          <button
            style="background:transparent;border:none;cursor:pointer;padding:7px 12px;color:#fff;font-size:16px;font-weight:700;line-height:1;border-top:1px solid rgba(255,255,255,0.08);"
            @click=${() => this._map?.zoomOut()}
            title="Zoom out"
          >
            −
          </button>
        </div>

        <div
          style="position:absolute;bottom:16px;left:50%;transform:translateX(-50%);display:flex;align-items:center;gap:6px;background:rgba(20,20,20,0.92);padding:6px 14px;border-radius:24px;box-shadow:0 2px 8px rgba(0,0,0,0.5);font-size:13px;pointer-events:auto;border:1px solid rgba(255,255,255,0.08);"
        >
          <button
            style="background:none;border:none;cursor:pointer;padding:4px 6px;border-radius:4px;display:flex;align-items:center;color:#fff;font-size:16px;"
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
            style="width:100px;height:4px;-webkit-appearance:none;background:rgba(255,255,255,0.25);border-radius:2px;outline:none;cursor:pointer;"
          />

          <span
            style="font-size:12px;font-weight:600;color:#fff;min-width:72px;text-align:center;font-variant-numeric:tabular-nums;"
            >${this._timeLabel}</span
          >
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
    _newEntity: { state: true },
  };

  constructor() {
    super();
    this._newEntity = "";
  }

  static styles = css`
    :host {
      display: block;
      font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
    }
    .section {
      margin-top: 16px;
      padding-top: 12px;
      border-top: 1px solid var(--divider-color, #e0e0e0);
    }
    .section-title {
      font-size: 13px;
      font-weight: 600;
      margin: 0 0 4px 0;
    }
    .section-hint {
      font-size: 11px;
      opacity: 0.7;
      margin: 0 0 10px 0;
      line-height: 1.4;
    }
    .add-row {
      display: flex;
      gap: 8px;
      align-items: center;
    }
    .add-row ha-entity-picker {
      flex: 1 1 auto;
    }
    .add-btn {
      flex: 0 0 auto;
      background: var(--primary-color, #03a9f4);
      color: #fff;
      border: none;
      border-radius: 4px;
      padding: 8px 14px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
    }
    .add-btn:disabled {
      opacity: 0.4;
      cursor: not-allowed;
    }
    .marker-list {
      list-style: none;
      padding: 0;
      margin: 10px 0 0 0;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .marker-row {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 6px 8px;
      background: rgba(0, 0, 0, 0.04);
      border-radius: 4px;
    }
    .marker-name {
      flex: 1 1 auto;
      font-size: 13px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .marker-color {
      flex: 0 0 auto;
      width: 36px;
      height: 28px;
      padding: 0;
      border: 1px solid var(--divider-color, #ccc);
      border-radius: 4px;
      background: transparent;
      cursor: pointer;
    }
    .remove-btn {
      flex: 0 0 auto;
      background: transparent;
      color: var(--error-color, #d32f2f);
      border: 1px solid var(--error-color, #d32f2f);
      border-radius: 4px;
      padding: 4px 10px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
    }
    .empty {
      font-size: 12px;
      opacity: 0.6;
      font-style: italic;
      padding: 4px 0;
    }
  `;

  setConfig(config) {
    this.config = { ...RainradarCard.getStubConfig(), ...config };
    if (!Array.isArray(this.config.secondary_markers)) {
      this.config = { ...this.config, secondary_markers: [] };
    }
  }

  _emit() {
    this.dispatchEvent(
      new CustomEvent("config-changed", {
        detail: { config: this.config },
        bubbles: true,
        composed: true,
      })
    );
  }

  _handleSimpleChange(e) {
    this.config = { ...this.config, ...e.detail.value };
    this._emit();
  }

  _addSelectedMarker() {
    if (!this._newEntity) return;
    const next = [
      ...(this.config.secondary_markers || []),
      { entity: this._newEntity, color: "#03a9f4" },
    ];
    this.config = { ...this.config, secondary_markers: next };
    this._newEntity = "";
    this._emit();
  }

  _removeMarker(idx) {
    const next = [...(this.config.secondary_markers || [])];
    next.splice(idx, 1);
    this.config = { ...this.config, secondary_markers: next };
    this._emit();
  }

  _updateMarkerColor(idx, color) {
    const next = [...(this.config.secondary_markers || [])];
    next[idx] = { ...next[idx], color };
    this.config = { ...this.config, secondary_markers: next };
    this._emit();
  }

  _friendlyName(entityId) {
    if (!entityId) return entityId;
    const state = this.hass?.states?.[entityId];
    return state?.attributes?.friendly_name || state?.name || entityId;
  }

  render() {
    if (!this.hass || !this.config) return nothing;
    const secondaryMarkers = this.config.secondary_markers || [];

    return html`
      <ha-form
        .hass=${this.hass}
        .data=${{
          center_entity: this.config.center_entity || "",
          marker_color: this.config.marker_color || "#d32f2f",
        }}
        .schema=${[
          {
            name: "center_entity",
            label: "Center map on entity",
            selector: { entity: { domain: ["zone", "device_tracker"] } },
          },
          {
            name: "marker_color",
            label: "Center marker color",
            selector: { color: {} },
          },
        ]}
        .computeLabel=${(s) => s.label}
        @value-changed=${this._handleSimpleChange}
      ></ha-form>

      <div class="section">
        <p class="section-title">Secondary markers</p>
        <p class="section-hint">
          Add additional entities (zones or device trackers) that should
          appear as markers on the map without changing the map's centre.
          Each marker can have its own color.
        </p>
        <div class="add-row">
          <ha-entity-picker
            .hass=${this.hass}
            .value=${this._newEntity}
            .includeDomains=${["zone", "device_tracker"]}
            @value-changed=${(e) => {
              this._newEntity = e.detail.value || "";
            }}
          ></ha-entity-picker>
          <button
            class="add-btn"
            ?disabled=${!this._newEntity}
            @click=${this._addSelectedMarker}
          >
            + Add
          </button>
        </div>

        ${secondaryMarkers.length === 0
          ? html`<p class="empty">No secondary markers yet.</p>`
          : html`
              <ul class="marker-list">
                ${secondaryMarkers.map(
                  (m, idx) => html`
                    <li class="marker-row">
                      <span class="marker-name" title=${m.entity}
                        >${this._friendlyName(m.entity)}</span
                      >
                      <input
                        class="marker-color"
                        type="color"
                        .value=${m.color || "#03a9f4"}
                        @input=${(e) =>
                          this._updateMarkerColor(idx, e.target.value)}
                        title="Marker color"
                      />
                      <button
                        class="remove-btn"
                        @click=${() => this._removeMarker(idx)}
                        title="Remove marker"
                      >
                        Remove
                      </button>
                    </li>
                  `
                )}
              </ul>
            `}
      </div>
    `;
  }
}

try {
  customElements.define("rainradar-card-editor", RainradarCardEditor);
} catch (e) {}

// Full DWD Niederschlagsradar color ramp (from GetLegendGraphic on the
// WMS at https://maps.dwd.de/geoserver/dwd/ows). The map card no longer
// ships an inline legend (it was too sparse — 4 bands, and the user
// wants the full breakdown). This standalone card is added via the
// Lovelace card picker and shows every band, in order of intensity, with
// its mm/h range.
const DWD_RADAR_BANDS = [
  { color: "#00e8e8", label: "trace",         range: "0.1 – 0.2" },
  { color: "#00d070", label: "very light",    range: "0.2 – 0.4" },
  { color: "#00d000", label: "light",         range: "0.4 – 1.0" },
  { color: "#00b800", label: "light–moderate", range: "1.0 – 2.0" },
  { color: "#b4e600", label: "moderate",      range: "2.0 – 3.0" },
  { color: "#f0e800", label: "moderate",      range: "3.0 – 5.0" },
  { color: "#ffc800", label: "moderate–heavy", range: "5.0 – 7.5" },
  { color: "#ff9600", label: "heavy",         range: "7.5 – 10" },
  { color: "#ff6400", label: "heavy",         range: "10 – 15" },
  { color: "#ff0000", label: "very heavy",    range: "15 – 30" },
  { color: "#c80000", label: "very heavy",    range: "30 – 45" },
  { color: "#c80064", label: "extreme",       range: "45 – 75" },
  { color: "#c800c8", label: "extreme",       range: "75 – 100" },
  { color: "#9600c8", label: "extreme",       range: "100 – 150" },
  { color: "#0000c8", label: "violent",       range: "≥ 150" },
];

class RainradarLegendCard extends LitElement {
  static properties = {
    hass: { type: Object },
    config: { type: Object },
    _isPreview: { state: true },
  };

  constructor() {
    super();
    this._isPreview = false;
  }

  static styles = css`
    :host {
      display: block;
      box-sizing: border-box;
      font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
    }
    ha-card {
      padding: 12px 14px;
      background: rgba(20, 20, 20, 0.92);
      color: #fff;
      border-radius: var(--ha-card-border-radius, 12px);
      box-shadow: 0 2px 6px rgba(0,0,0,0.5);
      border: 1px solid rgba(255,255,255,0.08);
      display: block;
    }
    .title {
      font-size: 13px;
      font-weight: 700;
      margin-bottom: 2px;
      letter-spacing: 0.2px;
    }
    .subtitle {
      font-size: 11px;
      opacity: 0.7;
      margin-bottom: 10px;
    }
    .band {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 12px;
      padding: 3px 0;
      line-height: 1.3;
    }
    .swatch {
      width: 18px;
      height: 18px;
      border-radius: 3px;
      flex: 0 0 18px;
      border: 1px solid rgba(0,0,0,0.35);
    }
    .label {
      flex: 1 1 auto;
    }
    .range {
      opacity: 0.75;
      font-size: 11px;
      font-variant-numeric: tabular-nums;
      text-align: right;
      min-width: 64px;
    }
    .unit {
      opacity: 0.55;
      font-size: 10px;
      margin-left: 2px;
    }
    .footer {
      font-size: 10px;
      opacity: 0.55;
      margin-top: 8px;
    }
  `;

  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this.config = config || {};
  }

  getCardSize() {
    return 3;
  }

  connectedCallback() {
    super.connectedCallback();
    // Same picker-preview guard as the map card: no async work, no
    // ResizeObserver, no ha-card resize listeners until we are on a
    // real dashboard. The picker tears the preview down by checking
    // shadowRoot / parentElement chain; any microtask scheduled here
    // can keep the spinner spinning past the teardown.
    this._isPreview = this._isInPreviewContext();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this._isPreview = false;
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

  render() {
    if (this._isPreview) {
      return html`<div
        style="display:flex;align-items:center;justify-content:center;width:100%;height:100%;min-height:200px;color:var(--secondary-text-color,#666);font-size:14px;"
      >Rainradar Legend</div>`;
    }
    return html`
      <ha-card>
        <div class="title">DWD precipitation <span class="unit">mm/h</span></div>
        <div class="subtitle">Niederschlagsradar · full color ramp</div>
        ${DWD_RADAR_BANDS.map(
          (b) => html`
            <div class="band">
              <span class="swatch" style="background:${b.color};"></span>
              <span class="label">${b.label}</span>
              <span class="range">${b.range}</span>
            </div>
          `
        )}
        <div class="footer">rainradar v${CARD_VERSION} · place next to the map card</div>
      </ha-card>
    `;
  }
}

try {
  customElements.define("rainradar-legend-card", RainradarLegendCard);
} catch (e) {}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "rainradar-card",
  name: "Rainradar",
  description: "DWD rain radar map with animation for Germany",
  preview: true,
});
window.customCards.push({
  type: "rainradar-legend-card",
  name: "Rainradar Legend",
  description: "Full DWD Niederschlagsradar color legend (15 bands, mm/h)",
  preview: true,
});

export { RainradarCard, RainradarLegendCard };
