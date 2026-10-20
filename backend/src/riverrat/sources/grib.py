"""Decoding precipitation grids from GRIB2 messages on projected grids (Stage IV's polar
stereographic, NBM's Lambert conformal)."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import eccodes
import numpy as np
import shapely
from shapely.geometry.base import BaseGeometry

from riverrat.geo.projection import LambertConformal, PolarStereographic

Projection = PolarStereographic | LambertConformal


class GribFormatError(ValueError):
    """The message isn't a precipitation grid in a form we understand."""


@dataclass(frozen=True)
class Grid:
    """Cell centers in projected meters, both ascending; rows are y, columns x."""

    projection: Projection
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
class Message:
    issued: datetime  # the forecast's reference time (for analyses, the period start)
    start: datetime  # accumulation period
    end: datetime
    percentile: int | None  # for percentile forecasts
    grid: Grid
    mm: np.ndarray  # y × x, NaN where missing


def decode(raw: bytes) -> Message:
    handle = eccodes.codes_new_from_message(raw)
    try:
        get = lambda key: eccodes.codes_get(handle, key)  # noqa: E731
        grid_type = get("gridType")
        if get("projectionCentreFlag") != 0:
            raise GribFormatError("expected a northern-hemisphere projection")
        if grid_type == "polar_stereographic":
            if get("LaDInDegrees") <= 0:
                raise GribFormatError("expected a north polar projection")
            projection: Projection = PolarStereographic(
                lat_ts=get("LaDInDegrees"),
                lon_0=get("orientationOfTheGridInDegrees"),
                radius=get("radius"),
            )
        elif grid_type == "lambert":
            projection = LambertConformal(
                lat_1=get("Latin1InDegrees"),
                lat_2=get("Latin2InDegrees"),
                lon_0=get("LoVInDegrees"),
                radius=get("radius"),
            )
        else:
            raise GribFormatError(f"unsupported grid {grid_type}")
        if get("shapeOfTheEarth") not in (0, 1, 6):  # spheres
            raise GribFormatError("expected a spherical earth")
        if get("jPointsAreConsecutive"):
            raise GribFormatError("expected rows of x")

        nx, ny = get("Nx"), get("Ny")
        values = eccodes.codes_get_values(handle).reshape(ny, nx)
        missing = get("missingValue")
        issued = datetime.strptime(f"{get('dataDate')}{get('dataTime'):04d}", "%Y%m%d%H%M").replace(
            tzinfo=UTC
        )
        first, last = (int(h) for h in str(get("stepRange")).split("-"))
        # Product templates 6 and 10 are percentile forecasts (10: over a time interval).
        is_percentile = get("productDefinitionTemplateNumber") in (6, 10)
        percentile = get("percentileValue") if is_percentile else None

        x0, y0 = (
            float(v)
            for v in projection.forward(
                get("longitudeOfFirstGridPointInDegrees"),
                get("latitudeOfFirstGridPointInDegrees"),
            )
        )
        dx = get("DxInMetres") * (-1 if get("iScansNegatively") else 1)
        dy = get("DyInMetres") * (1 if get("jScansPositively") else -1)
    except eccodes.KeyValueNotFoundError as exc:
        raise GribFormatError(str(exc)) from exc
    finally:
        eccodes.codes_release(handle)

    x, y = x0 + dx * np.arange(nx), y0 + dy * np.arange(ny)
    mm = np.where((values == missing) | (values < 0), np.nan, values).astype(np.float32)
    if dx < 0:
        x, mm = x[::-1], mm[:, ::-1]
    if dy < 0:
        y, mm = y[::-1], mm[::-1, :]
    return Message(
        issued=issued,
        start=issued + timedelta(hours=first),
        end=issued + timedelta(hours=last),
        percentile=percentile,
        grid=Grid(projection, x, y),
        mm=mm,
    )
