"""Hourly basin-average rain for every gauge watershed (DESIGN.md §5).

    uv run python -m riverrat.jobs.rain backfill                # every AORC year since 2002
    uv run python -m riverrat.jobs.rain backfill --year 2018    # just these years
    uv run python -m riverrat.jobs.rain backfill --refresh      # recompute every basin

History comes from AORC (DESIGN.md §4.3). Each year becomes one Parquet file,
data/rain/aorc/{year}.parquet, with columns (site, ts, mm): the USGS site number, the
end of the hour (UTC), and the area-weighted basin mean in mm. Only the grid chunks that
touch a basin are downloaded, and only for basins missing from the year's file, so
reruns are cheap and new basins are added without redoing the rest. Raw grids are never
kept on disk.
"""

import argparse
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import shapely
from shapely.geometry.base import BaseGeometry
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from riverrat.config import get_settings
from riverrat.db.models import Basin, Gauge
from riverrat.db.session import get_engine
from riverrat.geo.grid import cell_weights
from riverrat.http import PoliteClient
from riverrat.sources import aorc

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
    rows = session.execute(
        select(Gauge.source_id, func.ST_AsGeoJSON(Basin.geom))
        .join(Basin, Basin.gauge_id == Gauge.id)
        .where(Gauge.source == "USGS")
        .order_by(Gauge.source_id)
    ).all()
    session.rollback()  # no transaction open during the downloads
    return {site: shapely.from_geojson(geojson) for site, geojson in rows}


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
        _write(path, table.sort_by([("site", "ascending"), ("ts", "ascending")]))
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


def _read(path: Path) -> pa.Table:
    # Parquet has no second-precision timestamps; it stores ours as milliseconds.
    return pq.read_table(path).cast(RAIN_SCHEMA)


def _write(path: Path, table: pa.Table) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Basin-average rain")
    sub = parser.add_subparsers(dest="command", required=True)
    back = sub.add_parser("backfill", help="Hourly AORC basin rain, one file per year")
    back.add_argument("--year", type=int, action="append", help="Only these years")
    back.add_argument("--refresh", action="store_true", help="Recompute every basin")
    back.add_argument("--workers", type=int, default=8, help="Parallel chunk downloads")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    settings = get_settings()
    # AWS Open Data serves anonymous parallel reads; no throttle needed.
    with (
        PoliteClient(f"RiverRat/0.1 (+{settings.scraper_contact})", 0, timeout=120) as client,
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
