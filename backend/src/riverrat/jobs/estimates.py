"""Fit the range model for reaches without a gauge (forecast/estimate.py).

    uv run python -m riverrat.jobs.estimates     # -> data/estimates/range_model.json

From every gauged reach whose range is in cfs on its primary USGS gauge with a basin.
Rerun after syncing reaches or adding basins; it takes a second.
"""

import argparse
import json
import logging

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from riverrat.config import get_settings
from riverrat.db.models import Basin, Gauge, Reach, ReachGauge
from riverrat.db.session import get_engine
from riverrat.forecast.estimate import RangeModel, range_model_path
from riverrat.storage import write_text

log = logging.getLogger(__name__)


def fit(session: Session) -> RangeModel | None:
    rows = session.execute(
        select(Basin.area_sqmi, Reach.gradient_fpm, ReachGauge.range_min, ReachGauge.range_max)
        .join(ReachGauge, ReachGauge.reach_id == Reach.id)
        .join(Gauge, Gauge.id == ReachGauge.gauge_id)
        .join(Basin, Basin.gauge_id == Gauge.id)
        .where(
            Gauge.source == "USGS",
            ReachGauge.is_primary,
            ReachGauge.metric == "cfs",
            ReachGauge.range_min > 0,
            ReachGauge.range_max > ReachGauge.range_min,
            Basin.area_sqmi > 0,
        )
    ).all()
    session.rollback()
    if not rows:
        return None
    area, gradient, low, high = (np.array(column, float) for column in zip(*rows, strict=True))
    return RangeModel.fit(area, gradient, low, high)


def main() -> int:
    argparse.ArgumentParser(description="Fit the range model for ungauged reaches").parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with Session(get_engine()) as session:
        model = fit(session)
    if model is None:
        log.error("Too few gauged reaches to fit")
        return 1
    write_text(range_model_path(get_settings().data_dir), json.dumps(model.to_dict()))
    log.info("Range model: %s", model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
