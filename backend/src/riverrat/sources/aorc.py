"""NOAA AORC v1.1 hourly precipitation, from AWS Open Data (DESIGN.md §4.3).

One Zarr v2 store per year (`{year}.zarr`), on a 1/120° (~1 km) lat/lon grid over CONUS.
`APCP_surface` holds each hour's rain in mm (int16 × 0.1), stamped at the end of the
hour, in chunks of 144 hours × 128 × 256 cells. Since 2002 its precipitation is derived
from Stage IV. We read chunks straight over HTTPS (no zarr/s3fs dependency) and decode
them with pyarrow's zstd codec.
"""

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from math import ceil, prod

import numpy as np
import pyarrow as pa

from riverrat.http import PoliteClient

BUCKET_URL = "https://noaa-nws-aorc-v1-1-1km.s3.amazonaws.com"
RAIN = "APCP_surface"
_S3_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


class AorcFormatError(ValueError):
    """The store's layout isn't what this reader understands."""


def list_years(client: PoliteClient) -> list[int]:
    body = client.get(f"{BUCKET_URL}/?list-type=2&delimiter=/").text
    return parse_year_listing(body)


def parse_year_listing(xml: str) -> list[int]:
    prefixes = [p.text or "" for p in ET.fromstring(xml).iterfind(".//s3:Prefix", _S3_NS)]
    return sorted(int(p[:4]) for p in prefixes if p.endswith(".zarr/") and p[:4].isdigit())


@dataclass(frozen=True)
class ArraySpec:
    shape: tuple[int, ...]
    chunks: tuple[int, ...]
    dtype: np.dtype
    scale: float = 1.0
    fill: float | None = None

    def chunk_counts(self) -> tuple[int, ...]:
        return tuple(ceil(s / c) for s, c in zip(self.shape, self.chunks, strict=True))


def parse_spec(metadata: dict, name: str) -> ArraySpec:
    meta = metadata["metadata"]
    array, attrs = meta[f"{name}/.zarray"], meta.get(f"{name}/.zattrs", {})
    compressor = array.get("compressor") or {}
    if compressor.get("id") != "zstd" or array.get("filters") or array.get("order", "C") != "C":
        raise AorcFormatError(f"{name}: unsupported encoding {compressor} {array.get('filters')}")
    return ArraySpec(
        shape=tuple(array["shape"]),
        chunks=tuple(array["chunks"]),
        dtype=np.dtype(array["dtype"]),
        scale=float(attrs.get("scale_factor", 1.0)),
        fill=attrs.get("_FillValue", array.get("fill_value")),
    )


def decode_chunk(raw: bytes, spec: ArraySpec) -> np.ndarray:
    """One chunk as float32 in physical units, with fill values as NaN. Zarr v2 stores
    edge chunks at full size, so the result always has shape `spec.chunks`."""
    size = prod(spec.chunks) * spec.dtype.itemsize
    data = np.frombuffer(pa.decompress(raw, size, codec="zstd"), dtype=spec.dtype)
    values = data.astype(np.float32)
    if spec.fill is not None:
        values[data == spec.fill] = np.nan
    if spec.scale != 1.0:
        values *= np.float32(spec.scale)
    return values.reshape(spec.chunks)


class Year:
    """One year's store: grid coordinates, hour stamps, and rain chunks on demand."""

    def __init__(self, client: PoliteClient, year: int) -> None:
        self._client = client
        self.url = f"{BUCKET_URL}/{year}.zarr"
        metadata = json.loads(client.get(f"{self.url}/.zmetadata").content)
        self.rain = parse_spec(metadata, RAIN)
        self.lat = self._coordinate(parse_spec(metadata, "latitude"), "latitude")
        self.lon = self._coordinate(parse_spec(metadata, "longitude"), "longitude")
        seconds = self._coordinate(parse_spec(metadata, "time"), "time")
        self.time = seconds.astype("int64").astype("datetime64[s]")
        if (len(self.time), len(self.lat), len(self.lon)) != self.rain.shape:
            raise AorcFormatError(f"{self.url}: coordinates don't match {self.rain.shape}")

    def rain_chunk(self, t: int, i: int, j: int) -> np.ndarray:
        """Hours × latitude × longitude, mm."""
        return decode_chunk(self._client.get(f"{self.url}/{RAIN}/{t}.{i}.{j}").content, self.rain)

    def _coordinate(self, spec: ArraySpec, name: str) -> np.ndarray:
        # Coordinates are float64; decode them as-is so nothing is lost to float32.
        size = prod(spec.chunks) * spec.dtype.itemsize
        parts = []
        for n in range(spec.chunk_counts()[0]):
            raw = self._client.get(f"{self.url}/{name}/{n}").content
            parts.append(np.frombuffer(pa.decompress(raw, size, codec="zstd"), spec.dtype))
        return np.concatenate(parts)[: spec.shape[0]]
