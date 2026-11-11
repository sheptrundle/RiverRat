"""The map's river lines: every reach's course from AW, simplified for drawing. The chances
come from /api/outlook, which the page joins on the reach id."""

import json

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from riverrat.api import cache
from riverrat.api.reaches import SessionDep, catalog_version
from riverrat.db.models import Reach

router = APIRouter(prefix="/api/map", tags=["map"])

SIMPLIFY_DEG = 0.0005  # about 50 m: finer than a line's width at the zooms a section fills
DIGITS = 4  # about 10 m


class MapReach(BaseModel):
    id: int
    river: str
    section: str
    difficulty: str | None
    line: list[list[float]]  # [lon, lat], from put-in to take-out as AW draws it


class ReachMap(BaseModel):
    reaches: list[MapReach]


@router.get("", response_model=ReachMap)
def get_map(request: Request, session: SessionDep) -> Response:
    response = cache.respond(CACHED, request, session)
    # Courses change only with the weekly AW sync.
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


def _build(session: Session) -> ReachMap:
    shape = func.ST_AsGeoJSON(func.ST_SimplifyPreserveTopology(Reach.geom, SIMPLIFY_DEG), DIGITS)
    rows = session.execute(
        select(Reach.id, Reach.river, Reach.section, Reach.difficulty_raw, shape)
        .where(Reach.geom.is_not(None))
        .order_by(Reach.id)
    ).all()
    reaches = []
    for id_, river, section, difficulty, geojson in rows:
        geometry = json.loads(geojson)
        if geometry["type"] != "LineString" or len(geometry["coordinates"]) < 2:
            continue
        reaches.append(
            MapReach(
                id=id_,
                river=river,
                section=section,
                difficulty=difficulty,
                line=geometry["coordinates"],
            )
        )
    return ReachMap(reaches=reaches)


CACHED = cache.register("map", catalog_version, _build)
