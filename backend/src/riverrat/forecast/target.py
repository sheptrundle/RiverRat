"""Which gauge forecasts a reach, and everything needed to do it.

A reach is forecast through its gauge: the primary USGS gauge with a usable runnable
range. When the primary can't be used (often an AW "calculated" gauge), another of the
reach's USGS gauges stands in, preferring one on the same river. Flows are modeled in
cfs and reported in the range's units, through the gauge's stage rating for reaches
rated in feet.

A reach with no gauge at all but a mapped watershed (reach_basin) is estimated instead:
through a similar gauge nearby, scaled by watershed area, against a range estimated from
the watershed's size and gradient (forecast/estimate.py).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, aliased

from riverrat.db.models import Basin, Gauge, Reach, ReachBasin, ReachGauge
from riverrat.forecast import estimate, live
from riverrat.forecast.bands import Bands
from riverrat.models import analog, store

# A gauge whose flow record ended this long ago has been discontinued.
DISCONTINUED_AFTER = timedelta(days=60)


class NoForecast(Exception):
    """The reach can't be forecast; the message says why."""


@dataclass(frozen=True)
class Estimate:
    """How a reach without a gauge is estimated."""

    area_sqmi: float  # the reach's watershed
    donor_area_sqmi: float
    distance_km: float  # between the watersheds' centers


@dataclass(frozen=True)
class Target:
    reach: Reach
    gauge: Gauge
    bands: Bands
    lat: float
    lon: float
    library: analog.AnalogLibrary
    current: live.Current
    to_units: Callable  # cfs → the range's units
    primary: bool  # False: standing in for AW's primary gauge
    estimate: Estimate | None = None  # a reach without a gauge, through a donor gauge
    # For estimates: factors on flow, one per plausible range, each judged against `bands`
    spread: np.ndarray | None = None

    def values(self, members: np.ndarray, weights: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Forecast members (cfs) in the range's units, with their weights. An estimate
        repeats each member once per plausible range."""
        values = self.to_units(members)
        if self.spread is None:
            return values, weights
        n = len(self.spread)
        values = (values[None] * self.spread[:, None, None]).reshape(-1, values.shape[-1])
        return values, np.tile(weights, n) / n


def target(session: Session, data_dir: Path, reach: Reach, now: datetime) -> Target:
    # Each linked gauge, or for an NWS location its USGS twin at the same place.
    linked = aliased(Gauge)
    gauge = aliased(Gauge)
    rows = session.execute(
        select(ReachGauge, gauge, func.ST_Y(gauge.geom), func.ST_X(gauge.geom), Basin.area_sqmi)
        .join(linked, linked.id == ReachGauge.gauge_id)
        .join(
            gauge,
            gauge.id == case((linked.source == "USGS", linked.id), else_=linked.same_as_id),
        )
        .outerjoin(Basin, Basin.gauge_id == gauge.id)
        .where(
            ReachGauge.reach_id == reach.id,
            gauge.source == "USGS",
            gauge.geom.is_not(None),
            ReachGauge.metric.in_(("cfs", "ft")),
        )
        .order_by(ReachGauge.id)
    ).all()
    river = reach.river.lower()
    rows = sorted(rows, key=lambda r: (not r[0].is_primary, river not in r[1].name.lower()))
    problem = "the reach has no USGS gauge with a runnable range"
    if not rows and not session.scalar(
        select(ReachGauge.id).where(ReachGauge.reach_id == reach.id).limit(1)
    ):
        return _estimate(session, data_dir, reach, now)
    if not rows:
        calculated = session.scalars(
            select(Gauge.name)
            .join(ReachGauge, ReachGauge.gauge_id == Gauge.id)
            .where(ReachGauge.reach_id == reach.id, Gauge.source == "CALCULATED")
        ).first()
        if calculated:
            problem = (
                f"its gauge is American Whitewater's calculated \"{calculated}\", usually a dam's "
                "release; River Rat doesn't forecast dam-controlled flows yet"
            )
    for link, gauge, lat, lon, area in rows:
        try:
            bands = Bands(link.metric, link.range_min, link.range_max)
        except ValueError:
            continue
        if gauge.cfs_end is not None and gauge.cfs_end < now - DISCONTINUED_AFTER:
            problem = (
                f"gauge {gauge.source_id} stopped reporting flow in {gauge.cfs_end.year} "
                "(USGS discontinued it)"
            )
            continue
        library = store.library(data_dir, gauge.source_id, area)
        if library is None:
            if gauge.cfs_begin is None:
                problem = f"gauge {gauge.source_id} has never published continuous flow data"
            else:
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
            if live.reports_stage(session, gauge.id, now):
                problem = (
                    f"gauge {gauge.source_id} is reporting stage only right now "
                    "(USGS isn't publishing its flow)"
                )
            else:
                problem = (
                    f"gauge {gauge.source_id} has no reading in the last 48 hours (it may be down)"
                )
            continue
        return Target(reach, gauge, bands, lat, lon, library, current, to_units, link.is_primary)
    raise NoForecast(problem)


def _estimate(session: Session, data_dir: Path, reach: Reach, now: datetime) -> Target:
    """Through the nearest usable gauge whose basin is within DONOR_RATIO of the size."""
    basin = session.get(ReachBasin, reach.id)
    model = store.range_model(data_dir)
    if basin is None or model is None:
        raise NoForecast("the reach has no gauge")
    if estimate.snapped_downstream(basin.area_sqmi, basin.putin_area_sqmi):
        raise NoForecast("the reach has no gauge, and its watershed couldn't be mapped reliably")
    centre = (
        select(func.ST_Centroid(func.Geography(ReachBasin.geom)))
        .where(ReachBasin.reach_id == reach.id)
        .scalar_subquery()
    )
    distance = func.ST_Distance(func.Geography(func.ST_Centroid(Basin.geom)), centre)
    donors = session.execute(
        select(Gauge, Basin.area_sqmi, distance, func.ST_Y(Gauge.geom), func.ST_X(Gauge.geom))
        .join(Basin, Basin.gauge_id == Gauge.id)
        .where(
            Gauge.source == "USGS",
            Gauge.geom.is_not(None),
            Basin.area_sqmi.between(
                basin.area_sqmi / estimate.DONOR_RATIO, basin.area_sqmi * estimate.DONOR_RATIO
            ),
            distance <= estimate.DONOR_MAX_KM * 1000,
        )
        .order_by(distance)
    ).all()
    for gauge, area, meters, lat, lon in donors:
        if gauge.cfs_end is not None and gauge.cfs_end < now - DISCONTINUED_AFTER:
            continue
        library = store.library(data_dir, gauge.source_id, area)
        if library is None:
            continue
        current = live.current(session, gauge.id, now)
        if current is None:
            continue
        minimum, strays = model.minimum(basin.area_sqmi, reach.gradient_fpm)
        scale = basin.area_sqmi / area
        return Target(
            reach,
            gauge,
            Bands("cfs", minimum, minimum * model.ratio),
            lat,
            lon,
            library,
            current,
            lambda cfs, scale=scale: np.asarray(cfs, float) * scale,
            False,
            Estimate(basin.area_sqmi, area, meters / 1000),
            1 / strays,  # a member q is in a range strayed by f when q / f is in the fit
        )
    raise NoForecast(
        f"the reach has no gauge, and no similar gauge within {estimate.DONOR_MAX_KM:.0f} km"
    )


def _identity(cfs):
    return np.asarray(cfs, float)
