"""Fetch the watershed upstream of each USGS gauge from NLDI (DESIGN.md §5.1).

    uv run python -m riverrat.jobs.basins            # gauges without a basin
    uv run python -m riverrat.jobs.basins --refresh  # refetch every NLDI basin
    uv run python -m riverrat.jobs.basins --repair   # StreamStats for those that look wrong

Run `usgs sites` first: the area check compares against USGS drainage areas.
"""

import argparse
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx2
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from riverrat.config import get_settings
from riverrat.db.models import Basin, Gauge
from riverrat.db.session import get_engine
from riverrat.http import PoliteClient
from riverrat.sources import nldi, streamstats

log = logging.getLogger(__name__)

SQ_METERS_PER_SQ_MILE = 2_589_988.11
# Flag basins whose area differs from the published drainage area by more than this.
AREA_TOLERANCE = 0.10
# A basin found from the gauge's location instead (NLDI doesn't index every gauge) is
# kept only within this of the published area: a point can snap to a side stream.
POSITION_TOLERANCE = 0.25
# Where StreamStats' stream grid misses the gauge's point: nearby points to try (degrees,
# about 100 and 200 m), stopping at the first within AREA_TOLERANCE.
NUDGES = tuple(
    (x * d, y * d) for d in (0.001, 0.002) for x, y in ((1, 0), (-1, 0), (0, 1), (0, -1))
)


STREAMSTATS = "streamstats"


@dataclass
class BasinStats:
    fetched: int = 0
    by_position: int = 0  # of fetched: from the gauge's location
    missing: int = 0
    failed: int = 0
    mismatched: int = 0


def sync_basins(client: PoliteClient, session: Session, *, refresh: bool = False) -> BasinStats:
    stats = BasinStats()
    query = select(
        Gauge.id,
        Gauge.source_id,
        func.ST_X(Gauge.geom),
        func.ST_Y(Gauge.geom),
        Gauge.drainage_area_sqmi,
    ).where(Gauge.source == "USGS")
    # A refresh leaves repaired basins alone: NLDI would bring back what was wrong with them.
    kept = select(Basin.gauge_id).where(Basin.gauge_id == Gauge.id)
    if refresh:
        kept = kept.where(Basin.source == STREAMSTATS)
    query = query.where(~kept.exists())
    gauges = session.execute(query.order_by(Gauge.source_id)).all()
    session.rollback()  # no transaction open during the network calls below

    for gauge_id, number, lon, lat, drainage in gauges:
        try:
            geometry = nldi.parse_basin(client.get(nldi.basin_url(number)).json())
        except httpx2.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                stats.failed += 1
                log.warning("Basin for gauge %s failed: %s", number, exc)
                continue
            geometry = None
        except httpx2.HTTPError as exc:
            stats.failed += 1
            log.warning("Basin for gauge %s failed: %s", number, exc)
            continue
        if geometry is None and lon is not None and drainage:
            try:
                geometry = _basin_by_position(client, session, number, lon, lat, drainage)
            except httpx2.HTTPError as exc:
                log.warning("Basin by position for gauge %s failed: %s", number, exc)
            stats.by_position += geometry is not None
        if geometry is None:
            stats.missing += 1
            log.warning("NLDI has no basin for gauge %s", number)
            continue
        _store(session, gauge_id, geometry)
        session.commit()
        stats.fetched += 1

    stats.mismatched = _update_area_ratios(session)
    session.commit()
    return stats


@dataclass
class RepairStats:
    tried: int = 0
    repaired: int = 0
    unchanged: int = 0
    repaired_sites: list[str] = field(default_factory=list)


def repair_basins(client: PoliteClient, session: Session) -> RepairStats:
    """Delineate with StreamStats every gauge whose basin is missing or too far from the
    published drainage area, keeping the result only if it's closer to that area."""
    stats = RepairStats()
    gauges = session.execute(
        select(
            Gauge.id,
            Gauge.source_id,
            func.ST_X(Gauge.geom),
            func.ST_Y(Gauge.geom),
            Gauge.drainage_area_sqmi,
            Gauge.state_code,
            Basin.area_sqmi,
        )
        .outerjoin(Basin, Basin.gauge_id == Gauge.id)
        .where(
            Gauge.source == "USGS",
            Gauge.geom.is_not(None),
            Gauge.drainage_area_sqmi > 0,
            Basin.gauge_id.is_(None) | (func.abs(Basin.area_ratio - 1) > AREA_TOLERANCE),
        )
        .order_by(Gauge.source_id)
    ).all()
    session.rollback()  # no transaction open during the network calls below

    for gauge_id, number, lon, lat, drainage, state, current in gauges:
        region = streamstats.REGIONS.get(state or "")
        if region is None:
            continue
        stats.tried += 1
        best, best_miss = None, abs(current / drainage - 1) if current else float("inf")
        for dx, dy in ((0.0, 0.0), *NUDGES):
            try:
                body = client.get(streamstats.delineate_url(region, lon + dx, lat + dy)).json()
            except httpx2.HTTPError as exc:
                log.warning("StreamStats for gauge %s failed: %s", number, exc)
                continue
            geometry = streamstats.parse_basin(body)
            if geometry is None:
                continue
            area = session.scalar(select(_area_sqmi(_geom(geometry))))
            session.rollback()
            miss = abs(area / drainage - 1)
            if miss < best_miss:
                best, best_miss = geometry, miss
            if miss <= AREA_TOLERANCE:
                break
        if best is None:
            stats.unchanged += 1
            log.info("Gauge %s: nothing closer to the published area", number)
            continue
        _store(session, gauge_id, best, STREAMSTATS)
        session.commit()
        stats.repaired += 1
        stats.repaired_sites.append(number)
        log.info("Gauge %s: repaired, %.1f%% off the published area", number, 100 * best_miss)
    _update_area_ratios(session)
    session.commit()
    return stats


def _basin_by_position(client, session, number, lon, lat, drainage) -> dict | None:
    comid = nldi.parse_comid(client.get(nldi.position_url(lon, lat)).json())
    if comid is None:
        return None
    geometry = nldi.parse_basin(client.get(nldi.comid_basin_url(comid)).json())
    if geometry is None:
        return None
    area = session.scalar(select(_area_sqmi(_geom(geometry))))
    if abs(area / drainage - 1) > POSITION_TOLERANCE:
        log.warning(
            "Basin by position for gauge %s: %.0f mi² vs published %.0f, rejected",
            number,
            area,
            drainage,
        )
        return None
    return geometry


def _geom(geometry: dict):
    # Repair invalid rings and keep only the polygonal parts, as a MultiPolygon.
    return func.ST_Multi(
        func.ST_CollectionExtract(
            func.ST_MakeValid(func.ST_SetSRID(func.ST_GeomFromGeoJSON(json.dumps(geometry)), 4326)),
            3,
        )
    )


def _area_sqmi(geom):
    return func.ST_Area(func.Geography(geom)) / SQ_METERS_PER_SQ_MILE


def _store(session: Session, gauge_id: int, geometry: dict, source: str = "nldi") -> None:
    geom = _geom(geometry)
    values = {
        "geom": geom,
        "area_sqmi": _area_sqmi(geom),
        "source": source,
        "fetched_at": datetime.now(UTC),
    }
    stmt = insert(Basin).values(gauge_id=gauge_id, **values)
    session.execute(stmt.on_conflict_do_update(index_elements=[Basin.gauge_id], set_=values))


def _update_area_ratios(session: Session) -> int:
    """Recompute every basin's area ratio (drainage areas can arrive after the basin) and
    return how many fall outside the tolerance."""
    session.execute(
        text(
            "UPDATE basin SET area_ratio = basin.area_sqmi / gauge.drainage_area_sqmi "
            "FROM gauge WHERE gauge.id = basin.gauge_id AND gauge.drainage_area_sqmi > 0"
        )
    )
    return session.scalar(
        select(func.count()).where(func.abs(Basin.area_ratio - 1) > AREA_TOLERANCE)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch gauge watersheds from NLDI")
    parser.add_argument("--refresh", action="store_true", help="Refetch existing basins")
    parser.add_argument("--repair", action="store_true", help="StreamStats for wrong-looking ones")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    settings = get_settings()
    user_agent = f"RiverRat/0.1 (+{settings.scraper_contact})"
    with (
        # StreamStats can take a minute or more on a big watershed
        PoliteClient(user_agent, settings.nwps_request_interval_s, timeout=240.0) as client,
        Session(get_engine()) as session,
    ):
        if args.repair:
            log.info("Repair: %s", repair_basins(client, session))
            return 0
        stats = sync_basins(client, session, refresh=args.refresh)
    log.info("Basins: %s", stats)
    if stats.mismatched:
        log.info(
            "Review mismatches: SELECT * FROM basin WHERE abs(area_ratio - 1) > %.2f",
            AREA_TOLERANCE,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
