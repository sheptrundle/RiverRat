"""Hourly training tables and the storm-event library (DESIGN.md §6.4, §6.6).

    uv run python -m riverrat.jobs.events hourly      # data/hourly/{site}.parquet
    uv run python -m riverrat.jobs.events extract     # data/events/events.parquet

`hourly` lines up each gauge's flow history (data/usgs/iv) with its basin rain (AORC
Parquet, then Stage IV from Postgres) on one hourly clock, one file per gauge. `extract`
finds the storms in those tables and what the river did after each. Both rebuild from
scratch, so rerun them after the flow or rain backfills add data.
"""

import argparse
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.orm import Session

from riverrat.config import get_settings
from riverrat.db.models import Basin, BasinRain, Gauge
from riverrat.db.session import get_engine
from riverrat.features import events, hourly
from riverrat.jobs import rain as rain_jobs
from riverrat.jobs import usgs as usgs_jobs
from riverrat.storage import write_parquet

log = logging.getLogger(__name__)

EVENT_SCHEMA = pa.schema(
    [
        ("site", pa.string()),
        ("start", pa.timestamp("s", tz="UTC")),  # end of the first wet hour
        ("end", pa.timestamp("s", tz="UTC")),  # end of the last wet hour
        ("duration_h", pa.int32()),
        ("rain_mm", pa.float32()),
        ("max_1h_mm", pa.float32()),
        ("max_3h_mm", pa.float32()),
        ("centroid_h", pa.float32()),
        ("q0", pa.float32()),
        ("q0_24h_ago", pa.float32()),
        ("rain_24h_mm", pa.float32()),
        ("rain_7d_mm", pa.float32()),
        ("rain_30d_mm", pa.float32()),
        ("api_mm", pa.float32()),
        ("qpeak", pa.float32()),
        ("peak", pa.timestamp("s", tz="UTC")),
        ("t_peak_h", pa.int32()),  # from `start`
        ("lag_h", pa.float32()),  # from the rain's center
        ("peak_complete", pa.bool_()),
        ("window_h", pa.int32()),
        ("next_storm_h", pa.int32()),  # from `start`; null if none in the window
        ("flow_coverage", pa.float32()),
        ("runoff_mm", pa.float32()),
        ("runoff_ratio", pa.float32()),
    ]
)
STAGE4 = rain_jobs.STAGE4


def hourly_path(data_dir: Path, site: str) -> Path:
    return data_dir / "hourly" / f"{site}.parquet"


def events_path(data_dir: Path) -> Path:
    return data_dir / "events" / "events.parquet"


# --- hourly --------------------------------------------------------------------------


@dataclass
class HourlyStats:
    gauges: int = 0
    no_flow: int = 0


def build_hourly(
    session: Session,
    *,
    data_dir: Path,
    start_year: int,
    live_rain_start: datetime,
    now: datetime | None = None,
    sites: list[str] | None = None,
    batch_size: int = 50,
) -> HourlyStats:
    """One table per gauge with a basin, from `start_year` through the latest whole hour.
    Gauges without downloaded flow history are skipped."""
    stats = HourlyStats()
    query = (
        select(Gauge.id, Gauge.source_id)
        .join(Basin, Basin.gauge_id == Gauge.id)
        .where(Gauge.source == "USGS")
        .order_by(Gauge.source_id)
    )
    if sites:
        query = query.where(Gauge.source_id.in_(sites))
    gauges = session.execute(query).all()
    session.rollback()

    flow_dirs = {site: usgs_jobs.history_dir(data_dir, "cfs", site) for _, site in gauges}
    with_flow = [(gauge_id, site) for gauge_id, site in gauges if flow_dirs[site].is_dir()]
    stats.no_flow = len(gauges) - len(with_flow)
    hours = hourly.hour_ends(
        datetime(start_year, 1, 1, 1, tzinfo=UTC), hourly.latest_hour(now or datetime.now(UTC))
    )
    for i in range(0, len(with_flow), batch_size):
        batch = with_flow[i : i + batch_size]
        rain = _aorc_rain(data_dir, [site for _, site in batch], hours, live_rain_start)
        live = _live_rain(session, [gauge_id for gauge_id, _ in batch], hours, live_rain_start)
        for gauge_id, site in batch:
            mm = rain[site]
            if gauge_id in live:
                mm = np.where(np.isnan(live[gauge_id]), mm, live[gauge_id])
            readings = pa.concat_tables(
                [pq.read_table(path) for path in sorted(flow_dirs[site].glob("*.parquet"))]
            )
            cfs = hourly.hourly_flow(readings, hours)
            write_parquet(hourly_path(data_dir, site), hourly.to_table(hours, cfs, mm))
            stats.gauges += 1
        log.info("Hourly tables: %d of %d gauges", stats.gauges, len(with_flow))
    return stats


def _aorc_rain(
    data_dir: Path, sites: list[str], hours: np.ndarray, live_rain_start: datetime
) -> dict[str, np.ndarray]:
    out = {site: np.full(len(hours), np.nan, np.float32) for site in sites}
    for year in range(_year(hours[0]), live_rain_start.year + 1):
        path = rain_jobs.rain_path(data_dir, year)
        if not path.exists():
            continue
        table = pq.read_table(path, filters=[("site", "in", sites)])
        table = table.cast(rain_jobs.RAIN_SCHEMA)
        table = table.filter(pc.less(table["ts"], pa.scalar(live_rain_start, table["ts"].type)))
        names = table["site"].to_numpy(zero_copy_only=False)
        slots = _slots(table["ts"], hours)
        values = table["mm"].to_numpy(zero_copy_only=False)
        for site in sites:
            mask = (names == site) & (slots >= 0)
            out[site][slots[mask]] = values[mask]
    return out


def _live_rain(
    session: Session, gauge_ids: list[int], hours: np.ndarray, live_rain_start: datetime
) -> dict[int, np.ndarray]:
    rows = session.execute(
        select(BasinRain.gauge_id, BasinRain.ts, BasinRain.mm).where(
            BasinRain.gauge_id.in_(gauge_ids),
            BasinRain.source == STAGE4,
            BasinRain.ts >= live_rain_start,
        )
    ).all()
    session.rollback()
    out: dict[int, np.ndarray] = {}
    first = _seconds(hours[0])
    for gauge_id, ts, mm in rows:
        slot = (int(ts.timestamp()) - first) // 3600
        if 0 <= slot < len(hours):
            series = out.setdefault(gauge_id, np.full(len(hours), np.nan, np.float32))
            series[slot] = np.nan if mm is None else mm
    return out


def _slots(ts: pa.ChunkedArray, hours: np.ndarray) -> np.ndarray:
    """Positions of hour-end times in `hours`; -1 outside it."""
    seconds = ts.cast(pa.int64()).to_numpy()
    slots = (seconds - _seconds(hours[0])) // 3600
    return np.where((slots >= 0) & (slots < len(hours)), slots, -1)


def _seconds(hour: np.datetime64) -> int:
    return int(hour.astype("datetime64[s]").astype(np.int64))


def _year(hour: np.datetime64) -> int:
    return int(str(hour)[:4])


# --- events --------------------------------------------------------------------------


@dataclass
class EventStats:
    gauges: int = 0
    events: int = 0


def extract(session: Session, *, data_dir: Path) -> EventStats:
    """Every storm event at every gauge with an hourly table, in one file."""
    areas = dict(
        session.execute(
            select(Gauge.source_id, Basin.area_sqmi)
            .join(Basin, Basin.gauge_id == Gauge.id)
            .where(Gauge.source == "USGS")
        ).all()
    )
    session.rollback()
    stats = EventStats()
    tables = []
    for path in sorted((data_dir / "hourly").glob("*.parquet")):
        site = path.stem
        table = pq.read_table(path).cast(hourly.HOURLY_SCHEMA)
        found = events.extract_events(
            table["cfs"].to_numpy(), table["mm"].to_numpy(), areas.get(site)
        )
        tables.append(event_table(site, table["ts"], found))
        stats.gauges += 1
        stats.events += len(found)
    combined = pa.concat_tables(tables) if tables else EVENT_SCHEMA.empty_table()
    write_parquet(events_path(data_dir), combined)
    return stats


def event_table(site: str, ts: pa.ChunkedArray, found: list[events.Event]) -> pa.Table:
    times = ts.to_numpy()

    def at(i: int) -> datetime:
        return times[i].astype("datetime64[s]").item().replace(tzinfo=UTC)

    rows = [
        {
            "site": site,
            "start": at(e.start),
            "end": at(e.end),
            "duration_h": e.end - e.start + 1,
            "rain_mm": e.rain_mm,
            "max_1h_mm": e.max_1h_mm,
            "max_3h_mm": e.max_3h_mm,
            "centroid_h": e.centroid_h,
            "q0": e.q0,
            "q0_24h_ago": e.q0_24h_ago,
            "rain_24h_mm": e.rain_24h_mm,
            "rain_7d_mm": e.rain_7d_mm,
            "rain_30d_mm": e.rain_30d_mm,
            "api_mm": e.api_mm,
            "qpeak": e.qpeak,
            "peak": at(e.peak),
            "t_peak_h": e.peak - e.start,
            "lag_h": e.peak - e.start - e.centroid_h,
            "peak_complete": e.peak_complete,
            "window_h": e.window_end - e.start + 1,
            "next_storm_h": None if e.next_storm is None else e.next_storm - e.start,
            "flow_coverage": e.flow_coverage,
            "runoff_mm": e.runoff_mm,
            "runoff_ratio": e.runoff_ratio,
        }
        for e in found
    ]
    return pa.Table.from_pylist(rows, schema=EVENT_SCHEMA)


# --- CLI -----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Hourly tables and storm events")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("hourly", help="Align flow and basin rain, one file per gauge")
    build.add_argument("--gauge", action="append", help="Only this USGS site number")
    sub.add_parser("extract", help="Find storm events in the hourly tables")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    with Session(get_engine()) as session:
        if args.command == "hourly":
            stats = build_hourly(
                session,
                data_dir=settings.data_dir,
                start_year=settings.history_start_year,
                live_rain_start=settings.live_rain_start,
                sites=args.gauge,
            )
            log.info("Hourly tables: %s", stats)
        else:
            log.info("Events: %s", extract(session, data_dir=settings.data_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
