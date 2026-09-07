"""Tests for card/integration version sync and shared geometry helpers."""

from __future__ import annotations

from pathlib import Path
import re

from custom_components.rainradar.const import (
    INTEGRATION_VERSION,
    frames_cache_dir,
    frames_url_prefix,
    latlon_to_radar_pixel,
    mercator_bbox,
    pixel_intensity,
    safe_frame_filename,
)

CARD_SRC = (
    Path(__file__).parent.parent
    / "custom_components/rainradar/frontend/src/rainradar-card.js"
)
MANIFEST = (
    Path(__file__).parent.parent / "custom_components/rainradar/manifest.json"
)


def test_card_version_matches_integration_version():
    """Cache-bust contract: CARD_VERSION must track INTEGRATION_VERSION."""
    src = CARD_SRC.read_text(encoding="utf-8")
    match = re.search(r'const CARD_VERSION = "([^"]+)"', src)
    assert match, "CARD_VERSION not found in card source"
    assert match.group(1) == INTEGRATION_VERSION


def test_manifest_version_matches_integration_version():
    import json

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["version"] == INTEGRATION_VERSION


def test_dist_bundle_contains_current_version():
    dist = CARD_SRC.parent.parent / "dist" / "rainradar-card.js"
    assert dist.is_file(), "dist bundle missing — run npm run build"
    assert INTEGRATION_VERSION in dist.read_text(encoding="utf-8")


def test_frames_url_prefix_no_trailing_slash():
    """aiohttp PrefixResource asserts against trailing slashes at registration."""
    prefix = frames_url_prefix("some_entry")
    assert not prefix.endswith("/")
    assert prefix == "/rainradar/frames/some_entry"


def test_frames_cache_dir_not_under_storage():
    d = frames_cache_dir("/config", "some_entry")
    assert ".storage" not in d.parts
    assert d == Path("/config/rainradar/frames/some_entry")


def test_safe_frame_filename():
    assert (
        safe_frame_filename("2026-09-06T07:00:00Z") == "2026-09-06T07-00-00Z.png"
    )


def test_mercator_bbox_projected_meters():
    parts = mercator_bbox(-2.0, 42.0, 22.0, 60.0).split(",")
    x_min, y_min, x_max, y_max = (float(v) for v in parts)
    # Web Mercator x range for these lon values
    assert x_min < 0 < x_max
    assert 4_000_000 < y_min < 6_000_000
    assert 8_000_000 < y_max < 9_000_000


def test_latlon_to_radar_pixel_bounds():
    col, row = latlon_to_radar_pixel(52.47, 9.68)
    assert 0 <= col < 1200
    assert 0 <= row < 900


def test_pixel_intensity_known_colors():
    assert pixel_intensity(255, 0, 0) == 30.0  # red band
    assert pixel_intensity(0, 0, 255) == 150.0  # blue band
