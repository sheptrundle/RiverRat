"""Which gauge forecasts a reach, and everything needed to do it.

A reach is forecast through its gauge: the primary USGS gauge with a usable runnable
range. When the primary can't be used (often an AW "calculated" gauge), another of the
reach's USGS gauges stands in, preferring one on the same river. Flows are modeled in
cfs and reported in the range's units, through the gauge's stage rating for reaches
rated in feet.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, aliased

from riverrat.db.models import Basin, Gauge, Reach, ReachGauge
from riverrat.forecast import live
from riverrat.forecast.bands import Bands
from riverrat.models import analog, store

# A gauge whose flow record ended this long ago has been discontinued.
DISCONTINUED_AFTER = timedelta(days=60)


class NoForecast(Exception):
    """The reach can't be forecast; the message says why."""


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


def _identity(cfs):
    return np.asarray(cfs, float)
