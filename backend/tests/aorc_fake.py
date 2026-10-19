"""A tiny in-memory AORC store served over httpx2.MockTransport.

Grid: cells of 0.1° centered on 38.05…38.75°N and -81.95…-81.25°E (8 × 8), with chunks
of 4 hours × 4 × 4 cells, so tests exercise several spatial chunks and a ragged last
time chunk.
"""

import json
from datetime import UTC, datetime
from math import ceil

import httpx2
import numpy as np
import pyarrow as pa

from riverrat.sources import aorc

LAT = np.round(38.05 + 0.1 * np.arange(8), 4)
LON = np.round(-81.95 + 0.1 * np.arange(8), 4)
CHUNKS = (4, 4, 4)
FILL = -32767
SCALE = 0.1


def _zstd(array: np.ndarray) -> bytes:
    return pa.compress(np.ascontiguousarray(array).tobytes(), codec="zstd", asbytes=True)


def _zarray(shape, chunks, dtype) -> dict:
    return {
        "shape": list(shape),
        "chunks": list(chunks),
        "dtype": dtype,
        "compressor": {"id": "zstd", "level": 3},
        "filters": None,
        "order": "C",
        "zarr_format": 2,
    }


class FakeAorc:
    def __init__(self, rain_by_year: dict[int, np.ndarray]):
        """`rain_by_year[year]` is hours × 8 × 8 in mm (NaN = missing)."""
        self.files: dict[str, bytes] = {}
        self.requests: list[str] = []
        self.years = sorted(rain_by_year)
        for year, rain in rain_by_year.items():
            self._add_year(year, rain)

    def chunk_requests(self) -> list[str]:
        return [path for path in self.requests if f"/{aorc.RAIN}/" in path]

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        self.requests.append(path)
        if path == "/":
            prefixes = "".join(
                f"<CommonPrefixes><Prefix>{y}.zarr/</Prefix></CommonPrefixes>" for y in self.years
            )
            xml = (
                '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"{prefixes}</ListBucketResult>"
            )
            return httpx2.Response(200, text=xml)
        if path not in self.files:
            return httpx2.Response(404)
        return httpx2.Response(200, content=self.files[path])

    def _add_year(self, year: int, rain: np.ndarray) -> None:
        n_hours = rain.shape[0]
        base = f"/{year}.zarr"
        start = datetime(year, 1, 1, 1, tzinfo=UTC).timestamp()
        time = start + 3600.0 * np.arange(n_hours)
        metadata = {
            "metadata": {
                ".zgroup": {"zarr_format": 2},
                f"{aorc.RAIN}/.zarray": _zarray(rain.shape, CHUNKS, "<i2"),
                f"{aorc.RAIN}/.zattrs": {"_FillValue": FILL, "scale_factor": SCALE},
                "latitude/.zarray": _zarray((8,), (4,), "<f8"),
                "longitude/.zarray": _zarray((8,), (4,), "<f8"),
                "time/.zarray": _zarray((n_hours,), (CHUNKS[0],), "<f8"),
            }
        }
        self.files[f"{base}/.zmetadata"] = json.dumps(metadata).encode()
        for name, values in (("latitude", LAT), ("longitude", LON), ("time", time)):
            for n in range(ceil(len(values) / 4)):
                part = np.zeros(4, "<f8")
                chunk = values[n * 4 : n * 4 + 4]
                part[: len(chunk)] = chunk
                self.files[f"{base}/{name}/{n}"] = _zstd(part)

        stored = np.where(np.isnan(rain), FILL, np.round(rain / SCALE)).astype("<i2")
        for t in range(ceil(n_hours / CHUNKS[0])):
            for i in range(2):
                for j in range(2):
                    block = np.full(CHUNKS, FILL, "<i2")  # edge chunks are padded
                    part = stored[t * 4 : t * 4 + 4, i * 4 : i * 4 + 4, j * 4 : j * 4 + 4]
                    block[: part.shape[0]] = part
                    self.files[f"{base}/{aorc.RAIN}/{t}.{i}.{j}"] = _zstd(block)
