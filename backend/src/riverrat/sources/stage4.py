"""NCEP Stage IV hourly precipitation (DESIGN.md §4.3): our live and recent rain.

AORC, our training history, is built from Stage IV, so daily basin totals agree within
~0.5 mm on wet days (checked over 2025). Files are GRIB2 on the 4.76 km HRAP polar
stereographic grid, one per hour, named by the hour's end (UTC). NOMADS keeps about a
week and revises hours as RFC quality control arrives; Iowa State's IEM archive has
every hour since 2002.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import eccodes
import numpy as np
import shapely
from shapely.geometry.base import BaseGeometry

NOMADS_URL = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/pcpanl/prod"
IEM_URL = "https://mesonet.agron.iastate.edu/archive/data"
# Hours newer than this come from NOMADS (NOAA's own server); older ones from IEM.
NOMADS_WINDOW = timedelta(days=6)


class Stage4FormatError(ValueError):
    """The file isn't a one-hour polar stereographic precipitation grid."""


def nomads_url(hour_end: datetime) -> str:
    return f"{NOMADS_URL}/pcpanl.{hour_end:%Y%m%d}/st4_conus.{hour_end:%Y%m%d%H}.01h.grb2"


def iem_url(hour_end: datetime) -> str:
    return f"{IEM_URL}/{hour_end:%Y/%m/%d}/stage4/ST4.{hour_end:%Y%m%d%H}.01h.grib"


def url_for(hour_end: datetime, now: datetime) -> str:
    return nomads_url(hour_end) if now - hour_end < NOMADS_WINDOW else iem_url(hour_end)


@dataclass(frozen=True)
class PolarStereographic:
    """North polar stereographic projection on a sphere (Snyder, Map Projections, §21),
    true to scale at `lat_ts`. Written out here rather than using pyproj, whose bundled
    C libraries clash with ecCodes' on Linux."""

    lat_ts: float
    lon_0: float
    radius: float

    def _scale(self) -> float:
        return self.radius * (1 + np.sin(np.radians(self.lat_ts)))

    def forward(self, lon, lat) -> tuple[np.ndarray, np.ndarray]:
        rho = self._scale() * np.tan(np.radians(45 - np.asarray(lat) / 2))
        theta = np.radians(np.asarray(lon) - self.lon_0)
        return rho * np.sin(theta), -rho * np.cos(theta)

    def inverse(self, x, y) -> tuple[np.ndarray, np.ndarray]:
        x, y = np.asarray(x), np.asarray(y)
        lat = 90 - 2 * np.degrees(np.arctan(np.hypot(x, y) / self._scale()))
        lon = (self.lon_0 + np.degrees(np.arctan2(x, -y)) + 180) % 360 - 180
        return lon, lat


@dataclass(frozen=True)
class Grid:
    """Cell centers in projected meters, both ascending; rows are y, columns x."""

    projection: PolarStereographic
    x: np.ndarray
    y: np.ndarray

    def same_as(self, other: "Grid") -> bool:
        return (
            self.projection == other.projection
            and np.array_equal(self.x, other.x)
            and np.array_equal(self.y, other.y)
        )

    def project(self, geom: BaseGeometry) -> BaseGeometry:
        """A lon/lat geometry in this grid's coordinates."""
        return shapely.transform(
            geom, lambda xy: np.column_stack(self.projection.forward(xy[:, 0], xy[:, 1]))
        )


@dataclass(frozen=True)
class Field:
    hour_end: datetime
    grid: Grid
    mm: np.ndarray  # y × x, NaN where missing


def decode(raw: bytes) -> Field:
    handle = eccodes.codes_new_from_message(raw)
    try:
        get = lambda key: eccodes.codes_get(handle, key)  # noqa: E731
        if get("gridType") != "polar_stereographic" or get("stepRange") != "0-1":
            raise Stage4FormatError(f"{get('gridType')} {get('stepRange')}")
        nx, ny = get("Nx"), get("Ny")
        values = eccodes.codes_get_values(handle).reshape(ny, nx)
        missing = get("missingValue")
        hour_end = datetime.strptime(
            f"{get('validityDate')}{get('validityTime'):04d}", "%Y%m%d%H%M"
        ).replace(tzinfo=UTC)
        if get("LaDInDegrees") <= 0 or get("projectionCentreFlag") != 0:
            raise Stage4FormatError("expected a north polar projection")
        projection = PolarStereographic(
            lat_ts=get("LaDInDegrees"),
            lon_0=get("orientationOfTheGridInDegrees"),
            radius=get("radius"),
        )
        lon0, lat0 = (
            get("longitudeOfFirstGridPointInDegrees"),
            get("latitudeOfFirstGridPointInDegrees"),
        )
        x0, y0 = (float(v) for v in projection.forward(lon0, lat0))
        dx = get("DxInMetres") * (-1 if get("iScansNegatively") else 1)
        dy = get("DyInMetres") * (1 if get("jScansPositively") else -1)
    finally:
        eccodes.codes_release(handle)

    x, y = x0 + dx * np.arange(nx), y0 + dy * np.arange(ny)
    mm = np.where((values == missing) | (values < 0), np.nan, values).astype(np.float32)
    if dx < 0:
        x, mm = x[::-1], mm[:, ::-1]
    if dy < 0:
        y, mm = y[::-1], mm[::-1, :]
    return Field(hour_end, Grid(projection, x, y), mm)
