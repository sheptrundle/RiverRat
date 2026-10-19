"""Synthetic Stage IV GRIB2 files (polar stereographic, like the real HRAP grid) and a
fake NOMADS/IEM server."""

from datetime import datetime, timedelta
from email.utils import format_datetime

import eccodes
import httpx2
import numpy as np

# A 6 × 5 grid of 4.7625 km cells whose first point is at 38°N, 82°W.
NX, NY = 6, 5
LAT0, LON0 = 38.0, 278.0


def grib(hour_end: datetime, mm: np.ndarray) -> bytes:
    """One hour of rain (NY × NX, south to north; NaN = missing) ending at `hour_end`."""
    handle = eccodes.codes_grib_new_from_samples("polar_stereographic_pl_grib2")
    try:
        for key, value in {
            "Nx": NX,
            "Ny": NY,
            "LaDInDegrees": 60.0,
            "orientationOfTheGridInDegrees": 255.0,
            "DxInMetres": 4762.5,
            "DyInMetres": 4762.5,
            "latitudeOfFirstGridPointInDegrees": LAT0,
            "longitudeOfFirstGridPointInDegrees": LON0,
            "shapeOfTheEarth": 1,
            "scaleFactorOfRadiusOfSphericalEarth": 0,
            "scaledValueOfRadiusOfSphericalEarth": 6371200,
            "jScansPositively": 1,
            "iScansNegatively": 0,
            "productDefinitionTemplateNumber": 8,
        }.items():
            eccodes.codes_set(handle, key, value)
        start = hour_end - timedelta(hours=1)
        eccodes.codes_set(handle, "dataDate", int(f"{start:%Y%m%d}"))
        eccodes.codes_set(handle, "dataTime", int(f"{start:%H%M}"))
        eccodes.codes_set(handle, "stepRange", "0-1")
        eccodes.codes_set(handle, "bitmapPresent", 1)
        eccodes.codes_set(handle, "missingValue", 9999)
        eccodes.codes_set_values(handle, np.where(np.isnan(mm), 9999, mm).ravel())
        return eccodes.codes_get_message(handle)
    finally:
        eccodes.codes_release(handle)


class FakeStage4:
    def __init__(self):
        self.files: dict[str, tuple[bytes, datetime]] = {}  # url -> (body, last modified)
        self.gets: list[str] = []
        self.heads: list[str] = []

    def put(self, url: str, body: bytes, modified: datetime) -> None:
        self.files[url] = (body, modified)

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        (self.heads if request.method == "HEAD" else self.gets).append(url)
        if url not in self.files:
            return httpx2.Response(404)
        body, modified = self.files[url]
        headers = {"Last-Modified": format_datetime(modified, usegmt=True)}
        return httpx2.Response(
            200, content=b"" if request.method == "HEAD" else body, headers=headers
        )
