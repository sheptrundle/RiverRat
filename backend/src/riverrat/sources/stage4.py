"""NCEP Stage IV hourly precipitation (DESIGN.md §4.3): our live and recent rain.

AORC, our training history, is built from Stage IV, so daily basin totals agree within
~0.5 mm on wet days (checked over 2025). Files are GRIB2 on the 4.76 km HRAP polar
stereographic grid, one per hour, named by the hour's end (UTC). NOMADS keeps about a
week and revises hours as RFC quality control arrives; Iowa State's IEM archive has
every hour since 2002.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from riverrat.sources import grib

NOMADS_URL = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/pcpanl/prod"
IEM_URL = "https://mesonet.agron.iastate.edu/archive/data"
# Hours newer than this come from NOMADS (NOAA's own server); older ones from IEM.
NOMADS_WINDOW = timedelta(days=6)


class Stage4FormatError(grib.GribFormatError):
    """The file isn't a one-hour precipitation grid."""


def nomads_url(hour_end: datetime) -> str:
    return f"{NOMADS_URL}/pcpanl.{hour_end:%Y%m%d}/st4_conus.{hour_end:%Y%m%d%H}.01h.grb2"


def iem_url(hour_end: datetime) -> str:
    return f"{IEM_URL}/{hour_end:%Y/%m/%d}/stage4/ST4.{hour_end:%Y%m%d%H}.01h.grib"


def url_for(hour_end: datetime, now: datetime) -> str:
    return nomads_url(hour_end) if now - hour_end < NOMADS_WINDOW else iem_url(hour_end)


@dataclass(frozen=True)
class Field:
    hour_end: datetime
    grid: grib.Grid
    mm: np.ndarray  # y × x, NaN where missing


def decode(raw: bytes) -> Field:
    message = grib.decode(raw)
    if message.end - message.start != timedelta(hours=1) or message.percentile is not None:
        raise Stage4FormatError(f"{message.start:%Y-%m-%d %H}–{message.end:%H} UTC")
    return Field(message.end, message.grid, message.mm)
