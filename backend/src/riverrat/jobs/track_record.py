"""How the Outlook's chances have held up (the Track record page).

    uv run python -m riverrat.jobs.track_record archive   # keep this morning's Outlook
    uv run python -m riverrat.jobs.track_record score     # score days that are over
    uv run python -m riverrat.jobs.track_record replay --start 2026-10-19 --end 2026-11-01

Each morning (the first run after ARCHIVE_HOUR_UTC, 6 am EDT) every gauged reach's
chance of running today and each day ahead is kept. Once a day is over, it's scored
against the gauge: was it in range at some daylight hour? The baseline is persistence:
the reach's band when the forecast was made, held. Estimates (reaches without a gauge)
have nothing to score against and aren't kept.

`replay` fills mornings before archiving began, recomputing each from what was known
then: the latest NBM run issued by that morning, the readings up to it, and only past
storms whose windows had ended. Replayed rows are marked; the page shows both.
"""

import argparse
import logging
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import numpy as np
from sqlalchemy import delete, exists, func, select
from sqlalchemy.orm import Session

from riverrat.config import get_settings
from riverrat.db.models import ForecastArchive, Gauge, Observation, Reach, ReachGauge, ReachOutlook
from riverrat.db.session import get_engine
from riverrat.features import hourly
from riverrat.forecast import live, outlook
from riverrat.forecast.daylight import sun_times
from riverrat.forecast.target import NoForecast, target
from riverrat.jobs import outlook as outlook_jobs
from riverrat.models import store

log = logging.getLogger(__name__)

ARCHIVE_HOUR_UTC = 10
MAX_OUTLOOK_AGE = timedelta(hours=3)  # an older Outlook isn't this morning's
SCORE_AFTER = timedelta(hours=2)  # after sunset, for late readings
GIVE_UP_AFTER = timedelta(days=3)  # a day still without readings isn't scored
MIN_DAYLIGHT_SEEN = 0.5  # share of daylight hours with a reading
OUT_OF_RANGE = ("too_low", "too_high")


def archive(session: Session, now: datetime) -> int:
    """Keep the current Outlook if it's this morning's and none is kept yet. Returns
    how many rows were added."""
    if now.hour < ARCHIVE_HOUR_UTC:
        return 0
    morning = datetime.combine(now.date(), time(ARCHIVE_HOUR_UTC), UTC)
    if session.scalar(
        select(
            exists().where(ForecastArchive.issued_at >= morning, ForecastArchive.replay.is_(False))
        )
    ):
        return 0
    outlooks = session.scalars(
        select(ReachOutlook).where(
            ReachOutlook.estimate.is_(None), ReachOutlook.computed_at >= now - MAX_OUTLOOK_AGE
        )
    ).all()
    rows = [row for found in outlooks for row in archive_rows(found, found.computed_at)]
    session.add_all(rows)
    session.commit()
    return len(rows)


def archive_rows(found: ReachOutlook, issued_at: datetime, replay: bool = False):
    persistence = found.now_band not in OUT_OF_RANGE
    issued = issued_at.astimezone(UTC).date()
    for day in found.days:
        d = date.fromisoformat(day["day"])
        if d < issued:
            continue
        yield ForecastArchive(
            reach_id=found.reach_id,
            issued_at=issued_at,
            day=d,
            lead_days=(d - issued).days,
            gauge_id=found.gauge_id,
            metric=found.metric,
            range_min=found.range_min,
            range_max=found.range_max,
            p_runnable=day["p_runnable"],
            persistence=persistence,
            replay=replay,
        )


@dataclass
class ScoreStats:
    scored: int = 0
    unscorable: int = 0
    waiting: int = 0


def score(session: Session, now: datetime) -> ScoreStats:
    """Score every kept forecast whose day is over."""
    stats = ScoreStats()
    pending = session.execute(
        select(ForecastArchive.gauge_id, ForecastArchive.metric)
        .where(ForecastArchive.scored_at.is_(None), ForecastArchive.day <= now.date())
        .distinct()
    ).all()
    for gauge_id, metric in pending:
        lat, lon = session.execute(
            select(func.ST_Y(Gauge.geom), func.ST_X(Gauge.geom)).where(Gauge.id == gauge_id)
        ).one()
        rows = session.scalars(
            select(ForecastArchive).where(
                ForecastArchive.gauge_id == gauge_id,
                ForecastArchive.metric == metric,
                ForecastArchive.scored_at.is_(None),
                ForecastArchive.day <= now.date(),
            )
        ).all()
        days = sorted({r.day for r in rows})
        start = datetime.combine(days[0], time(), UTC)
        end = datetime.combine(days[-1] + timedelta(days=1), time(), UTC)
        hours = hourly.hour_ends(start + hourly.HOUR, end)
        readings = session.execute(
            select(Observation.ts, Observation.value, Observation.qualifier).where(
                Observation.gauge_id == gauge_id,
                Observation.param == metric,
                Observation.ts > start,
                Observation.ts <= end,
            )
        ).all()
        flow = hourly.hourly_flow(
            hourly.readings_table(readings), hours, nonnegative=metric == "cfs"
        )
        times = hours.astype("datetime64[s]")
        for row in rows:
            sunrise, sunset = sun_times(row.day, lat, lon)
            if now < sunset + SCORE_AFTER:
                stats.waiting += 1
                continue
            daylight = (times >= _np(sunrise)) & (times <= _np(sunset))
            values = flow[daylight]
            seen = np.isfinite(values)
            if seen.mean() >= MIN_DAYLIGHT_SEEN:
                low = -np.inf if row.range_min is None else row.range_min
                high = np.inf if row.range_max is None else row.range_max
                row.observed = bool(np.any((values[seen] >= low) & (values[seen] <= high)))
                row.scored_at = now
                stats.scored += 1
            elif now > sunset + GIVE_UP_AFTER:
                row.scored_at = now
                stats.unscorable += 1
            else:
                stats.waiting += 1
        session.commit()
    return stats


def _np(at: datetime) -> np.datetime64:
    return np.datetime64(at.astimezone(UTC).replace(tzinfo=None), "s")


def replay(session: Session, *, data_dir: Path, start: date, end: date) -> int:
    """Recompute and keep the mornings from `start` through `end` (replacing earlier
    replays of them). Returns how many rows were kept."""
    mornings = [
        datetime.combine(start + timedelta(days=n), time(ARCHIVE_HOUR_UTC), UTC)
        for n in range((end - start).days + 1)
    ]
    session.execute(
        delete(ForecastArchive).where(
            ForecastArchive.replay, ForecastArchive.issued_at.in_(mornings)
        )
    )
    session.commit()
    reaches = session.scalars(
        select(Reach).where(exists().where(ReachGauge.reach_id == Reach.id)).order_by(Reach.id)
    ).all()
    recessions: dict[str, object] = {}  # fitted once, on flows before the first morning
    kept = 0
    for morning in mornings:
        ensembles: dict[int, tuple | None] = {}
        rows = []
        for reach in reaches:
            try:
                found = target(session, data_dir, reach, morning)
            except NoForecast:
                continue
            gauge = found.gauge
            if gauge.id not in ensembles:
                ensembles[gauge.id] = _replay_ensemble(
                    session, data_dir, found, morning, mornings[0], recessions
                )
            if ensembles[gauge.id] is None:
                continue
            library, ensemble, rain = ensembles[gauge.id]
            row = outlook_jobs._row(replace(found, library=library), ensemble, rain, morning)
            rows.extend(archive_rows(row, morning, replay=True))
        session.add_all(rows)
        session.commit()
        kept += len(rows)
        log.info("Replayed %s: %d reach-days", morning.date(), len(rows))
    return kept


def _replay_ensemble(session, data_dir, found, morning, first, recessions):
    site = found.gauge.source_id
    area = found.library.area_sqmi
    if site not in recessions:
        earliest = store.library_before(data_dir, site, area, first)
        recessions[site] = earliest.recession if earliest else None
    library = store.library_before(data_dir, site, area, morning, recessions[site])
    rain = live.rain_outlook(session, found.gauge.id, morning)
    if library is None or rain is None:
        return None
    return library, outlook.ensemble(library, found.current.state, rain), rain


def main() -> int:
    parser = argparse.ArgumentParser(description="Keep and score the Outlook's forecasts")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("archive", help="Keep this morning's Outlook")
    sub.add_parser("score", help="Score kept forecasts whose day is over")
    again = sub.add_parser("replay", help="Recompute past mornings from what was known then")
    again.add_argument("--start", type=date.fromisoformat, required=True)
    again.add_argument("--end", type=date.fromisoformat, required=True)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    now = datetime.now(UTC)
    with Session(get_engine()) as session:
        if args.command == "archive":
            log.info("Archived %d reach-days", archive(session, now))
        elif args.command == "score":
            log.info("Score: %s", score(session, now))
        else:
            data_dir = get_settings().data_dir
            log.info(
                "Replay: %d reach-days",
                replay(session, data_dir=data_dir, start=args.start, end=args.end),
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
