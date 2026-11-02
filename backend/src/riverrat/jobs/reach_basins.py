"""Watersheds for reaches without a gauge (reach_basin), from NLDI.

    uv run python -m riverrat.jobs.reach_basins            # reaches without one
    uv run python -m riverrat.jobs.reach_basins --refresh  # refetch all

A reach without a gauge is estimated from a similar gauge nearby, scaled by watershed
area, so it needs its own watershed. AW's line doesn't say which end is downstream: a
point just inside each end is snapped to NHDPlus, and the end with the larger watershed
is the take-out. Points sit INSET along the line, not on the ends, so a take-out at a
confluence doesn't snap to the bigger river it flows into.
"""

import argparse
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx2
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from riverrat.config import get_settings
from riverrat.db.models import Reach, ReachBasin, ReachGauge
from riverrat.db.session import get_engine
from riverrat.http import PoliteClient
from riverrat.jobs.basins import _area_sqmi, _geom
from riverrat.sources import nldi

log = logging.getLogger(__name__)

INSET = 0.05  # share of the line's length


@dataclass
class ReachBasinStats:
    reaches: int = 0
    fetched: int = 0
    missing: int = 0
    failed: int = 0


def sync(client: PoliteClient, session: Session, *, refresh: bool = False) -> ReachBasinStats:
    query = (
        select(
            Reach.id,
            *(
                f(func.ST_LineInterpolatePoint(Reach.geom, at))
                for at in (INSET, 1 - INSET)
                for f in (func.ST_X, func.ST_Y)
            ),
        )
        .where(
            func.ST_GeometryType(Reach.geom) == "ST_LineString",
            ~select(ReachGauge.id).where(ReachGauge.reach_id == Reach.id).exists(),
        )
        .order_by(Reach.id)
    )
    if not refresh:
        query = query.where(
            ~select(ReachBasin.reach_id).where(ReachBasin.reach_id == Reach.id).exists()
        )
    reaches = session.execute(query).all()
    session.rollback()  # no transaction open during the network calls below
    stats = ReachBasinStats(reaches=len(reaches))

    for reach_id, x0, y0, x1, y1 in reaches:
        try:
            ends = [_basin_at(client, session, x, y) for x, y in ((x0, y0), (x1, y1))]
        except httpx2.HTTPError as exc:
            stats.failed += 1
            log.warning("Watershed for reach %d failed: %s", reach_id, exc)
            continue
        found = sorted((e for e in ends if e), key=lambda e: e[2])
        if not found:
            stats.missing += 1
            continue
        comid, geometry, area = found[-1]
        values = {
            "comid": comid,
            "geom": _geom(geometry),
            "area_sqmi": area,
            "putin_area_sqmi": found[0][2] if len(found) == 2 else None,
            "fetched_at": datetime.now(UTC),
        }
        stmt = insert(ReachBasin).values(reach_id=reach_id, **values)
        session.execute(
            stmt.on_conflict_do_update(index_elements=[ReachBasin.reach_id], set_=values)
        )
        session.commit()
        stats.fetched += 1
    return stats


def _basin_at(client: PoliteClient, session: Session, lon: float, lat: float):
    """(COMID, GeoJSON polygon, area in mi²) of the watershed at a point, or None."""
    try:
        comid = nldi.parse_comid(client.get(nldi.position_url(lon, lat)).json())
    except httpx2.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    if comid is None:
        return None
    geometry = nldi.parse_basin(client.get(nldi.comid_basin_url(comid)).json())
    if geometry is None:
        return None
    area = session.scalar(select(_area_sqmi(_geom(geometry))))
    session.rollback()
    return comid, geometry, area


def main() -> int:
    parser = argparse.ArgumentParser(description="Watersheds for reaches without a gauge")
    parser.add_argument("--refresh", action="store_true", help="Refetch every watershed")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    settings = get_settings()
    user_agent = f"RiverRat/0.1 (+{settings.scraper_contact})"
    with (
        PoliteClient(user_agent, settings.nwps_request_interval_s) as client,
        Session(get_engine()) as session,
    ):
        log.info("Reach watersheds: %s", sync(client, session, refresh=args.refresh))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
