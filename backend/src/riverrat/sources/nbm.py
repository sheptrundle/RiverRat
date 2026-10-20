"""National Blend of Models (DESIGN.md §4.4): forecast rain with calibrated uncertainty.

NBM's `qmd` files on AWS Open Data carry 6-hour precipitation totals as percentiles
(every 5% now, every 1% before late 2023) on the 2.5 km CONUS Lambert grid. Only the
00/06/12/18Z runs reach past 36 h (to ~8 days). Each field is ~1 MB inside a ~200 MB
file, so we read the `.idx` beside it and fetch just the fields we need by byte range.
The bucket keeps every run since May 2020.
"""

import re
from dataclasses import dataclass
from datetime import datetime

BUCKET_URL = "https://noaa-nbm-grib2-pds.s3.amazonaws.com"
CYCLES = (0, 6, 12, 18)
PERIOD_H = 6
HORIZON_H = 144
PERCENTILES = (5, 10, 25, 50, 75, 90, 95)

_FIELD = re.compile(r"^APCP:surface:(\d+)-(\d+) hour acc fcst:(\d+)% level$")


def qmd_url(issued: datetime, lead_h: int) -> str:
    return (
        f"{BUCKET_URL}/blend.{issued:%Y%m%d}/{issued:%H}/qmd/"
        f"blend.t{issued:%H}z.qmd.f{lead_h:03d}.co.grib2"
    )


def leads() -> range:
    """Forecast hours whose file holds the 6-hour total ending then."""
    return range(PERIOD_H, HORIZON_H + 1, PERIOD_H)


@dataclass(frozen=True)
class ByteRange:
    percentile: int
    start: int
    end: int  # inclusive

    def header(self) -> dict[str, str]:
        return {"Range": f"bytes={self.start}-{self.end}"}


def percentile_ranges(idx: str, lead_h: int) -> list[ByteRange]:
    """Byte ranges of the wanted percentiles of the 6-hour total ending at `lead_h`, from
    a wgrib2-style index (`n:offset:d=YYYYMMDDHH:VAR:level:step:extra`)."""
    entries = []
    for line in idx.splitlines():
        parts = line.split(":", 3)
        if len(parts) == 4:
            entries.append((int(parts[1]), parts[3]))
    ranges = []
    for n, (offset, description) in enumerate(entries):
        match = _FIELD.match(description.rstrip(":"))
        if not match:
            continue
        first, last, percentile = (int(g) for g in match.groups())
        if (first, last) != (lead_h - PERIOD_H, lead_h) or percentile not in PERCENTILES:
            continue
        if n + 1 == len(entries):
            raise ValueError("can't size the last message without the file length")
        ranges.append(ByteRange(percentile, offset, entries[n + 1][0] - 1))
    found = {r.percentile for r in ranges}
    if found != set(PERCENTILES):
        missing = sorted(set(PERCENTILES) - found)
        raise ValueError(f"f{lead_h:03d} index lacks percentiles {missing}")
    return ranges
