"""Tests for DWD warning helpers (point-in-polygon, level, headline)."""

from __future__ import annotations

from custom_components.rainradar.warnings import (
    _point_in_polygon,
    resolve_warnings_for_coordinates,
    warning_headline_from_warnings,
    warning_level_from_warnings,
)

# Flat [lat1, lon1, lat2, lon2, ...] ring around Hannover.
HANNOVER_POLYGON = [52.3, 9.5, 52.3, 9.9, 52.6, 9.9, 52.6, 9.5]


def test_point_in_polygon_inside():
    assert _point_in_polygon(9.7, 52.45, HANNOVER_POLYGON) is True


def test_point_in_polygon_outside():
    assert _point_in_polygon(10.5, 52.45, HANNOVER_POLYGON) is False
    assert _point_in_polygon(9.7, 51.0, HANNOVER_POLYGON) is False


def test_point_in_polygon_degenerate():
    assert _point_in_polygon(9.7, 52.45, []) is False
    assert _point_in_polygon(9.7, 52.45, [52.3, 9.5]) is False


def _warning(warn_id: str, level: int, polygon: list[float], headline: str):
    return {
        "warnId": warn_id,
        "level": level,
        "headLine": headline,
        "regions": [{"polygon": polygon}],
    }


def test_resolve_warnings_matching():
    warnings = [
        _warning("w1", 2, HANNOVER_POLYGON, "Sturm"),
        _warning("w2", 3, [48.0, 11.0, 48.2, 11.0, 48.2, 11.4, 48.0, 11.4], "Unwetter"),
    ]
    matching = resolve_warnings_for_coordinates(warnings, 52.45, 9.7)
    assert len(matching) == 1
    assert matching[0]["warnId"] == "w1"


def test_resolve_warnings_dedupes_by_id():
    warnings = [
        _warning("w1", 2, HANNOVER_POLYGON, "Sturm"),
        _warning("w1", 2, HANNOVER_POLYGON, "Sturm (dup)"),
    ]
    matching = resolve_warnings_for_coordinates(warnings, 52.45, 9.7)
    assert len(matching) == 1


def test_resolve_warnings_none_or_empty():
    assert resolve_warnings_for_coordinates(None, 52.0, 9.0) == []
    assert resolve_warnings_for_coordinates([], 52.0, 9.0) == []


def test_warning_level_highest_wins():
    warnings = [
        _warning("a", 1, HANNOVER_POLYGON, "minor"),
        _warning("b", 4, HANNOVER_POLYGON, "extreme"),
        _warning("c", 2, HANNOVER_POLYGON, "moderate"),
    ]
    assert warning_level_from_warnings(warnings) == 4
    assert warning_level_from_warnings([]) == 0
    assert warning_level_from_warnings(None) == 0


def test_warning_headline_of_highest():
    warnings = [
        _warning("a", 1, HANNOVER_POLYGON, "minor"),
        _warning("b", 4, HANNOVER_POLYGON, "extreme"),
    ]
    assert warning_headline_from_warnings(warnings) == "extreme"
    assert warning_headline_from_warnings([]) is None
