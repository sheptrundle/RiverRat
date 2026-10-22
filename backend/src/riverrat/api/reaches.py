"""Reach endpoints (DESIGN.md §9): the What-if forecast for one storm, and the slider's
curve over storm sizes.

A reach is forecast through its gauge: the primary USGS gauge with a usable runnable
range. When the primary can't be used (often an AW "calculated" gauge), another of the
reach's USGS gauges stands in, preferring one on the same river. Flows are modeled in
cfs and reported in the range's units, through the gauge's stage rating for reaches
rated in feet.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Literal

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from riverrat.config import get_settings
from riverrat.db.models import Basin, Gauge, Reach, ReachGauge
from riverrat.db.session import get_engine
from riverrat.forecast import bands as band_names
from riverrat.forecast import live, response
from riverrat.forecast.bands import Bands
from riverrat.forecast.daily import Day, daily
from riverrat.forecast.daylight import sun_times
from riverrat.models import analog, store

router = APIRouter(prefix="/api/reaches", tags=["reaches"])

StormType = Literal["burst", "typical", "soaker"]
MAX_START_H = 120  # a storm starting later would barely fit in the 6-day horizon
LEVELS = (0.1, 0.5, 0.9)
SHOWN_ANALOGS = 8


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session


def get_data_dir() -> Path:
    return get_settings().data_dir


def get_now() -> datetime:
    return datetime.now(UTC)


SessionDep = Annotated[Session, Depends(get_session)]
DataDirDep = Annotated[Path, Depends(get_data_dir)]
NowDep = Annotated[datetime, Depends(get_now)]


# --- response models -----------------------------------------------------------------


class ReachSummary(BaseModel):
    id: int
    aw_id: int | None
    river: str
    section: str
    difficulty: str | None
    states: list[str]
    gauge_site: str | None  # the primary gauge
    metric: str | None
    range_min: float | None
    range_max: float | None


class ReachInfo(BaseModel):
    id: int
    aw_id: int | None
    river: str
    section: str
    difficulty: str | None


class GaugeInfo(BaseModel):
    id: int
    site: str
    name: str
    primary: bool  # False: standing in for AW's primary gauge


class BandsInfo(BaseModel):
    metric: str
    minimum: float | None
    maximum: float | None
    edges: list[float | None]  # lower edges of low, medium, high, too high; None if unbounded


class Now(BaseModel):
    at: datetime  # end of the latest hour with readings: hour 0 of the forecast
    cfs: float
    value: float  # in the range's units
    band: str
    ft_observed: float | None
    api_mm: float  # antecedent precipitation index (basin wetness)
    rain_hours: int  # hours of live basin rain behind it, of 720


class StormInfo(BaseModel):
    rain_in: float
    type: StormType
    start_h: int
    duration_h: int


class Fan(BaseModel):
    """Hourly flow quantiles in the range's units; hour h ends at start + h hours."""

    start: datetime
    p10: list[float]
    p50: list[float]
    p90: list[float]


class Series(BaseModel):
    """Hourly values in the range's units; value i ends at start + i hours (None = no
    readings)."""

    start: datetime
    values: list[float | None]


class Peak(BaseModel):
    p10: float
    p50: float
    p90: float


class DayOut(BaseModel):
    day: date
    sunrise: datetime
    sunset: datetime
    p_runnable: float
    peak_bands: dict[str, float]
    p_too_high_all_day: float
    peak: Peak
    hours_in_range: float


class AnalogOut(BaseModel):
    start: datetime
    rain_in: float
    duration_h: int
    q0_cfs: float
    qpeak_cfs: float
    t_peak_h: int
    weight: float


class WhatIf(BaseModel):
    reach: ReachInfo
    gauge: GaugeInfo
    bands: BandsInfo
    now: Now
    observed: Series  # the last 48 hours, through now.at
    storm: StormInfo
    forecast: Fan
    days: list[DayOut]
    analogs: list[AnalogOut]  # the most similar past storms, most similar first
    extrapolated: bool  # more rain than any matched storm: numbers are rough


class RainRange(BaseModel):
    p10: float
    p50: float
    p90: float


class DayCurveOut(BaseModel):
    day: date
    p_runnable: list[float]  # per rain amount
    p_reach: list[float]
    p_too_high_all_day: list[float]
    rain_needed_in: float | None
    forecast_rain_in: RainRange | None  # NBM basin total from now through this day's sunset


class RainResponse(BaseModel):
    reach: ReachInfo
    gauge: GaugeInfo
    bands: BandsInfo
    now: Now
    storm_type: StormType
    start_h: int
    rain_in: list[float]
    peak_p10: list[float]  # peak in the range's units, per rain amount
    peak_p50: list[float]
    peak_p90: list[float]
    days: list[DayCurveOut]
    forecast_issued_at: datetime | None


# --- assembling a reach's forecast ---------------------------------------------------


@dataclass(frozen=True)
class Target:
    reach: Reach
    gauge: Gauge
    bands: Bands
    lat: float
    lon: float
    library: analog.AnalogLibrary
    current: live.Current
    to_units: Callable
    primary: bool


def _target(session: Session, data_dir: Path, reach_id: int, now: datetime) -> Target:
    reach = session.get(Reach, reach_id)
    if reach is None:
        raise HTTPException(404, "no such reach")
    rows = session.execute(
        select(ReachGauge, Gauge, func.ST_Y(Gauge.geom), func.ST_X(Gauge.geom), Basin.area_sqmi)
        .join(Gauge, Gauge.id == ReachGauge.gauge_id)
        .outerjoin(Basin, Basin.gauge_id == Gauge.id)
        .where(
            ReachGauge.reach_id == reach_id,
            Gauge.source == "USGS",
            Gauge.geom.is_not(None),
            ReachGauge.metric.in_(("cfs", "ft")),
        )
        .order_by(ReachGauge.id)
    ).all()
    river = reach.river.lower()
    rows = sorted(rows, key=lambda r: (not r[0].is_primary, river not in r[1].name.lower()))
    problem = "the reach has no USGS gauge with a runnable range"
    for link, gauge, lat, lon, area in rows:
        try:
            bands = Bands(link.metric, link.range_min, link.range_max)
        except ValueError:
            continue
        library = store.library(data_dir, gauge.source_id, area)
        if library is None:
            problem = f"gauge {gauge.source_id} has too little history to forecast"
            continue
        to_units = _identity
        if link.metric == "ft":
            rating = store.rating(data_dir, gauge.source_id)
            if rating is None:
                problem = f"gauge {gauge.source_id} has no stage rating for a range in feet"
                continue
            to_units = rating.to_ft
        current = live.current(session, gauge.id, now)
        if current is None:
            problem = f"gauge {gauge.source_id} has no reading in the last 48 hours"
            continue
        return Target(reach, gauge, bands, lat, lon, library, current, to_units, link.is_primary)
    raise HTTPException(409, problem)


def _identity(cfs):
    return np.asarray(cfs, float)


def _reach_info(reach: Reach) -> ReachInfo:
    return ReachInfo(
        id=reach.id,
        aw_id=reach.aw_id,
        river=reach.river,
        section=reach.section,
        difficulty=reach.difficulty_raw,
    )


def _gauge_info(target: Target) -> GaugeInfo:
    gauge = target.gauge
    return GaugeInfo(id=gauge.id, site=gauge.source_id, name=gauge.name, primary=target.primary)


def _bands_info(bands: Bands) -> BandsInfo:
    return BandsInfo(
        metric=bands.metric,
        minimum=bands.minimum,
        maximum=bands.maximum,
        edges=[float(e) if np.isfinite(e) else None for e in bands.edges()],
    )


def _now(target: Target) -> Now:
    state = target.current.state
    value = float(target.to_units(state.q))
    return Now(
        at=state.at,
        cfs=state.q,
        value=value,
        band=band_names.NAMES[int(target.bands.classify(value))],
        ft_observed=target.current.ft,
        api_mm=state.api_mm,
        rain_hours=target.current.rain_hours,
    )


def _quantiles(values) -> dict:
    """p10, p50, p90 keyword arguments from three values (or rows)."""
    return {
        name: value.tolist() if isinstance(value, np.ndarray) else float(value)
        for name, value in zip(("p10", "p50", "p90"), values, strict=True)
    }


def _observed(target: Target) -> Series:
    values = target.to_units(target.current.recent_cfs)
    return Series(
        start=target.current.recent_start,
        values=[float(v) if np.isfinite(v) else None for v in values],
    )


def _day_out(day: Day) -> DayOut:
    return DayOut(
        day=day.day,
        sunrise=day.sunrise,
        sunset=day.sunset,
        p_runnable=day.p_runnable,
        peak_bands=day.peak_bands,
        p_too_high_all_day=day.p_too_high_all_day,
        peak=Peak(**_quantiles(day.peak)),
        hours_in_range=day.hours_in_range,
    )


# --- endpoints -----------------------------------------------------------------------


@router.get("")
def list_reaches(session: SessionDep) -> list[ReachSummary]:
    """Every reach with its primary gauge and runnable range (no forecasts)."""
    rows = session.execute(
        select(
            Reach, Gauge.source_id, ReachGauge.metric, ReachGauge.range_min, ReachGauge.range_max
        )
        .outerjoin(ReachGauge, (ReachGauge.reach_id == Reach.id) & ReachGauge.is_primary)
        .outerjoin(Gauge, Gauge.id == ReachGauge.gauge_id)
        .order_by(Reach.river, Reach.section, ReachGauge.id)
    ).all()
    out: dict[int, ReachSummary] = {}
    for reach, site, metric, low, high in rows:
        if reach.id not in out:
            out[reach.id] = ReachSummary(
                **_reach_info(reach).model_dump(),
                states=reach.states,
                gauge_site=site,
                metric=metric,
                range_min=low,
                range_max=high,
            )
    return list(out.values())


@router.get("/{reach_id}/whatif")
def whatif(
    reach_id: int,
    session: SessionDep,
    data_dir: DataDirDep,
    now: NowDep,
    rain_in: Annotated[float, Query(ge=0, le=6)] = 1.0,
    storm: StormType = "typical",
    start_h: Annotated[int, Query(ge=0, le=MAX_START_H)] = 0,
) -> WhatIf:
    """The forecast if the basin gets `rain_in` inches, as a `storm` whose first wet hour
    ends `start_h + 1` hours after the latest reading."""
    target = _target(session, data_dir, reach_id, now)
    state = target.current.state
    if rain_in > 0:
        forecast = target.library.forecast(state, response.storm(rain_in, storm, start_h))
    else:
        forecast = response.no_rain(target.library, state)
    values = target.to_units(forecast.members)
    fan = np.stack(
        [analog.weighted_quantile(column, forecast.weights, LEVELS) for column in values.T], axis=1
    )
    days = daily(values, forecast.weights, forecast.start, target.bands, target.lat, target.lon)
    return WhatIf(
        reach=_reach_info(target.reach),
        gauge=_gauge_info(target),
        bands=_bands_info(target.bands),
        now=_now(target),
        observed=_observed(target),
        storm=StormInfo(
            rain_in=rain_in,
            type=storm,
            start_h=start_h,
            duration_h=response.STORM_TYPES[storm][0],
        ),
        forecast=Fan(start=forecast.start, **_quantiles(fan)),
        days=[_day_out(d) for d in days],
        analogs=[
            AnalogOut(
                start=a.start,
                rain_in=a.rain_mm / response.MM_PER_IN,
                duration_h=a.duration_h,
                q0_cfs=a.q0,
                qpeak_cfs=a.qpeak,
                t_peak_h=a.t_peak_h,
                weight=a.weight,
            )
            for a in forecast.analogs[:SHOWN_ANALOGS]
        ],
        extrapolated=forecast.extrapolated,
    )


@router.get("/{reach_id}/rain-response")
def rain_response(
    reach_id: int,
    session: SessionDep,
    data_dir: DataDirDep,
    now: NowDep,
    storm: StormType = "typical",
    start_h: Annotated[int, Query(ge=0, le=MAX_START_H)] = 0,
) -> RainResponse:
    """Chance of running each day and the peak, for every storm size from 0 to 4 in, with
    the NBM forecast's basin total through each day for comparison."""
    target = _target(session, data_dir, reach_id, now)
    curve = response.rain_response(
        target.library,
        target.current.state,
        target.bands,
        target.lat,
        target.lon,
        storm_type=storm,
        start_h=start_h,
        to_units=target.to_units,
    )
    outlook = live.rain_outlook(session, target.gauge.id, now)
    days = []
    for day in curve.days:
        forecast_rain = None
        if outlook is not None:
            sunset = sun_times(day.day, target.lat, target.lon)[1]
            if outlook.periods[-1][1] >= sunset:  # the forecast reaches this day's end
                totals = response.forecast_total(outlook.periods, now, sunset)
                forecast_rain = RainRange(**_quantiles(np.array(totals) / response.MM_PER_IN))
        days.append(
            DayCurveOut(
                day=day.day,
                p_runnable=day.p_runnable.tolist(),
                p_reach=day.p_reach.tolist(),
                p_too_high_all_day=day.p_too_high_all_day.tolist(),
                rain_needed_in=day.rain_needed_in,
                forecast_rain_in=forecast_rain,
            )
        )
    return RainResponse(
        reach=_reach_info(target.reach),
        gauge=_gauge_info(target),
        bands=_bands_info(target.bands),
        now=_now(target),
        storm_type=storm,
        start_h=start_h,
        rain_in=curve.rain_in.tolist(),
        peak_p10=curve.peak[:, 0].tolist(),
        peak_p50=curve.peak[:, 1].tolist(),
        peak_p90=curve.peak[:, 2].tolist(),
        days=days,
        forecast_issued_at=outlook.issued_at if outlook else None,
    )
