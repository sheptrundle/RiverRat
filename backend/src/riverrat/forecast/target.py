"""Which gauge forecasts a reach, and everything needed to do it.

A reach is forecast through its gauge: the primary USGS gauge with a usable runnable
range. When the primary can't be used (often an AW "calculated" gauge), another of the
reach's USGS gauges stands in, preferring one on the same river. Flows are modeled in
cfs and reported in the range's units, through the gauge's stage rating for reaches
rated in feet.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from riverrat.db.models import Basin, Gauge, Reach, ReachGauge
from riverrat.forecast import live
from riverrat.forecast.bands import Bands
from riverrat.models import analog, store


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
    rows = session.execute(
        select(ReachGauge, Gauge, func.ST_Y(Gauge.geom), func.ST_X(Gauge.geom), Basin.area_sqmi)
        .join(Gauge, Gauge.id == ReachGauge.gauge_id)
        .outerjoin(Basin, Basin.gauge_id == Gauge.id)
        .where(
            ReachGauge.reach_id == reach.id,
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
    raise NoForecast(problem)


def _identity(cfs):
    return np.asarray(cfs, float)
