"""Compute the Outlook for every reach (DESIGN.md §6.3).

    uv run python -m riverrat.jobs.outlook     # replaces the reach_outlook table

Each forecastable reach gets its gauge's ensemble under the latest NBM rain forecast
(forecast/outlook.py) turned into daily products in the reach's bands. Reaches sharing
a gauge share its ensemble. The worker reruns this hourly, after the rain forecasts.
"""

import argparse
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from riverrat.config import get_settings
from riverrat.db.models import Gauge, Reach, ReachGauge, ReachOutlook
from riverrat.db.session import get_engine
from riverrat.forecast import bands as band_names
from riverrat.forecast import live, outlook
from riverrat.forecast.daily import daily
from riverrat.forecast.daylight import sun_times
from riverrat.forecast.response import MM_PER_IN, forecast_total
from riverrat.forecast.target import NoForecast, Target, target

log = logging.getLogger(__name__)


@dataclass
class OutlookStats:
    reaches: int = 0
    gauges: int = 0
    skipped: Counter = field(default_factory=Counter)  # why reaches couldn't be forecast


def compute(session: Session, *, data_dir: Path, now: datetime) -> OutlookStats:
    stats = OutlookStats()
    # By primary gauge, so each gauge's storm library loads once.
    reaches = (
        session.scalars(
            select(Reach)
            .outerjoin(ReachGauge, (ReachGauge.reach_id == Reach.id) & ReachGauge.is_primary)
            .outerjoin(Gauge, Gauge.id == ReachGauge.gauge_id)
            .order_by(Gauge.source_id, Reach.id)
        )
        .unique()
        .all()
    )
    ensembles: dict[int, tuple[outlook.Ensemble, live.RainOutlook | None]] = {}
    rows = []
    for reach in reaches:
        try:
            found = target(session, data_dir, reach, now)
        except NoForecast as problem:
            stats.skipped[_reason(str(problem))] += 1
            continue
        if found.gauge.id not in ensembles:
            rain = live.rain_outlook(session, found.gauge.id, now)
            ensembles[found.gauge.id] = (
                outlook.ensemble(found.library, found.current.state, rain),
                rain,
            )
        ensemble, rain = ensembles[found.gauge.id]
        rows.append(_row(found, ensemble, rain, now))
    session.execute(delete(ReachOutlook))
    session.add_all(rows)
    session.commit()
    stats.reaches, stats.gauges = len(rows), len(ensembles)
    return stats


def _row(found: Target, ensemble: outlook.Ensemble, rain, now: datetime) -> ReachOutlook:
    values = found.to_units(ensemble.members)
    days = []
    for day in daily(values, ensemble.weights, ensemble.start, found.bands, found.lat, found.lon):
        sunset = sun_times(day.day, found.lat, found.lon)[1]
        rain_in = None
        if rain is not None and rain.periods[-1][1] >= sunset:
            p10, p50, p90 = (v / MM_PER_IN for v in forecast_total(rain.periods, now, sunset))
            rain_in = {"p10": round(p10, 2), "p50": round(p50, 2), "p90": round(p90, 2)}
        days.append(
            {
                "day": day.day.isoformat(),
                "p_runnable": round(day.p_runnable, 3),
                "peak_bands": {name: round(p, 3) for name, p in day.peak_bands.items()},
                "p_too_high_all_day": round(day.p_too_high_all_day, 3),
                "peak": {k: _sig(v) for k, v in zip(("p10", "p50", "p90"), day.peak, strict=True)},
                "hours_in_range": day.hours_in_range,
                "rain_in": rain_in,
            }
        )
    state = found.current.state
    value = float(found.to_units(state.q))
    return ReachOutlook(
        reach_id=found.reach.id,
        gauge_id=found.gauge.id,
        primary=found.primary,
        computed_at=now,
        as_of=state.at,
        rain_issued_at=rain.issued_at if rain else None,
        metric=found.bands.metric,
        now_value=value,
        now_band=band_names.NAMES[int(np.asarray(found.bands.classify(value)))],
        extrapolated=ensemble.extrapolated,
        days=days,
    )


def _sig(value: float, digits: int = 3) -> float:
    """`value` to `digits` significant figures (flows don't need more)."""
    return float(f"{value:.{digits}g}")


def _reason(problem: str) -> str:
    """The kind of problem, without the gauge number."""
    return problem.split(" has ", 1)[-1] if problem.startswith("gauge ") else problem


def main() -> int:
    argparse.ArgumentParser(description="Compute the Outlook for every reach").parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    started = time.monotonic()
    with Session(get_engine()) as session:
        stats = compute(session, data_dir=get_settings().data_dir, now=datetime.now(UTC))
    log.info(
        "Outlook: %d reaches through %d gauges in %.0f s",
        stats.reaches,
        stats.gauges,
        time.monotonic() - started,
    )
    for reason, count in stats.skipped.most_common():
        log.info("  not forecast (%d): %s", count, reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
