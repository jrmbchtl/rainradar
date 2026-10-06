"""Minimal Zarr v3 reader for the WeatherNext 3 statistics bucket.

Why this exists
---------------
WN3 data is Zarr v3 on Cloud Storage. The obvious way to read it is
``xarray`` + ``zarr`` + ``obstore``, but ``zarr`` depends on ``numcodecs``,
which publishes **no musllinux wheels** for CPython 3.11+ (verified against
PyPI: the only musllinux builds ever released are 0.10.0a2/a3, for cp310).
Home Assistant OS containers are musl-based and run CPython 3.14, so
``pip install zarr`` there falls back to a source build and fails — and when
HA cannot install an integration's requirements the *whole integration* stops
setting up, not just WN3.

So this reads the store directly over HTTPS with aiohttp. Supported:

- ``bytes`` (endian), ``transpose``, ``zstd``, ``gzip``/``zlib``
- ``sharding_indexed`` (index at start or end, ``crc32c`` optional)
- ``crc32c`` as an index codec (tolerated, not verified)
- ``blosc`` via ``numcodecs`` when it happens to be importable

Anything else raises :class:`WNZarrError` naming the codec, so an unsupported
store produces an actionable log line rather than silent garbage.

zstd needs no dependency: CPython 3.14 ships ``compression.zstd`` in the
standard library (PEP 784). ``zstandard`` is used as a fallback for older
runtimes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import math
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Chunk reads are independent; cap socket pressure.
_MAX_CONCURRENT_READS = 8

# Zarr marks a missing inner chunk with offset == length == 2**64 - 1.
_MAX_UINT_64 = 2**64 - 1

# v3 data_type -> numpy dtype string.
_DTYPES = {
    "float16": "f2",
    "float32": "f4",
    "float64": "f8",
    "int8": "i1",
    "int16": "i2",
    "int32": "i4",
    "int64": "i8",
    "uint8": "u1",
    "uint16": "u2",
    "uint32": "u4",
    "uint64": "u8",
    "complex64": "c8",
    "complex128": "c16",
}


class WNZarrError(Exception):
    """Raised when the store uses something this reader cannot decode."""


class MissingArrayError(WNZarrError):
    """Raised when an array's ``zarr.json`` does not exist.

    WN3 variable names are resolved by probing candidates, so absence is a
    normal control-flow signal rather than a failure. Callers that want to tell
    the two apart can catch this specifically.
    """


def codec_name(codec: Any) -> str:
    """Return a codec's name, tolerating the bare-string shorthand."""
    if isinstance(codec, str):
        return codec
    return str((codec or {}).get("name", ""))


def codec_config(codec: Any) -> dict[str, Any]:
    """Return a codec's configuration mapping."""
    if isinstance(codec, str):
        return {}
    config = (codec or {}).get("configuration") or {}
    return config if isinstance(config, dict) else {}


def _as_numpy_dtype(data_type: Any) -> str:
    """Map a v3 ``data_type`` (string or extension object) to a numpy dtype."""
    name = data_type if isinstance(data_type, str) else (data_type or {}).get("name", "")
    if str(name) in _DTYPES:
        return _DTYPES[str(name)]
    raise WNZarrError(f"unsupported Zarr data_type {name!r}")


def _zstd_decompress(payload: bytes) -> bytes:
    """Decompress a zstd frame using the stdlib (3.14+) or zstandard."""
    try:
        from compression import zstd  # Python 3.14+, PEP 784

        return zstd.decompress(payload)
    except ImportError:
        pass
    try:
        import zstandard
    except ImportError as err:
        raise WNZarrError(
            "zstd needs Python 3.14+ (stdlib compression.zstd) or 'zstandard'"
        ) from err
    return zstandard.ZstdDecompressor().decompress(payload)


def _gunzip(payload: bytes) -> bytes:
    """Decompress a gzip or zlib stream."""
    import gzip
    import zlib

    if payload[:2] == b"\x1f\x8b":
        return gzip.decompress(payload)
    try:
        return zlib.decompress(payload, 47)  # wbits=47 auto-detects
    except zlib.error:
        return zlib.decompress(payload)


def _blosc_decompress(payload: bytes) -> bytes:
    """Decompress a blosc frame via numcodecs, if importable."""
    try:
        import numcodecs.blosc
    except ImportError as err:
        raise WNZarrError(
            "blosc needs the 'numcodecs' package, which has no musllinux wheels "
            "for Python 3.11+ (this is what makes WN3 unavailable on HA OS)"
        ) from err
    return numcodecs.blosc.decompress(payload)


def _decompress_bytes(payload: bytes, codecs: list[Any]) -> bytes:
    """Run the bytes->bytes codecs in reverse order.

    ``bytes`` is the array->bytes serializer, so in the decode direction it is
    a no-op (the dtype is applied separately by :meth:`RemoteZarrV3._to_array`).
    ``crc32c`` is a check-only codec: it appends a 4-byte trailer that we drop
    without verifying, because the fetch length has to account for it.
    """
    for codec in reversed(codecs):
        name = codec_name(codec)
        if name == "bytes":
            continue
        if name == "crc32c":
            payload = payload[:-4]
            continue
        if name == "zstd":
            payload = _zstd_decompress(payload)
        elif name in ("gzip", "zlib"):
            payload = _gunzip(payload)
        elif name in ("blosc", "blosc2"):
            payload = _blosc_decompress(payload)
        else:
            raise WNZarrError(f"unsupported Zarr bytes codec {name!r}")
    return payload


@dataclass(frozen=True)
class ArrayMeta:
    """The subset of Zarr v3 array metadata this reader needs."""

    path: str
    shape: tuple[int, ...]
    chunk_shape: tuple[int, ...]
    dtype: str
    codecs: tuple[Any, ...]
    dimension_names: tuple[str | None, ...]
    fill_value: Any
    separator: str

    @property
    def ndim(self) -> int:
        """Number of dimensions."""
        return len(self.shape)

    @property
    def sharding(self) -> dict[str, Any] | None:
        """The sharding_indexed configuration, or None when unsharded."""
        for codec in self.codecs:
            if codec_name(codec) == "sharding_indexed":
                return codec_config(codec)
        return None

    @property
    def dim_index(self) -> dict[str, int]:
        """Map named dimensions to their position."""
        return {n: i for i, n in enumerate(self.dimension_names) if n}


def parse_array_meta(path: str, doc: dict[str, Any]) -> ArrayMeta:
    """Build :class:`ArrayMeta` from a v3 ``zarr.json`` document."""
    if doc.get("zarr_format") != 3:
        raise WNZarrError(f"{path}: expected Zarr format 3, got {doc.get('zarr_format')!r}")

    shape = tuple(int(x) for x in doc.get("shape") or ())
    grid_cfg = (doc.get("chunk_grid") or {}).get("configuration") or {}
    chunk_shape = tuple(int(x) for x in grid_cfg.get("chunk_shape") or ())
    if len(chunk_shape) != len(shape):
        raise WNZarrError(f"{path}: chunk_shape {chunk_shape} vs shape {shape}")

    key_enc = doc.get("chunk_key_encoding") or {}
    if codec_name(key_enc) not in ("default", "v2"):
        raise WNZarrError(f"{path}: unsupported chunk_key_encoding {codec_name(key_enc)!r}")
    separator = (key_enc.get("configuration") or {}).get("separator", "/")

    names = doc.get("dimension_names")
    if names is None:  # pre-3.1 xarray convention
        legacy = (doc.get("attributes") or {}).get("_ARRAY_DIMENSIONS")
        names = list(legacy) if legacy else [None] * len(shape)

    return ArrayMeta(
        path=path.strip("/"),
        shape=shape,
        chunk_shape=chunk_shape,
        dtype=_as_numpy_dtype(doc.get("data_type")),
        codecs=tuple(doc.get("codecs") or ()),
        dimension_names=tuple(names),
        fill_value=doc.get("fill_value"),
        separator=separator,
    )


def fill_value_for(dtype: str, raw: Any) -> Any:
    """Interpret a v3 ``fill_value`` for a numpy dtype."""
    import numpy as np

    kind = np.dtype(dtype).kind
    if raw is None:
        return np.nan if kind == "f" else 0
    if isinstance(raw, str):
        if raw.lower() == "nan":
            return np.nan
        try:
            return np.dtype(dtype).type(raw)
        except (TypeError, ValueError):
            return np.nan if kind == "f" else 0
    try:
        return np.dtype(dtype).type(raw)
    except (TypeError, ValueError):
        return np.nan if kind == "f" else 0


def clipped_chunk_shape(meta: ArrayMeta, chunk_index: tuple[int, ...]) -> tuple[int, ...]:
    """Shape of a chunk once clipped at the array edges (Zarr decodes it so)."""
    out = []
    for size, chunk, index in zip(meta.shape, meta.chunk_shape, chunk_index, strict=True):
        start = index * chunk
        out.append(max(0, min(chunk, size - start)))
    return tuple(out)


class RemoteZarrV3:
    """Reads Zarr v3 arrays straight from Cloud Storage over HTTPS."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        access_token: str | None = None,
    ) -> None:
        """Initialize a reader for one store prefix."""
        self._session = session
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
        self._sem = asyncio.Semaphore(_MAX_CONCURRENT_READS)
        self._meta_cache: dict[str, ArrayMeta] = {}

    def _url(self, path: str) -> str:
        """Absolute URL for a store key."""
        return f"{self._base}/{path.lstrip('/')}"

    async def _get_json(self, path: str) -> dict[str, Any]:
        """GET and parse a JSON document."""
        async with self._sem:
            async with self._session.get(self._url(path), headers=self._headers) as resp:
                if resp.status == 404:
                    raise MissingArrayError(f"{path}: HTTP 404")
                if resp.status != 200:
                    raise WNZarrError(f"{path}: HTTP {resp.status}")
                return await resp.json(content_type=None)

    async def _get_range(self, path: str, start: int, length: int) -> bytes:
        """GET ``length`` bytes at ``start`` (HTTP Range)."""
        headers = dict(self._headers)
        headers["Range"] = f"bytes={start}-{start + length - 1}"
        async with self._sem:
            async with self._session.get(self._url(path), headers=headers) as resp:
                if resp.status == 404:
                    return b""
                if resp.status not in (200, 206):
                    raise WNZarrError(f"{path}: HTTP {resp.status}")
                return await resp.read()

    async def _get_tail(self, path: str, length: int) -> bytes:
        """GET the last ``length`` bytes (HTTP suffix Range)."""
        headers = dict(self._headers)
        headers["Range"] = f"bytes=-{length}"
        async with self._sem:
            async with self._session.get(self._url(path), headers=headers) as resp:
                if resp.status == 404:
                    return b""
                if resp.status not in (200, 206):
                    raise WNZarrError(f"{path}: HTTP {resp.status}")
                return await resp.read()

    # ------------------------------------------------------------- metadata

    async def array_meta(self, array_path: str) -> ArrayMeta:
        """Fetch and cache one array's ``zarr.json``."""
        key = array_path.strip("/")
        if key not in self._meta_cache:
            self._meta_cache[key] = parse_array_meta(
                key, await self._get_json(f"{key}/zarr.json")
            )
            meta = self._meta_cache[key]
            # Logged because "which codecs does the live store actually use" is
            # the first question when a decode fails.
            _LOGGER.debug(
                "Zarr array %s: shape=%s chunks=%s dtype=%s codecs=%s dims=%s",
                key,
                meta.shape,
                meta.chunk_shape,
                meta.dtype,
                [codec_name(c) for c in meta.codecs],
                meta.dimension_names,
            )
        return self._meta_cache[key]

    async def coordinate(self, array_path: str) -> Any:
        """Read a 1-D coordinate array (lat/lon) as numpy."""
        import numpy as np

        meta = await self.array_meta(array_path)
        out = np.full(
            meta.shape, fill_value_for(meta.dtype, meta.fill_value), dtype=meta.dtype
        )
        await self._fill(out, meta, {0: slice(0, meta.shape[0])})
        return out

    # --------------------------------------------------------------- reading

    async def read(self, meta: ArrayMeta, selection: dict[int, Any]) -> Any:
        """Read a selection into a new numpy array.

        ``selection`` maps a dimension index to an int or a slice; omitted
        dimensions are read in full. Int dimensions yield a length-1 axis,
        matching Zarr's own behaviour.
        """
        import numpy as np

        bounds = [
            self._bound(meta, dim, selection.get(dim, slice(None)))
            for dim in range(meta.ndim)
        ]
        shape = [1 if isinstance(selection.get(dim, slice(None)), int) else b - a
                 for dim, (a, b) in enumerate(bounds)]
        out = np.full(shape, fill_value_for(meta.dtype, meta.fill_value), dtype=meta.dtype)
        await self._fill(out, meta, selection)
        return out

    @staticmethod
    def _bound(meta: ArrayMeta, dim: int, item: Any) -> tuple[int, int]:
        """Normalize one selection entry to a half-open (start, stop)."""
        if isinstance(item, slice):
            start, stop, _ = item.indices(meta.shape[dim])
            return start, max(start, stop)
        index = int(item)
        if index < 0:
            index += meta.shape[dim]
        return index, index + 1

    async def _fill(self, out: Any, meta: ArrayMeta, selection: dict[int, Any]) -> None:
        """Copy the requested selection from the store into ``out``."""
        bounds = [self._bound(meta, dim, selection.get(dim, slice(None)))
                  for dim in range(meta.ndim)]

        needed: list[tuple[int, ...]] = []
        ranges = []
        for dim, (start, stop) in enumerate(bounds):
            chunk = meta.chunk_shape[dim]
            if stop <= start:
                return  # empty selection
            ranges.append(range(start // chunk, (stop - 1) // chunk + 1))

        def _walk(dim: int, acc: list[int]) -> None:
            if dim == meta.ndim:
                needed.append(tuple(acc))
                return
            for value in ranges[dim]:
                acc.append(value)
                _walk(dim + 1, acc)
                acc.pop()

        _walk(0, [])
        if not needed:
            return

        chunks = await asyncio.gather(*(self._read_chunk(meta, idx) for idx in needed))

        for chunk_index, data in zip(needed, chunks, strict=True):
            src: list[slice] = []
            dest: list[slice] = []
            for dim, (start, _stop) in enumerate(bounds):
                chunk_start = chunk_index[dim] * meta.chunk_shape[dim]
                lo = max(start, chunk_start)
                hi = min(_stop, chunk_start + data.shape[dim])
                if hi <= lo:
                    break
                src.append(slice(lo - chunk_start, hi - chunk_start))
                dest.append(slice(lo - start, hi - start))
            else:
                out[tuple(dest)] = data[tuple(src)]

    async def _read_chunk(self, meta: ArrayMeta, chunk_index: tuple[int, ...]) -> Any:
        """Fetch and decode one chunk, or one shard of a sharded array."""
        shard = meta.sharding
        if shard is None:
            key = self._chunk_key(meta, chunk_index)
            payload = await self._get_whole(key)
            # Chunks at the array edge are stored at full chunk size and padded
            # with the fill value (verified against zarr-python), so decode the
            # full grid and drop the overhang.
            data = self._to_array(payload, meta, meta.chunk_shape)
            return data[
                tuple(slice(0, n) for n in clipped_chunk_shape(meta, chunk_index))
            ]
        return await self._read_shard(meta, chunk_index, shard)

    async def _get_whole(self, path: str) -> bytes:
        """GET a whole object."""
        async with self._sem:
            async with self._session.get(self._url(path), headers=self._headers) as resp:
                if resp.status == 404:
                    return b""
                if resp.status != 200:
                    raise WNZarrError(f"{path}: HTTP {resp.status}")
                return await resp.read()

    def _chunk_key(self, meta: ArrayMeta, chunk_index: tuple[int, ...]) -> str:
        """Build the storage key for a chunk (default/v2 key encoding)."""
        parts = "c" + "".join(f"{meta.separator}{i}" for i in chunk_index)
        return f"{meta.path}/{parts}" if meta.path else parts

    async def _read_shard(
        self, meta: ArrayMeta, chunk_index: tuple[int, ...], shard_cfg: dict[str, Any]
    ) -> Any:
        """Decode a whole shard, then return the region this chunk covers."""
        import numpy as np

        inner_shape = tuple(int(x) for x in shard_cfg.get("chunk_shape") or ())
        if len(inner_shape) != meta.ndim:
            raise WNZarrError(f"{meta.path}: sharding chunk_shape has wrong ndim")

        per_shard = tuple(
            s // c for s, c in zip(meta.chunk_shape, inner_shape, strict=True)
        )
        chunks_total = math.prod(per_shard)

        index_codecs = list(shard_cfg.get("index_codecs") or [{"name": "bytes"}])
        index_len = 16 * chunks_total + (
            4 if any(codec_name(c) == "crc32c" for c in index_codecs) else 0
        )

        key = self._chunk_key(meta, chunk_index)
        if shard_cfg.get("index_location", "end") == "start":
            raw_index = await self._get_range(key, 0, index_len)
        else:
            raw_index = await self._get_tail(key, index_len)

        index = np.frombuffer(_decompress_bytes(raw_index, index_codecs), dtype="<u8")
        if index.size < chunks_total * 2:
            raise WNZarrError(f"{meta.path}: shard index truncated")
        index = index[: chunks_total * 2].reshape((*per_shard, 2))

        shard_shape = clipped_chunk_shape(meta, chunk_index)
        fill = fill_value_for(meta.dtype, meta.fill_value)
        out = np.full(shard_shape, fill, dtype=meta.dtype)

        inner_codecs = list(shard_cfg.get("codecs") or [{"name": "bytes"}])
        wanted = [
            ic for ic in np.ndindex(*per_shard)
            if all(ic[d] * inner_shape[d] < shard_shape[d] for d in range(meta.ndim))
        ]

        async def _load(ic: tuple[int, ...]) -> tuple[tuple[int, ...], Any]:
            offset, length = int(index[ic][0]), int(index[ic][1])
            if offset == _MAX_UINT_64 and length == _MAX_UINT_64:
                return ic, None  # empty inner chunk -> fill value
            payload = await self._get_range(key, offset, length)
            return ic, self._to_array(payload, meta, inner_shape, inner_codecs)

        for ic, data in await asyncio.gather(*(_load(ic) for ic in wanted)):
            target = tuple(
                slice(
                    ic[d] * inner_shape[d],
                    min((ic[d] + 1) * inner_shape[d], shard_shape[d]),
                )
                for d in range(meta.ndim)
            )
            if data is None:
                out[target] = fill
            else:
                out[target] = data[tuple(slice(0, n) for n in out[target].shape)]

        return out

    def _to_array(
        self,
        payload: bytes,
        meta: ArrayMeta,
        shape: tuple[int, ...],
        codecs: list[Any] | None = None,
    ) -> Any:
        """Apply a codec pipeline and interpret the bytes as a numpy array."""
        import numpy as np

        pipeline = [c for c in meta.codecs if codec_name(c) != "sharding_indexed"] \
            if codecs is None else codecs

        for codec in pipeline:
            name = codec_name(codec)
            if name not in ("bytes", "transpose", "zstd", "gzip", "zlib",
                            "crc32c", "blosc", "blosc2"):
                raise WNZarrError(
                    f"{meta.path}: unsupported Zarr codec {name!r} "
                    f"(pipeline: {[codec_name(c) for c in pipeline]})"
                )

        raw = _decompress_bytes(
            payload, [c for c in pipeline if codec_name(c) != "bytes"]
        )

        transpose = next((c for c in pipeline if codec_name(c) == "transpose"), None)
        if transpose is not None:
            order = codec_config(transpose).get("order")
            if order:
                raw = raw.transpose(np.argsort(list(order)))

        bytes_codec = next((c for c in pipeline if codec_name(c) == "bytes"), None)
        endian = (
            codec_config(bytes_codec).get("endian", "little") if bytes_codec else "little"
        )
        dtype = np.dtype(f"{'>' if endian == 'big' else '<'}{meta.dtype}")

        count = math.prod(shape) if shape else 1
        expected = count * dtype.itemsize
        have = len(raw) // dtype.itemsize
        if have < count:
            # Truncated chunk: pad the tail with fill. zarr-python never writes
            # these, but a partial gzip/zstd frame or a hand-built store might.
            padded = np.full(count, fill_value_for(meta.dtype, meta.fill_value), dtype=dtype)
            padded[:have] = np.frombuffer(raw, dtype=dtype, count=have)
            return padded.reshape(shape)
        return np.frombuffer(raw[:expected], dtype=dtype, count=count).reshape(shape)


def grid_dims(
    meta: ArrayMeta, lat_dim: str | None = None, lon_dim: str | None = None
) -> tuple[int, int, int] | None:
    """Locate the (latitude, longitude, lead-time) dimension indices.

    The WN3 store's dimension names have never been confirmed against the live
    bucket, so match on prefix (``lat``/``lon``/``lead``) and fall back to
    position: latitude/longitude are the last two axes, lead time the first.
    Returns None when the array has fewer than three dimensions.
    """
    names = [n or "" for n in meta.dimension_names]

    def _find(exact: str | None, prefix: str, default: int) -> int:
        if exact and exact in names:
            return names.index(exact)
        for i, name in enumerate(names):
            if name.startswith(prefix):
                return i
        return default

    if meta.ndim < 3:
        return None
    lon_i = _find(lon_dim, "lon", meta.ndim - 1)
    lat_i = _find(lat_dim, "lat", meta.ndim - 2)
    if lat_i == lon_i:
        return None

    remaining = [d for d in range(meta.ndim) if d not in (lat_i, lon_i)]
    if not remaining:
        return None
    lead_i = _find(None, "lead", remaining[0])
    return lat_i, lon_i, lead_i


async def array_exists(reader: RemoteZarrV3, array_path: str) -> bool:
    """True when the array's ``zarr.json`` can be fetched and parsed."""
    try:
        await reader.array_meta(array_path)
    except (MissingArrayError, TimeoutError, WNZarrError, aiohttp.ClientError):
        return False
    return True


async def read_series(
    reader: RemoteZarrV3,
    array_path: str,
    lat_index: int,
    lon_index: int,
    lead_start: int,
    lead_count: int,
    lat_dim: str | None = None,
    lon_dim: str | None = None,
) -> Any:
    """Read a 1-D lead-time series at one grid cell.

    Returns None when the array is absent or lacks the expected dimensions;
    genuine decode failures propagate.
    """
    try:
        meta = await reader.array_meta(array_path)
    except (MissingArrayError, TimeoutError, aiohttp.ClientError):
        return None

    dims = grid_dims(meta, lat_dim, lon_dim)
    if dims is None:
        return None
    lat_i, lon_i, lead_i = dims

    data = await reader.read(
        meta,
        {
            lat_i: lat_index,
            lon_i: lon_index,
            lead_i: slice(lead_start, min(lead_start + lead_count, meta.shape[lead_i])),
        },
    )
    return data.reshape(-1)


def nearest_index(values: Any, target: float) -> int:
    """Index of the value closest to ``target`` (ties -> first)."""
    import numpy as np

    values = np.asarray(values)
    return int(np.abs(values - target).argmin())


async def read_plane(
    reader: RemoteZarrV3,
    array_path: str,
    lead_index: int = 0,
    lat_dim: str | None = None,
    lon_dim: str | None = None,
) -> Any:
    """Read the whole 2-D (latitude, longitude) field at one lead step.

    Used by the global overlay, which renders full grids rather than a point.
    Returns None when the array is absent or lacks the expected dimensions;
    genuine decode failures propagate.
    """
    try:
        meta = await reader.array_meta(array_path)
    except (MissingArrayError, TimeoutError, aiohttp.ClientError):
        return None
    dims = grid_dims(meta, lat_dim, lon_dim)
    if dims is None:
        return None
    lat_i, lon_i, lead_i = dims

    selection: dict[int, Any] = {
        lat_i: slice(0, meta.shape[lat_i]),
        lon_i: slice(0, meta.shape[lon_i]),
        lead_i: lead_index,
    }

    data = await reader.read(meta, selection)
    # int selections yield length-1 axes; drop them so the caller gets (lat, lon).
    return data.reshape(meta.shape[lat_i], meta.shape[lon_i])
