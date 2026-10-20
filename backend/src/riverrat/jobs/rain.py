"""Hourly basin-average rain for every gauge watershed (DESIGN.md §5).

    uv run python -m riverrat.jobs.rain backfill                # every AORC year since 2002
    uv run python -m riverrat.jobs.rain backfill --year 2018    # just these years
    uv run python -m riverrat.jobs.rain backfill --refresh      # recompute every basin
    uv run python -m riverrat.jobs.rain recent                  # Stage IV since AORC ends

History comes from AORC (DESIGN.md §4.3). Each year becomes one Parquet file,
data/rain/aorc/{year}.parquet, with columns (site, ts, mm): the USGS site number, the
end of the hour (UTC), and the area-weighted basin mean in mm. Only the grid chunks that
touch a basin are downloaded, and only for basins missing from the year's file, so
reruns are cheap and new basins are added without redoing the rest. Raw grids are never
kept on disk.

AORC's history ends with the last whole year it publishes; `recent` continues from there
with Stage IV (the product AORC is built from) into Postgres (`basin_rain`), one hour
per file. It runs hourly in the worker: each run processes hours it hasn't seen, newest
first, and refetches hours from the last two days whose file has been revised.
"""

import argparse
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx2
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import shapely
from shapely.geometry.base import BaseGeometry
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from riverrat.config import get_settings
from riverrat.db.models import Basin, BasinRain, Gauge, RainHour
from riverrat.db.session import get_engine
from riverrat.geo.grid import CellWeights, basin_mean, cell_weights
from riverrat.http import PoliteClient
from riverrat.sources import aorc, grib, stage4
from riverrat.storage import write_parquet

log = logging.getLogger(__name__)

RAIN_SCHEMA = pa.schema(
    [
        ("site", pa.string()),
        ("ts", pa.timestamp("s", tz="UTC")),
        ("mm", pa.float32()),
    ]
)
# An hour's basin mean is reported only if at least this share of the basin has data.
MIN_COVERAGE = 0.5


def rain_path(data_dir: Path, year: int) -> Path:
    return data_dir / "rain" / "aorc" / f"{year}.parquet"


@dataclass
class RainStats:
    years_done: int = 0
    years_skipped: int = 0
    basins_computed: int = 0
    chunks: int = 0


def load_basins(session: Session) -> dict[str, BaseGeometry]:
    return {site: geom for _, site, geom in basin_rows(session)}


def basin_rows(session: Session) -> list[tuple[int, str, BaseGeometry]]:
    rows = session.execute(
        select(Gauge.id, Gauge.source_id, func.ST_AsGeoJSON(Basin.geom))
        .join(Basin, Basin.gauge_id == Gauge.id)
        .where(Gauge.source == "USGS")
        .order_by(Gauge.source_id)
    ).all()
    session.rollback()  # no transaction open during the downloads
    return [(gauge_id, site, shapely.from_geojson(geojson)) for gauge_id, site, geojson in rows]


def backfill(
    client: PoliteClient,
    session: Session,
    *,
    data_dir: Path,
    start_year: int,
    years: list[int] | None = None,
    refresh: bool = False,
    workers: int = 8,
) -> RainStats:
    stats = RainStats()
    basins = load_basins(session)
    available = aorc.list_years(client)
    wanted = [y for y in available if y >= start_year and (years is None or y in years)]
    for year in wanted:
        path = rain_path(data_dir, year)
        existing = None if refresh or not path.exists() else _read(path)
        done = set(existing.column("site").unique().to_pylist()) if existing else set()
        todo = {site: geom for site, geom in basins.items() if site not in done}
        if not todo:
            stats.years_skipped += 1
            continue
        log.info("AORC %d: computing %d basins", year, len(todo))
        table, chunks = basin_rain(aorc.Year(client, year), todo, workers=workers)
        if existing is not None:
            keep = pc.invert(pc.is_in(existing.column("site"), pa.array(sorted(todo))))
            table = pa.concat_tables([existing.filter(keep), table])
        write_parquet(path, table.sort_by([("site", "ascending"), ("ts", "ascending")]))
        stats.years_done += 1
        stats.basins_computed += len(todo)
        stats.chunks += chunks
    return stats


def basin_rain(
    store: aorc.Year, basins: dict[str, BaseGeometry], *, workers: int = 8
) -> tuple[pa.Table, int]:
    """Hourly basin means for one year, and how many chunks were downloaded."""
    sites = list(basins)
    n_hours = len(store.time)
    hours_per_chunk, chunk_lat, chunk_lon = store.rain.chunks

    # For each spatial chunk: which basins touch it, at which cells, with what weights.
    by_chunk: dict[tuple[int, int], list[tuple[int, np.ndarray, np.ndarray]]] = defaultdict(list)
    totals = np.zeros(len(sites))
    for g, site in enumerate(sites):
        cells = cell_weights(basins[site], store.lat, store.lon)
        totals[g] = cells.weights.sum()
        if not totals[g]:
            log.warning("Basin %s lies outside the AORC grid", site)
            continue
        keys = np.stack([cells.rows // chunk_lat, cells.cols // chunk_lon], axis=1)
        for key in np.unique(keys, axis=0):
            mask = (keys == key).all(axis=1)
            local = (cells.rows[mask] % chunk_lat) * chunk_lon + cells.cols[mask] % chunk_lon
            by_chunk[(int(key[0]), int(key[1]))].append((g, local, cells.weights[mask]))

    padded = store.rain.chunk_counts()[0] * hours_per_chunk
    weighted = np.zeros((len(sites), padded))
    covered = np.zeros((len(sites), padded))

    def work(t: int, key: tuple[int, int]):
        flat = store.rain_chunk(t, *key).reshape(hours_per_chunk, -1)
        out = []
        for g, local, w in by_chunk[key]:
            values = flat[:, local]
            valid = ~np.isnan(values)
            out.append((g, np.nan_to_num(values) @ w, valid @ w))
        return t, out

    tasks = [(t, key) for t in range(store.rain.chunk_counts()[0]) for key in by_chunk]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for t, parts in pool.map(lambda task: work(*task), tasks):
            hours = slice(t * hours_per_chunk, (t + 1) * hours_per_chunk)
            for g, total, weight in parts:
                weighted[g, hours] += total
                covered[g, hours] += weight

    weighted, covered = weighted[:, :n_hours], covered[:, :n_hours]
    enough = covered >= MIN_COVERAGE * totals[:, None]
    with np.errstate(invalid="ignore", divide="ignore"):
        mm = np.where(enough, weighted / covered, np.nan).astype(np.float32)

    table = pa.table(
        {
            "site": pa.array(np.repeat(sites, n_hours), pa.string()),
            "ts": pa.array(np.tile(store.time, len(sites)), RAIN_SCHEMA.field("ts").type),
            "mm": pa.array(mm.ravel(), pa.float32(), from_pandas=True),
        },
        schema=RAIN_SCHEMA,
    )
    return table, len(tasks)


# --- recent: Stage IV into Postgres ---------------------------------------------------

STAGE4 = "stage4"
# Hours this recent are checked for revised files (RFC quality control arrives late).
REVISION_WINDOW = timedelta(hours=48)


@dataclass
class RecentStats:
    hours: int = 0
    revised: int = 0
    unavailable: int = 0
    failed: int = 0
    remaining: int = 0


def update_recent(
    client: PoliteClient,
    session: Session,
    *,
    start: datetime,
    now: datetime | None = None,
    max_hours: int | None = None,
    refresh: bool = False,
) -> RecentStats:
    """Basin rain for every hour ending after `start`, newest first."""
    stats = RecentStats()
    now = now or datetime.now(UTC)
    basins = basin_rows(session)
    seen = dict(
        session.execute(
            select(RainHour.ts, RainHour.file_modified).where(
                RainHour.source == STAGE4, RainHour.ts >= start
            )
        ).all()
    )
    session.rollback()  # no transaction open during the downloads

    latest = now.replace(minute=0, second=0, microsecond=0)
    hours = [
        latest - timedelta(hours=n) for n in range(int((latest - start) / timedelta(hours=1)) + 1)
    ]
    todo = [h for h in hours if refresh or h not in seen]
    for hour in hours:
        if refresh or hour not in seen or latest - hour >= REVISION_WINDOW:
            continue
        modified = _last_modified(client, stage4.url_for(hour, now))
        if modified and (seen[hour] is None or modified > seen[hour]):
            todo.append(hour)
            stats.revised += 1
    todo.sort(reverse=True)
    if max_hours is not None:
        stats.remaining = max(len(todo) - max_hours, 0)
        todo = todo[:max_hours]

    weights: tuple[grib.Grid, list[CellWeights]] | None = None
    for hour in todo:
        try:
            response = client.get(stage4.url_for(hour, now))
            field = stage4.decode(response.content)
        except httpx2.HTTPStatusError as exc:
            if exc.response.status_code == 404:  # not published yet, or a gap in the archive
                stats.unavailable += 1
            else:
                stats.failed += 1
                log.warning("Stage IV %s failed: %s", hour.isoformat(), exc)
            continue
        except (httpx2.HTTPError, stage4.Stage4FormatError) as exc:
            stats.failed += 1
            log.warning("Stage IV %s failed: %s", hour.isoformat(), exc)
            continue
        if field.hour_end != hour:
            stats.failed += 1
            log.warning("Stage IV %s: file is for %s", hour.isoformat(), field.hour_end)
            continue
        if weights is None or not weights[0].same_as(field.grid):
            weights = (field.grid, _grid_weights(field.grid, [geom for _, _, geom in basins]))
        _store_hour(session, hour, field, basins, weights[1], _modified_header(response))
        stats.hours += 1
        if stats.hours % 100 == 0:
            log.info("Stage IV: %d of %d hours done (back to %s)", stats.hours, len(todo), hour)
    return stats


def _grid_weights(grid: grib.Grid, geoms: list[BaseGeometry]) -> list[CellWeights]:
    return [cell_weights(grid.project(g), grid.y, grid.x, geographic=False) for g in geoms]


def _store_hour(session, hour, field, basins, weights, file_modified) -> None:
    rows = []
    for (gauge_id, _, _), cells in zip(basins, weights, strict=True):
        mm = basin_mean(field.mm, cells, MIN_COVERAGE)
        rows.append({"gauge_id": gauge_id, "source": STAGE4, "ts": hour, "mm": mm})
    if rows:
        stmt = insert(BasinRain).values(rows)
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=["gauge_id", "source", "ts"], set_={"mm": stmt.excluded.mm}
            )
        )
    marker = {
        "source": STAGE4,
        "ts": hour,
        "file_modified": file_modified,
        "fetched_at": datetime.now(UTC),
    }
    stmt = insert(RainHour).values(marker)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["source", "ts"],
            set_={
                "file_modified": stmt.excluded.file_modified,
                "fetched_at": stmt.excluded.fetched_at,
            },
        )
    )
    session.commit()


def _last_modified(client: PoliteClient, url: str) -> datetime | None:
    try:
        return _modified_header(client.head(url))
    except httpx2.HTTPError:
        return None


def _modified_header(response: httpx2.Response) -> datetime | None:
    value = response.headers.get("Last-Modified")
    try:
        return parsedate_to_datetime(value) if value else None
    except ValueError:
        return None


def _read(path: Path) -> pa.Table:
    # Parquet has no second-precision timestamps; it stores ours as milliseconds.
    return pq.read_table(path).cast(RAIN_SCHEMA)


def main() -> int:
    parser = argparse.ArgumentParser(description="Basin-average rain")
    sub = parser.add_subparsers(dest="command", required=True)
    back = sub.add_parser("backfill", help="Hourly AORC basin rain, one file per year")
    back.add_argument("--year", type=int, action="append", help="Only these years")
    back.add_argument("--refresh", action="store_true", help="Recompute every basin")
    back.add_argument("--workers", type=int, default=8, help="Parallel chunk downloads")
    recent = sub.add_parser("recent", help="Hourly Stage IV basin rain since AORC ends")
    recent.add_argument("--max-hours", type=int, help="Process at most this many hours")
    recent.add_argument("--refresh", action="store_true", help="Recompute every hour")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    settings = get_settings()
    user_agent = f"RiverRat/0.1 (+{settings.scraper_contact})"
    if args.command == "recent":
        with (
            PoliteClient(user_agent, settings.stage4_request_interval_s, timeout=120) as client,
            Session(get_engine()) as session,
        ):
            stats = update_recent(
                client,
                session,
                start=settings.live_rain_start,
                max_hours=args.max_hours,
                refresh=args.refresh,
            )
            log.info("Recent rain: %s", stats)
        return 0
    # AWS Open Data serves anonymous parallel reads; no throttle needed.
    with (
        PoliteClient(user_agent, 0, timeout=120) as client,
        Session(get_engine()) as session,
    ):
        stats = backfill(
            client,
            session,
            data_dir=settings.data_dir,
            start_year=settings.history_start_year,
            years=args.year,
            refresh=args.refresh,
            workers=args.workers,
        )
        log.info("Rain backfill: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
