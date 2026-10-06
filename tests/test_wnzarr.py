"""Tests for the dependency-free Zarr v3 reader.

Fixtures are written by the real ``zarr`` library (plain and sharded, zstd and
gzip), then read back through ``wnzarr`` and compared against the numpy array
that was written. That exercises the codec pipelines and the sharding index
layout without needing Google credentials.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from custom_components.rainradar import wnzarr


@pytest.fixture(autouse=True)
def verify_cleanup():
    """Skip the plugin's lingering-thread/timer audit for this module.

    Writing the fixtures with the real ``zarr`` library spawns an executor
    thread on the test loop. That is harness noise from the fixture writer, not
    integration behaviour, so there is nothing to assert on here.
    """
    yield


async def _write(
    root: Path, name: str, shape, chunks, *, shards=None, compressor="zstd", data=None
):
    """Write a zarr v3 array + 1-D coordinates with the real library."""
    import zarr
    from zarr.codecs import GzipCodec, ZstdCodec

    store = zarr.storage.LocalStore(str(root / name))
    group = zarr.open_group(store=store, mode="w", zarr_format=3)
    comp = ZstdCodec(level=1) if compressor == "zstd" else GzipCodec(level=1)
    arr = group.create_array(
        name="v",
        shape=shape,
        dtype="float32",
        chunks=chunks,
        shards=shards,
        compressors=[comp],
        dimension_names=["lead_time", "latitude", "longitude"],
        fill_value=np.nan,
    )
    if data is None:
        rng = np.random.default_rng(1234)
        data = (rng.random(shape, dtype=np.float32) * 100.0 + 250.0).astype("float32")
    arr[:] = data

    coords = {
        "latitude": np.linspace(-88.0, 88.0, shape[1]).astype("float32"),
        "longitude": np.linspace(0.0, 359.0, shape[2]).astype("float32"),
        "lead_time": np.arange(shape[0], dtype="float64"),
    }
    for coord_name, values in coords.items():
        c = group.create_array(
            name=coord_name,
            shape=values.shape,
            dtype=values.dtype,
            fill_value=np.nan,
            dimension_names=[coord_name],
        )
        c[:] = values

    # Close the store so its executor thread does not outlive the test.
    store.close()
    return data, coords


async def test_transport_sends_auth_and_range_headers(tmp_path: Path) -> None:
    """URL building, the Bearer header and Range headers must actually be sent.

    Every other test in this module overrides the four ``_get_*`` methods, so
    without this one the real ``aiohttp`` calls are never executed — a typo in a
    URL or a dropped header would go unnoticed until it hit the live bucket.
    Served through HA's request mocker, so no sockets are involved.
    """
    root = tmp_path / "http.zarr"
    data, _ = await _write(
        root, "http.zarr", (4, 40, 40), (1, 10, 10), shards=(2, 20, 20)
    )
    base = "https://bucket.example.com/prefix/predictions.zarr"
    session = _RecordingSession(root, base)

    reader = wnzarr.RemoteZarrV3(session, base, "ya29.test-token")  # type: ignore[arg-type]
    lat = await reader.coordinate("latitude")
    lon = await reader.coordinate("longitude")
    series = await wnzarr.read_series(
        reader, "v",
        wnzarr.nearest_index(lat, 0.0),
        wnzarr.nearest_index(lon, 100.0),
        0, 4,
    )
    i_lat = wnzarr.nearest_index(lat, 0.0)
    i_lon = wnzarr.nearest_index(lon, 100.0)
    np.testing.assert_allclose(series, data[:, i_lat, i_lon], rtol=1e-6)

    assert session.calls, "no requests were made"
    for url, headers in session.calls:
        assert headers.get("Authorization") == "Bearer ya29.test-token"
        assert url.startswith(f"{base}/")
    # Shard indexes are read from the end via a suffix Range request.
    ranges = [h.get("Range") for _, h in session.calls]
    assert any(r and r.startswith("bytes=-") for r in ranges), ranges


class _RecordingSession:
    """Minimal aiohttp-like session that records requests and serves a fixture.

    Stands in for ``ClientSession`` so the reader's own URL building and header
    construction run for real, without binding a socket (the HA test plugin
    blocks that). Implements the same ``get()`` context-manager protocol the
    reader uses.
    """

    def __init__(self, root: Path, base: str) -> None:
        self._root = root
        self._base = base
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, *, headers: dict | None = None):
        """Record the request and return a response context manager."""
        self.calls.append((url, dict(headers or {})))
        rel = url.removeprefix(self._base).lstrip("/")
        # The fixture store lives in a subdirectory of root.
        target = self._root / "http.zarr" / rel
        if not target.is_file():
            return _FakeResponse(404, b"")
        body = target.read_bytes()
        spec = (headers or {}).get("Range")
        if not spec:
            return _FakeResponse(200, body)
        size = len(body)
        spec = spec.removeprefix("bytes=")
        if spec.startswith("-"):
            # Suffix range: the last N bytes.
            start, end = max(0, size - int(spec[1:])), size - 1
        else:
            lo, _, hi = spec.partition("-")
            start = int(lo)
            end = min(int(hi), size - 1) if hi else size - 1
        return _FakeResponse(206, body[start : end + 1])


class _FakeResponse:
    """Async context manager mimicking an ``aiohttp`` response."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def read(self) -> bytes:
        """Body bytes."""
        return self._body

    async def json(self, content_type=None) -> dict:
        """Parsed JSON body."""
        return json.loads(self._body)


class _LocalReader(wnzarr.RemoteZarrV3):
    """Reader backed by a local directory, exercising the same code paths."""

    def __init__(self, root: str) -> None:
        self._root = Path(root)
        self._headers: dict = {}
        self._meta_cache: dict = {}

    def _url(self, path: str) -> str:
        return str(self._root / path.lstrip("/"))

    async def _get_json(self, path: str) -> dict:
        import json

        target = self._root / path
        if not target.is_file():
            # Mirror the real reader: a missing zarr.json is an HTTP 404.
            raise wnzarr.MissingArrayError(f"{path}: HTTP 404")
        return json.loads(target.read_text())

    async def _get_whole(self, path: str) -> bytes:
        target = self._root / path.lstrip("/")
        return target.read_bytes() if target.is_file() else b""

    async def _get_range(self, path: str, start: int, length: int) -> bytes:
        target = self._root / path.lstrip("/")
        if not target.is_file():
            return b""
        with target.open("rb") as fh:
            fh.seek(start)
            return fh.read(length)

    async def _get_tail(self, path: str, length: int) -> bytes:
        target = self._root / path.lstrip("/")
        if not target.is_file():
            return b""
        with target.open("rb") as fh:
            fh.seek(max(0, target.stat().st_size - length))
            return fh.read()


@pytest.mark.parametrize(
    ("selection", "expected_shape"),
    [
        # int selections yield a length-1 axis, matching Zarr's own behaviour.
        ({1: 1}, (5, 1, 4)),
        ({0: 2, 1: 0, 2: 3}, (1, 1, 1)),
        ({0: slice(1, 4), 1: 2, 2: 1}, (3, 1, 1)),
        # Negative indices count from the end.
        ({1: -1, 0: slice(0, 2), 2: slice(0, 4)}, (2, 1, 4)),
        # A slice past the end is clamped, not an error.
        ({0: slice(3, 99), 1: slice(0, 3), 2: slice(0, 4)}, (2, 3, 4)),
        # An empty selection returns an empty array.
        ({0: slice(2, 2), 1: slice(0, 3), 2: slice(0, 4)}, (0, 3, 4)),
    ],
)
async def test_read_selections(tmp_path: Path, selection, expected_shape) -> None:
    """Every selection form must match numpy's equivalent indexing."""
    shape = (5, 3, 4)
    data = np.arange(np.prod(shape), dtype="float32").reshape(shape)
    await _write(tmp_path, "sel.zarr", shape, (2, 3, 4), data=data)
    reader = _LocalReader(str(tmp_path / "sel.zarr"))
    meta = await reader.array_meta("v")

    got = await reader.read(meta, selection)

    expected = data
    for dim, item in selection.items():
        if isinstance(item, slice):
            expected = np.take(
                expected, range(*item.indices(shape[dim])), axis=dim
            )
        else:
            expected = np.take(expected, [item % shape[dim]], axis=dim)

    assert got.shape == expected_shape
    np.testing.assert_array_equal(got, expected)


async def test_plain_zstd_chunks(tmp_path: Path) -> None:
    data, coords = await _write(tmp_path, "plain.zarr", (5, 40, 50), (1, 20, 25))
    reader = _LocalReader(str(tmp_path / "plain.zarr"))

    lat = await reader.coordinate("latitude")
    lon = await reader.coordinate("longitude")
    np.testing.assert_allclose(lat, coords["latitude"])
    np.testing.assert_allclose(lon, coords["longitude"])

    i_lat = wnzarr.nearest_index(lat, 0.0)
    i_lon = wnzarr.nearest_index(lon, 100.0)
    series = await wnzarr.read_series(reader, "v", i_lat, i_lon, 0, 5)
    np.testing.assert_allclose(series, data[:, i_lat, i_lon], rtol=1e-6)


async def test_sharded_chunks_with_crc32c_index(tmp_path: Path) -> None:
    """sharding_indexed with the recommended trailing crc32c index codec."""
    data, _ = await _write(
        tmp_path, "sharded.zarr", (4, 40, 40), (1, 10, 10), shards=(2, 20, 20)
    )
    reader = _LocalReader(str(tmp_path / "sharded.zarr"))
    meta = await reader.array_meta("v")
    assert meta.sharding is not None, "fixture must actually be sharded"

    lat = await reader.coordinate("latitude")
    lon = await reader.coordinate("longitude")
    i_lat = wnzarr.nearest_index(lat, -12.0)
    i_lon = wnzarr.nearest_index(lon, 250.0)
    series = await wnzarr.read_series(reader, "v", i_lat, i_lon, 0, 4)
    np.testing.assert_allclose(series, data[:, i_lat, i_lon], rtol=1e-6)


async def test_many_inner_chunks_per_shard(tmp_path: Path) -> None:
    """Inner chunks far smaller than the shard: the index must be honoured."""
    data, _ = await _write(
        tmp_path, "blocks.zarr", (3, 40, 40), (1, 4, 4), shards=(1, 20, 20)
    )
    reader = _LocalReader(str(tmp_path / "blocks.zarr"))
    lat = await reader.coordinate("latitude")
    lon = await reader.coordinate("longitude")
    for target_lat, target_lon in ((0.0, 0.0), (-44.0, 359.0), (12.5, 180.0)):
        i_lat = wnzarr.nearest_index(lat, target_lat)
        i_lon = wnzarr.nearest_index(lon, target_lon)
        series = await wnzarr.read_series(reader, "v", i_lat, i_lon, 0, 3)
        np.testing.assert_allclose(
            series, data[:, i_lat, i_lon], rtol=1e-6, err_msg=f"{target_lat},{target_lon}"
        )


async def test_plane_read_for_the_overlay(tmp_path: Path) -> None:
    """A full lat/lon slab (what the global overlay renders) must be exact."""
    data, _ = await _write(tmp_path, "plane.zarr", (3, 20, 30), (1, 10, 10))
    reader = _LocalReader(str(tmp_path / "plane.zarr"))
    plane = await wnzarr.read_plane(reader, "v", lead_index=1)
    np.testing.assert_allclose(plane, data[1], rtol=1e-6)


async def test_plane_read_absent_array(tmp_path: Path) -> None:
    await _write(tmp_path, "p2.zarr", (2, 4, 4), (1, 4, 4))
    reader = _LocalReader(str(tmp_path / "p2.zarr"))
    assert await wnzarr.read_plane(reader, "nope") is None


async def test_gzip_compressor(tmp_path: Path) -> None:
    data, _ = await _write(
        tmp_path, "gz.zarr", (2, 20, 20), (1, 10, 10), compressor="gzip"
    )
    reader = _LocalReader(str(tmp_path / "gz.zarr"))
    lat = await reader.coordinate("latitude")
    lon = await reader.coordinate("longitude")
    i_lat = wnzarr.nearest_index(lat, 5.0)
    i_lon = wnzarr.nearest_index(lon, 90.0)
    series = await wnzarr.read_series(reader, "v", i_lat, i_lon, 0, 2)
    np.testing.assert_allclose(series, data[:, i_lat, i_lon], rtol=1e-6)


async def test_absent_array_returns_none(tmp_path: Path) -> None:
    await _write(tmp_path, "some.zarr", (2, 4, 4), (1, 4, 4))
    reader = _LocalReader(str(tmp_path / "some.zarr"))
    assert await wnzarr.read_series(reader, "does_not_exist", 0, 0, 0, 1) is None


async def test_edge_chunks_when_chunks_do_not_divide_the_shape(tmp_path: Path) -> None:
    """The trailing partial chunk must be padded with fill, not shifted."""
    shape = (3, 25, 35)
    data, _ = await _write(tmp_path, "ragged.zarr", shape, (1, 10, 10))
    reader = _LocalReader(str(tmp_path / "ragged.zarr"))
    lat = await reader.coordinate("latitude")
    lon = await reader.coordinate("longitude")
    # Last row/column live in the clipped edge chunk.
    series = await wnzarr.read_series(reader, "v", shape[1] - 1, shape[2] - 1, 0, 3)
    np.testing.assert_allclose(series, data[:, -1, -1], rtol=1e-6)
    assert series.shape == (3,)
    assert wnzarr.nearest_index(lat, 88.0) == shape[1] - 1
    assert wnzarr.nearest_index(lon, 359.0) == shape[2] - 1


async def test_edge_chunks_sharded(tmp_path: Path) -> None:
    """Shards that overhang the array edge must still decode."""
    shape = (2, 25, 25)
    data, _ = await _write(
        tmp_path, "ragged_shard.zarr", shape, (1, 5, 5), shards=(1, 20, 20)
    )
    reader = _LocalReader(str(tmp_path / "ragged_shard.zarr"))
    series = await wnzarr.read_series(reader, "v", 24, 24, 0, 2)
    np.testing.assert_allclose(series, data[:, 24, 24], rtol=1e-6)


async def test_lead_time_range_is_clipped_to_the_array(tmp_path: Path) -> None:
    """Asking for more lead steps than exist must not raise or over-read."""
    data, _ = await _write(tmp_path, "short.zarr", (3, 10, 10), (1, 10, 10))
    reader = _LocalReader(str(tmp_path / "short.zarr"))
    series = await wnzarr.read_series(reader, "v", 5, 5, 0, 48)
    np.testing.assert_allclose(series, data[:, 5, 5], rtol=1e-6)
    assert series.shape == (3,)


def test_nearest_index_ties_and_edges() -> None:
    values = np.array([0.0, 10.0, 20.0])
    assert wnzarr.nearest_index(values, 19.0) == 2
    assert wnzarr.nearest_index(values, -100.0) == 0
    assert wnzarr.nearest_index(values, 1e9) == 2
    assert wnzarr.nearest_index(values, 4.0) == 0  # tie -> first


def test_unsupported_codec_names_itself() -> None:
    """An unknown codec must say which codec, not fail opaquely."""
    meta = wnzarr.parse_array_meta(
        "v",
        {
            "zarr_format": 3,
            "shape": [4, 4],
            "data_type": "float32",
            "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [2, 2]}},
            "chunk_key_encoding": {"name": "default"},
            "codecs": [{"name": "bytes"}, {"name": "lz4"}],
            "fill_value": "NaN",
        },
    )
    reader = wnzarr.RemoteZarrV3.__new__(wnzarr.RemoteZarrV3)
    with pytest.raises(wnzarr.WNZarrError, match="lz4"):
        reader._to_array(b"\x00" * 16, meta, (2, 2))


def test_unsupported_dtype_names_itself() -> None:
    with pytest.raises(wnzarr.WNZarrError, match="data_type"):
        wnzarr.parse_array_meta(
            "v",
            {
                "zarr_format": 3,
                "shape": [2],
                "data_type": "decimal128",
                "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [2]}},
                "chunk_key_encoding": {"name": "default"},
                "codecs": [{"name": "bytes"}],
                "fill_value": 0,
            },
        )


def test_rejects_zarr_v2() -> None:
    with pytest.raises(wnzarr.WNZarrError, match="format 3"):
        wnzarr.parse_array_meta(
            "v", {"zarr_format": 2, "shape": [2], "chunks": [2], "dtype": "<f4"}
        )


def test_dimension_name_fallbacks() -> None:
    """Pre-3.1 arrays carry _ARRAY_DIMENSIONS instead."""
    meta = wnzarr.parse_array_meta(
        "v",
        {
            "zarr_format": 3,
            "shape": [2, 3, 4],
            "data_type": "float32",
            "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [1, 3, 4]}},
            "chunk_key_encoding": {"name": "default"},
            "codecs": [{"name": "bytes"}],
            "fill_value": "NaN",
            "attributes": {"_ARRAY_DIMENSIONS": ["lead_time", "latitude", "longitude"]},
        },
    )
    assert meta.dim_index["latitude"] == 1
    assert meta.dim_index["longitude"] == 2


def _meta_with_dims(names):
    return wnzarr.ArrayMeta(
        path="v",
        shape=(2, 3, 4),
        chunk_shape=(1, 3, 4),
        dtype="f4",
        codecs=({"name": "bytes"},),
        dimension_names=names,
        fill_value="NaN",
        separator="/",
    )


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        # Canonical xarray names.
        (("lead_time", "latitude", "longitude"), (1, 2, 0)),
        # WN3-style short names.
        (("lead_time", "lat", "lon"), (1, 2, 0)),
        # Grid-ish names seen in some model output.
        (("time", "lat_grid", "lon_grid"), (1, 2, 0)),
        # Unnamed dims must fall back to position (lat/lon are the last two).
        ((None, None, None), (1, 2, 0)),
        # Transposed storage order must still resolve by name.
        (("latitude", "longitude", "lead_time"), (0, 1, 2)),
    ],
)
def test_grid_dims_resolution(names, expected) -> None:
    assert wnzarr.grid_dims(_meta_with_dims(names)) == expected


def test_grid_dims_needs_three_dimensions() -> None:
    two_d = wnzarr.ArrayMeta(
        path="v", shape=(3, 4), chunk_shape=(3, 4), dtype="f4",
        codecs=({"name": "bytes"},), dimension_names=("latitude", "longitude"),
        fill_value="NaN", separator="/",
    )
    assert wnzarr.grid_dims(two_d) is None
