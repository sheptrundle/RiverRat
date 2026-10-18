"""Fetch the watershed upstream of each USGS gauge from NLDI (DESIGN.md §5.1).

    uv run python -m riverrat.jobs.basins            # gauges without a basin
    uv run python -m riverrat.jobs.basins --refresh  # refetch every basin

Run `usgs sites` first: the area check compares against USGS drainage areas.
"""

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx2
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from riverrat.config import get_settings
from riverrat.db.models import Basin, Gauge
from riverrat.db.session import get_engine
from riverrat.http import PoliteClient
from riverrat.sources import nldi

log = logging.getLogger(__name__)

SQ_METERS_PER_SQ_MILE = 2_589_988.11
# Flag basins whose area differs from the published drainage area by more than this.
AREA_TOLERANCE = 0.10


@dataclass
class BasinStats:
    fetched: int = 0
    missing: int = 0
    failed: int = 0
    mismatched: int = 0


def sync_basins(client: PoliteClient, session: Session, *, refresh: bool = False) -> BasinStats:
    stats = BasinStats()
    query = select(Gauge.id, Gauge.source_id).where(Gauge.source == "USGS")
    if not refresh:
        query = query.where(~select(Basin.gauge_id).where(Basin.gauge_id == Gauge.id).exists())
    gauges = session.execute(query.order_by(Gauge.source_id)).all()
    session.rollback()  # no transaction open during the network calls below

    for gauge_id, number in gauges:
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


def _store(session: Session, gauge_id: int, geometry: dict) -> None:
    # Repair invalid rings and keep only the polygonal parts, as a MultiPolygon.
    geom = func.ST_Multi(
        func.ST_CollectionExtract(
            func.ST_MakeValid(func.ST_SetSRID(func.ST_GeomFromGeoJSON(json.dumps(geometry)), 4326)),
            3,
        )
    )
    values = {
        "geom": geom,
        "area_sqmi": func.ST_Area(func.Geography(geom)) / SQ_METERS_PER_SQ_MILE,
        "source": "nldi",
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
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    settings = get_settings()
    user_agent = f"RiverRat/0.1 (+{settings.scraper_contact})"
    with (
        PoliteClient(user_agent, settings.nwps_request_interval_s) as client,
        Session(get_engine()) as session,
    ):
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
