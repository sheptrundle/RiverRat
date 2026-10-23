"""Stage ratings for reaches whose runnable range is in feet (forecast/rating.py).

    uv run python -m riverrat.jobs.ratings          # data/ratings/ratings.json

Fits each gauge's rating from its last two years of hourly flow and stage: the
downloaded history (data/usgs/iv) plus the live readings in Postgres, which keep the
30-day offset current after the history ends. The worker refits daily.
"""

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from riverrat.config import get_settings
from riverrat.db.models import Gauge, Observation, ReachGauge
from riverrat.db.session import get_engine
from riverrat.features import hourly
from riverrat.forecast.rating import FIT_DAYS, Rating
from riverrat.jobs import usgs as usgs_jobs
from riverrat.storage import write_text

log = logging.getLogger(__name__)


def ratings_path(data_dir: Path) -> Path:
    return data_dir / "ratings" / "ratings.json"


@dataclass
class RatingStats:
    gauges: int = 0
    fitted: int = 0


def fit_all(
    session: Session, *, data_dir: Path, now: datetime | None = None, sites=None
) -> RatingStats:
    """Refit every USGS gauge that some reach rates in feet, replacing the ratings file."""
    # Gauges a reach rates in feet, directly or as an NWS location's USGS twin.
    linked = aliased(Gauge)
    query = (
        select(Gauge.id, Gauge.source_id)
        .join(linked, (linked.id == Gauge.id) | (linked.same_as_id == Gauge.id))
        .join(ReachGauge, ReachGauge.gauge_id == linked.id)
        .where(Gauge.source == "USGS", ReachGauge.metric == "ft")
        .distinct()
        .order_by(Gauge.source_id)
    )
    if sites:
        query = query.where(Gauge.source_id.in_(sites))
    gauges = session.execute(query).all()

    end = hourly.latest_hour(now or datetime.now(UTC))
    hours = hourly.hour_ends(end - timedelta(days=FIT_DAYS) + hourly.HOUR, end)
    stats = RatingStats(gauges=len(gauges))
    path = ratings_path(data_dir)
    # Refitting a few gauges keeps the others' ratings.
    kept = json.loads(path.read_text()) if sites and path.exists() else {}
    fitted = {}
    for gauge_id, site in gauges:
        cfs, ft = (
            hourly.hourly_flow(_readings(session, data_dir, gauge_id, site, unit, hours), hours)
            for unit in ("cfs", "ft")
        )
        rating = Rating.fit(cfs.astype(float), ft.astype(float))
        if rating is not None:
            fitted[site] = rating.to_dict() | {"fitted_at": end.isoformat()}
    session.rollback()
    stats.fitted = len(fitted)
    write_text(path, json.dumps({k: v for k, v in kept.items() if k not in sites} | fitted))
    return stats


def _readings(
    session: Session, data_dir: Path, gauge_id: int, site: str, unit: str, hours: np.ndarray
) -> pa.Table:
    first_year, last_year = int(str(hours[0])[:4]), int(str(hours[-1])[:4])
    tables = [
        pq.read_table(path, columns=["ts", "value", "qualifier"]).cast(
            hourly.readings_table([]).schema
        )
        for year in range(first_year, last_year + 1)
        if (path := usgs_jobs.history_path(data_dir, unit, site, year)).exists()
    ]
    since = hours[0].astype("datetime64[s]").item().replace(tzinfo=UTC) - hourly.HOUR
    live = session.execute(
        select(Observation.ts, Observation.value, Observation.qualifier).where(
            Observation.gauge_id == gauge_id, Observation.param == unit, Observation.ts > since
        )
    ).all()
    return pa.concat_tables([*tables, hourly.readings_table(live)])


def main() -> int:
    parser = argparse.ArgumentParser(description="Fit stage ratings for reaches rated in feet")
    parser.add_argument("--gauge", action="append", help="Only this USGS site number")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with Session(get_engine()) as session:
        stats = fit_all(session, data_dir=get_settings().data_dir, sites=args.gauge)
    log.info("Ratings: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
