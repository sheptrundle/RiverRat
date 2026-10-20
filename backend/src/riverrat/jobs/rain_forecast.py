"""Archive forecast basin rain from NBM (DESIGN.md §4.4, §6.7).

    uv run python -m riverrat.jobs.rain_forecast archive                     # last 2 days
    uv run python -m riverrat.jobs.rain_forecast archive --since 2026-10-01  # catch up

For each 00/06/12/18Z run not yet stored: the 5th–95th percentiles of every 6-hour rain
total out to 6 days, averaged over each gauge's basin, into Postgres (`rain_forecast`).
A run is stored only once NBM has published all of it, and in one transaction, so a
failed run leaves nothing behind and is retried next time. The worker runs this hourly.
"""

import argparse
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx2
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from riverrat.config import get_settings
from riverrat.db.models import RainForecast, RainForecastRun
from riverrat.db.session import get_engine
from riverrat.geo.grid import CellWeights, basin_mean, cell_weights
from riverrat.http import PoliteClient
from riverrat.jobs import rain as rain_jobs
from riverrat.sources import grib, nbm

log = logging.getLogger(__name__)

NBM = "nbm"
LOOKBACK = timedelta(days=2)


class NotPublished(Exception):
    """The run isn't (fully) on the bucket yet."""


@dataclass
class ForecastStats:
    runs: int = 0
    not_published: int = 0
    failed: int = 0
    remaining: int = 0


def runs_between(since: datetime, now: datetime) -> list[datetime]:
    """NBM long-range run times in [since, now], newest first."""
    hour = since.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    if hour < since:
        hour += timedelta(hours=1)
    runs = []
    while hour <= now:
        if hour.hour in nbm.CYCLES:
            runs.append(hour)
        hour += timedelta(hours=1)
    return runs[::-1]


def archive(
    client: PoliteClient,
    session: Session,
    *,
    now: datetime | None = None,
    since: datetime | None = None,
    max_runs: int | None = None,
    workers: int = 8,
) -> ForecastStats:
    """Newest first; `max_runs` caps the runs stored (unpublished ones don't count)."""
    stats = ForecastStats()
    now = now or datetime.now(UTC)
    since = since or now - LOOKBACK
    basins = rain_jobs.basin_rows(session)
    stored = set(
        session.scalars(
            select(RainForecastRun.issued_at).where(
                RainForecastRun.source == NBM, RainForecastRun.issued_at >= since
            )
        )
    )
    session.rollback()  # no transaction open during the downloads
    todo = [issued for issued in runs_between(since, now) if issued not in stored]

    weights: tuple[grib.Grid, list[CellWeights]] | None = None
    geoms = [geom for _, _, geom in basins]
    with ThreadPoolExecutor(workers) as pool:
        for n, issued in enumerate(todo):
            if max_runs is not None and stats.runs == max_runs:
                stats.remaining = len(todo) - n
                break
            try:
                _require_published(client, issued)
                rows = []
                for lead in nbm.leads():
                    messages = _fetch_period(client, pool, issued, lead)
                    grid = messages[0].grid
                    if weights is None or not weights[0].same_as(grid):
                        weights = (grid, _grid_weights(grid, geoms))
                    rows.extend(_period_rows(issued, lead, messages, basins, weights[1]))
            except NotPublished:
                stats.not_published += 1
                continue
            except (httpx2.HTTPError, grib.GribFormatError, ValueError) as exc:
                stats.failed += 1
                log.warning("NBM %s failed: %s", issued.isoformat(), exc)
                continue
            _store_run(session, issued, rows)
            stats.runs += 1
            log.info("NBM %s stored", issued.isoformat())
    return stats


def _require_published(client: PoliteClient, issued: datetime) -> None:
    """NBM uploads a run's files in order of lead time; the last one means it's all
    there."""
    try:
        client.head(nbm.qmd_url(issued, nbm.HORIZON_H) + ".idx")
    except httpx2.HTTPStatusError as exc:
        if exc.response.status_code in (403, 404):  # S3 says 403 for absent keys
            raise NotPublished from exc
        raise


def _fetch_period(
    client: PoliteClient, pool: ThreadPoolExecutor, issued: datetime, lead: int
) -> list[grib.Message]:
    url = nbm.qmd_url(issued, lead)
    ranges = nbm.percentile_ranges(client.get(url + ".idx").text, lead)
    raw = pool.map(lambda r: client.get(url, headers=r.header()).content, ranges)
    messages = [grib.decode(body) for body in raw]
    for wanted, message in zip(ranges, messages, strict=True):
        expected = (issued, issued + timedelta(hours=lead - nbm.PERIOD_H), wanted.percentile)
        if (message.issued, message.start, message.percentile) != expected:
            raise grib.GribFormatError(
                f"f{lead:03d}: got {message.issued} {message.start} p{message.percentile}"
            )
    return messages


def _grid_weights(grid: grib.Grid, geoms: list) -> list[CellWeights]:
    return [cell_weights(grid.project(g), grid.y, grid.x, geographic=False) for g in geoms]


def _period_rows(issued, lead, messages, basins, weights) -> list[dict]:
    valid_end = issued + timedelta(hours=lead)
    rows = []
    for (gauge_id, _, _), cells in zip(basins, weights, strict=True):
        row = {"gauge_id": gauge_id, "source": NBM, "issued_at": issued, "valid_end": valid_end}
        for message in messages:
            row[f"p{message.percentile:02d}"] = basin_mean(
                message.mm, cells, rain_jobs.MIN_COVERAGE
            )
        rows.append(row)
    return rows


def _store_run(session: Session, issued: datetime, rows: list[dict]) -> None:
    columns = [f"p{p:02d}" for p in nbm.PERCENTILES]
    for start in range(0, len(rows), 2000):
        stmt = insert(RainForecast).values(rows[start : start + 2000])
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=["gauge_id", "source", "issued_at", "valid_end"],
                set_={c: stmt.excluded[c] for c in columns},
            )
        )
    stmt = insert(RainForecastRun).values(
        source=NBM, issued_at=issued, fetched_at=datetime.now(UTC)
    )
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["source", "issued_at"], set_={"fetched_at": stmt.excluded.fetched_at}
        )
    )
    session.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive forecast basin rain")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("archive", help="Store NBM runs not yet archived")
    run.add_argument(
        "--since",
        type=lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC),
        help="Oldest run to consider (default: 2 days ago)",
    )
    run.add_argument("--max-runs", type=int, help="Store at most this many runs")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    settings = get_settings()
    user_agent = f"RiverRat/0.1 (+{settings.scraper_contact})"
    # AWS Open Data serves anonymous parallel reads; no throttle needed.
    with PoliteClient(user_agent, 0, timeout=120) as client, Session(get_engine()) as session:
        stats = archive(client, session, since=args.since, max_runs=args.max_runs)
        log.info("Rain forecasts: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
