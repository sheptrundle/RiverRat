"""USGS gauge jobs (DESIGN.md §4.2, §6.6).

    uv run python -m riverrat.jobs.usgs sites       # site metadata + periods of record
    uv run python -m riverrat.jobs.usgs live        # last few hours of 15-min data -> Postgres
    uv run python -m riverrat.jobs.usgs backfill    # 15-min history -> data/usgs/iv/*.parquet

`sites` must run first: `live` and `backfill` rely on locations and periods of record.
"""

import argparse
import logging
import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from riverrat.config import get_settings
from riverrat.db.models import Gauge, Observation, ReachGauge
from riverrat.db.session import get_engine
from riverrat.http import PoliteClient
from riverrat.sources import usgs

log = logging.getLogger(__name__)

HISTORY_SCHEMA = pa.schema(
    [
        ("ts", pa.timestamp("us", tz="UTC")),
        ("value", pa.float64()),
        ("approved", pa.bool_()),
        ("qualifier", pa.string()),
    ]
)


def _now() -> datetime:
    return datetime.now(UTC)


def _chunks(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


# --- sites ---------------------------------------------------------------------------


def sync_sites(
    client: PoliteClient, session: Session, *, max_age: timedelta = timedelta(days=30)
) -> int:
    """Fill in location, drainage area, and 15-minute periods of record for USGS gauges
    whose metadata is missing or older than `max_age`."""
    gauges = session.scalars(
        select(Gauge).where(
            Gauge.source == "USGS",
            or_(Gauge.metadata_synced_at.is_(None), Gauge.metadata_synced_at < _now() - max_age),
        )
    ).all()
    by_number = {g.source_id: g for g in gauges}
    missing = 0
    for chunk in _chunks(sorted(by_number), 100):
        ids = ",".join(usgs.site_id(n) for n in chunk)
        found = set()
        for feature in usgs.features(
            client, usgs.items_url("monitoring-locations", id=ids, f="json", limit=500)
        ):
            site = usgs.parse_site(feature)
            gauge = by_number.get(site.number)
            if gauge is None:
                continue
            found.add(site.number)
            gauge.geom = f"SRID=4326;POINT({site.lon} {site.lat})"
            gauge.agency_name = site.name
            gauge.drainage_area_sqmi = site.drainage_area_sqmi
            gauge.state_code = site.state_code
            gauge.huc = site.huc

        for number in chunk:
            gauge = by_number[number]
            gauge.cfs_begin = gauge.cfs_end = gauge.ft_begin = gauge.ft_end = None
        series_url = usgs.items_url(
            "time-series-metadata",
            monitoring_location_id=ids,
            parameter_code=",".join(usgs.PARAMS),
            statistic_id=usgs.INSTANTANEOUS,
            f="json",
            limit=1000,
        )
        for feature in usgs.features(client, series_url):
            series = usgs.parse_series(feature)
            if series is None or series.number not in by_number:
                continue
            _widen_period(by_number[series.number], series)

        now = _now()
        for number in chunk:
            by_number[number].metadata_synced_at = now
        missing += len(set(chunk) - found)
        session.commit()
    if missing:
        log.warning("%d gauges were not found in USGS monitoring locations", missing)
    return len(by_number)


def _widen_period(gauge: Gauge, series: usgs.Series) -> None:
    """A site can have several 15-minute series per parameter (e.g. after an equipment
    change); record the overall span."""
    begin_attr, end_attr = f"{series.unit}_begin", f"{series.unit}_end"
    begin, end = getattr(gauge, begin_attr), getattr(gauge, end_attr)
    if series.begin and (begin is None or series.begin < begin):
        setattr(gauge, begin_attr, series.begin)
    if series.end and (end is None or series.end > end):
        setattr(gauge, end_attr, series.end)


# --- live ----------------------------------------------------------------------------


@dataclass
class LiveStats:
    tiles: int = 0
    stored: int = 0
    pruned: int = 0


def ingest_live(
    client: PoliteClient,
    session: Session,
    *,
    hours: float = 3,
    tile_deg: float = 3.0,
    retention: timedelta = timedelta(days=60),
) -> LiveStats:
    """Store the last `hours` of 15-minute readings for active USGS gauges.

    `continuous` takes one site per request but accepts a bbox, so we query a few tiles
    covering our gauges instead of hundreds of single-site requests."""
    stats = LiveStats()
    now = _now()
    active_since = now - timedelta(days=30)
    rows = session.execute(
        select(Gauge.id, Gauge.source_id, func.ST_X(Gauge.geom), func.ST_Y(Gauge.geom)).where(
            Gauge.source == "USGS",
            Gauge.geom.is_not(None),
            or_(Gauge.cfs_end > active_since, Gauge.ft_end > active_since),
        )
    ).all()
    gauge_ids = {number: gauge_id for gauge_id, number, _, _ in rows}
    tiles = {(math.floor(lon / tile_deg), math.floor(lat / tile_deg)) for _, _, lon, lat in rows}

    window = f"{usgs.iso(now - timedelta(hours=hours))}/{usgs.iso(now)}"
    for tx, ty in sorted(tiles):
        bbox = f"{tx * tile_deg},{ty * tile_deg},{(tx + 1) * tile_deg},{(ty + 1) * tile_deg}"
        url = usgs.items_url(
            "continuous",
            bbox=bbox,
            parameter_code=",".join(usgs.PARAMS),
            time=window,
            f="json",
            limit=10_000,
            skipGeometry="true",
        )
        observations = (usgs.parse_observation(f["properties"]) for f in usgs.features(client, url))
        stats.stored += _upsert_observations(
            session, (o for o in observations if o and o.number in gauge_ids), gauge_ids
        )
        stats.tiles += 1
    stats.pruned = session.execute(
        delete(Observation).where(Observation.ts < now - retention)
    ).rowcount
    session.commit()
    return stats


def _upsert_observations(
    session: Session, observations: Iterable[usgs.Observation], gauge_ids: dict[str, int]
) -> int:
    rows = {
        (gauge_ids[o.number], o.unit, o.ts): {
            "gauge_id": gauge_ids[o.number],
            "param": o.unit,
            "ts": o.ts,
            "value": o.value,
            "approved": o.approved,
            "qualifier": o.qualifier,
        }
        for o in observations
    }
    for chunk in _chunks(list(rows.values()), 5000):
        stmt = insert(Observation).values(chunk)
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=[Observation.gauge_id, Observation.param, Observation.ts],
                set_={c: stmt.excluded[c] for c in ("value", "approved", "qualifier")},
            )
        )
    return len(rows)


# --- backfill ------------------------------------------------------------------------


@dataclass
class BackfillStats:
    gauges: int = 0
    files_written: int = 0
    files_skipped: int = 0
    rows: int = 0


def history_path(data_dir: Path, unit: str, number: str, year: int) -> Path:
    return data_dir / "usgs" / "iv" / unit / number / f"{year}.parquet"


def backfill(
    client: PoliteClient,
    session: Session,
    *,
    data_dir: Path,
    start_year: int,
    numbers: list[str] | None = None,
    limit: int | None = None,
) -> BackfillStats:
    """Download 15-minute history into one Parquet file per gauge, unit, and year.

    Discharge is fetched for every gauge; stage only for gauges that AW rates in feet.
    Existing files are kept except for the current and previous year, whose provisional
    data USGS still revises."""
    stats = BackfillStats()
    now = _now()
    ft_gauges = set(session.scalars(select(ReachGauge.gauge_id).where(ReachGauge.metric == "ft")))
    query = select(Gauge).where(Gauge.source == "USGS").order_by(Gauge.source_id).limit(limit)
    if numbers:
        query = query.where(Gauge.source_id.in_(numbers))

    for gauge in session.scalars(query):
        periods = [("cfs", gauge.cfs_begin, gauge.cfs_end)]
        if gauge.id in ft_gauges:
            periods.append(("ft", gauge.ft_begin, gauge.ft_end))
        stats.gauges += 1
        for unit, begin, end in periods:
            if begin is None or end is None:
                continue
            for year in range(max(start_year, begin.year), end.year + 1):
                path = history_path(data_dir, unit, gauge.source_id, year)
                if path.exists() and year < now.year - 1:
                    stats.files_skipped += 1
                    continue
                window_end = min(datetime(year + 1, 1, 1, tzinfo=UTC), now)
                rows = usgs.history(
                    client, gauge.source_id, unit, datetime(year, 1, 1, tzinfo=UTC), window_end
                )
                _write_history(path, rows)
                stats.files_written += 1
                stats.rows += len(rows)
        if stats.gauges % 10 == 0:
            log.info("Backfill: %d gauges, %d files written", stats.gauges, stats.files_written)
    return stats


def _write_history(path: Path, rows: list) -> None:
    """Write atomically; an empty file still marks the year as done."""
    columns = list(zip(*rows, strict=True)) if rows else [[], [], [], []]
    table = pa.Table.from_arrays(
        [pa.array(c, t.type) for c, t in zip(columns, HISTORY_SCHEMA, strict=True)],
        schema=HISTORY_SCHEMA,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(path)


# --- CLI -----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="USGS gauge jobs")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sites", help="Sync site metadata and periods of record")
    live = sub.add_parser("live", help="Ingest recent 15-minute readings")
    live.add_argument("--hours", type=float, default=3, help="Look-back window (default 3)")
    fill = sub.add_parser("backfill", help="Download 15-minute history to Parquet")
    fill.add_argument("--gauge", action="append", help="Only this USGS site number")
    fill.add_argument("--limit", type=int, help="At most this many gauges")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    settings = get_settings()
    if not settings.usgs_api_key:
        log.warning("USGS_API_KEY is not set; USGS allows far fewer requests without one")
    headers = {"X-Api-Key": settings.usgs_api_key} if settings.usgs_api_key else None
    user_agent = f"RiverRat/0.1 (+{settings.scraper_contact})"

    with (
        PoliteClient(user_agent, settings.usgs_request_interval_s, headers=headers) as client,
        Session(get_engine()) as session,
    ):
        if args.command == "sites":
            log.info("Synced metadata for %d gauges", sync_sites(client, session))
        elif args.command == "live":
            stats = ingest_live(
                client,
                session,
                hours=args.hours,
                retention=timedelta(days=settings.observation_retention_days),
            )
            log.info(
                "Live: %d tiles, %d readings stored, %d pruned",
                stats.tiles,
                stats.stored,
                stats.pruned,
            )
        else:
            stats = backfill(
                client,
                session,
                data_dir=settings.data_dir,
                start_year=settings.history_start_year,
                numbers=args.gauge,
                limit=args.limit,
            )
            log.info("Backfill: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
