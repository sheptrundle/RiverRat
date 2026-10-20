"""Synthetic NBM qmd files (Lambert conformal, percentile fields, wgrib2-style `.idx`) and
a fake S3 bucket that answers HEAD and byte-range GETs."""

from datetime import datetime, timedelta

import eccodes
import httpx2
import numpy as np

from riverrat.sources import nbm

# A 6 × 5 grid of 2.54 km cells whose first point is at 38°N, 82°W, on NBM's projection.
NX, NY = 6, 5
LAT0, LON0 = 38.0, 278.0
DX = 2539.703


def message(issued: datetime, lead: int, percentile: int | None, mm: np.ndarray) -> bytes:
    """The 6-hour total ending `lead` hours after `issued` (NY × NX, south to north); a
    probability field instead when `percentile` is None."""
    handle = eccodes.codes_grib_new_from_samples("GRIB2")
    try:
        eccodes.codes_set(handle, "gridDefinitionTemplateNumber", 30)
        for key, value in {
            "Nx": NX,
            "Ny": NY,
            "LaDInDegrees": 25.0,
            "LoVInDegrees": 265.0,
            "Latin1InDegrees": 25.0,
            "Latin2InDegrees": 25.0,
            "DxInMetres": DX,
            "DyInMetres": DX,
            "latitudeOfFirstGridPointInDegrees": LAT0,
            "longitudeOfFirstGridPointInDegrees": LON0,
            "shapeOfTheEarth": 1,
            "scaleFactorOfRadiusOfSphericalEarth": 0,
            "scaledValueOfRadiusOfSphericalEarth": 6371200,
            "iScansNegatively": 0,
            "jScansPositively": 1,
            "productDefinitionTemplateNumber": 10 if percentile is not None else 9,
        }.items():
            eccodes.codes_set(handle, key, value)
        if percentile is not None:
            eccodes.codes_set(handle, "percentileValue", percentile)
        eccodes.codes_set(handle, "dataDate", int(f"{issued:%Y%m%d}"))
        eccodes.codes_set(handle, "dataTime", int(f"{issued:%H%M}"))
        eccodes.codes_set(handle, "stepRange", f"{lead - nbm.PERIOD_H}-{lead}")
        eccodes.codes_set_values(handle, mm.astype(float).ravel())
        return eccodes.codes_get_message(handle)
    finally:
        eccodes.codes_release(handle)


def field(percentile: int) -> np.ndarray:
    """Every cell gets percentile / 10 mm, so any basin's mean is known exactly."""
    return np.full((NY, NX), percentile / 10)


class FakeNbm:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.requests: list[tuple[str, str, str | None]] = []  # method, url, range

    def publish(self, issued: datetime, *, leads=None, wrong_start: bool = False) -> None:
        """Write a run's qmd files: a probability field, then every 5th percentile."""
        for lead in leads or nbm.leads():
            parts, index = [], []
            offset = 0
            step = f"{lead - nbm.PERIOD_H}-{lead} hour acc fcst"
            date = f"d={issued:%Y%m%d%H}"
            fields = [(None, "prob >0.254:prob fcst 255/255")] + [
                (p, f"{p}% level") for p in range(0, 101, 5)
            ]
            for n, (percentile, label) in enumerate(fields, start=1):
                shown = issued - timedelta(hours=6) if wrong_start else issued
                body = message(shown, lead, percentile, field(percentile or 0))
                index.append(f"{n}:{offset}:{date}:APCP:surface:{step}:{label}")
                parts.append(body)
                offset += len(body)
            tail = f"TMP:2 m above ground:{lead} hour fcst:"
            index.append(f"{len(fields) + 1}:{offset}:{date}:{tail}")
            parts.append(b"7777")  # stands in for the rest of the file
            url = nbm.qmd_url(issued, lead)
            self.files[url] = b"".join(parts)
            self.files[url + ".idx"] = ("\n".join(index) + "\n").encode()

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        byte_range = request.headers.get("Range")
        self.requests.append((request.method, url, byte_range))
        if url not in self.files:
            return httpx2.Response(403)  # what S3 says for a missing key without list rights
        body = self.files[url]
        if request.method == "HEAD":
            return httpx2.Response(200, headers={"Content-Length": str(len(body))})
        if byte_range:
            start, end = (int(v) for v in byte_range.removeprefix("bytes=").split("-"))
            return httpx2.Response(206, content=body[start : end + 1])
        return httpx2.Response(200, content=body)
