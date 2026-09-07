"""Tests for the DWD station catalog parser and nearest-station lookup."""

from __future__ import annotations

import math

import pytest

from custom_components.rainradar.station_mapping import (
    _haversine,
    _is_active,
    _parse_station_line,
    find_nearest_station,
    find_nearest_stations,
)
from tests.conftest import make_station

STATION_LINE = (
    "00044 20070401 20260905             44     52.9336    8.2370 "
    "Großenkneten                             Niedersachsen"
)
CLOSED_STATION_LINE = (
    "00100 19700101 20000101            100     50.0000    8.0000 "
    "Alte Station                             Hessen"
)


def test_parse_station_line_active():
    st = _parse_station_line(STATION_LINE, {"temperature"})
    assert st is not None
    assert st.station_id == "00044"
    assert st.name == "Großenkneten"
    assert st.lat == pytest.approx(52.9336)
    assert st.lon == pytest.approx(8.2370)


def test_parse_station_line_closed_station_skipped():
    assert _parse_station_line(CLOSED_STATION_LINE, set()) is None


def test_parse_station_line_outside_bbox_skipped():
    line = (
        "00999 20070401 20260905             44     60.0000   20.0000 "
        "Far Away                                 Abroad"
    )
    assert _parse_station_line(line, set()) is None


def test_is_active_formats():
    assert _is_active("99991231") is True
    assert _is_active("garbage") is False


def test_haversine_known_distance():
    # Hamburg to Munich is ~590 km great-circle.
    d = _haversine(53.63, 10.00, 48.41, 11.50)
    assert 550 < d < 630


def test_find_nearest_station_single():
    stations = [make_station("1", 53.6, 10.0), make_station("2", 48.4, 11.5)]
    nearest = find_nearest_station(53.0, 10.1, stations)
    assert nearest.station_id == "1"
    assert nearest.distance_km > 0


def test_find_nearest_stations_returns_sorted_top_n():
    stations = [
        make_station("near", 52.5, 9.7),
        make_station("mid", 52.0, 9.6),
        make_station("far", 48.4, 11.5),
        make_station("farthest", 47.0, 11.0),
    ]
    result = find_nearest_stations(52.47, 9.68, stations, n=2)
    assert [s.station_id for s, _ in result] == ["near", "mid"]
    dists = [d for _, d in result]
    assert dists == sorted(dists)
    # Does not mutate station objects
    assert all(s.distance_km == 0.0 for s, _ in result)


def test_find_nearest_stations_empty_catalog():
    assert find_nearest_stations(52.0, 9.0, [], n=3) == []
    assert find_nearest_station(52.0, 9.0, []) is None


def test_haversine_symmetric():
    d1 = _haversine(52.0, 9.0, 48.0, 11.0)
    d2 = _haversine(48.0, 11.0, 52.0, 9.0)
    assert math.isclose(d1, d2)
