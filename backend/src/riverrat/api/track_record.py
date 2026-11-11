"""The Track record endpoint: how the Outlook's kept forecasts have scored
(jobs/track_record.py)."""

from datetime import date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from riverrat.api import cache
from riverrat.api.reaches import SessionDep
from riverrat.db.models import ForecastArchive, Reach

router = APIRouter(prefix="/api/track-record", tags=["track-record"])

BINS = 10
RECENT_DAYS = 3
RECENT_LIMIT = 80


class Lead(BaseModel):
    lead_days: int  # 0 = the morning of
    n: int
    brier: float
    brier_persistence: float  # holding the band the reach was in that morning
    skill: float | None  # 1 − brier / brier_persistence


class Bin(BaseModel):
    low: float  # the forecast chances in this bin
    high: float
    n: int
    said: float  # mean forecast chance
    ran: float  # share of those days the reach ran


class Call(BaseModel):
    day: date
    issued_at: datetime
    reach_id: int
    river: str
    section: str
    altname: str
    p_runnable: float
    observed: bool


class TrackRecord(BaseModel):
    first_day: date | None
    last_day: date | None
    scored: int  # reach-days
    replayed: int  # of those, from mornings recomputed afterwards
    base_rate: float | None  # share of scored reach-days that ran
    leads: list[Lead]
    reliability: list[Bin]
    recent: list[Call]  # the day-before calls of the last few scored days


def scores_version(session: Session) -> tuple:
    """Changes whenever days are scored (jobs/track_record.py)."""
    return tuple(
        session.execute(
            select(func.count(ForecastArchive.scored_at), func.max(ForecastArchive.scored_at))
        ).one()
    )


@router.get("", response_model=TrackRecord)
def get_track_record(
    request: Request,
    session: SessionDep,
    state: str | None = None,
    reaches: Annotated[str | None, Query(description="Comma-separated reach IDs")] = None,
) -> Response:
    if not state and not reaches:  # the Track record page: cached
        return cache.respond(CACHED, request, session)
    body = _summarize(session, state, reaches).model_dump_json()
    return Response(body, media_type="application/json")


def _summarize(
    session: Session, state: str | None = None, reaches: str | None = None
) -> TrackRecord:
    scored = [ForecastArchive.observed.is_not(None)]
    if state:
        scored.append(Reach.states.contains([state]))
    if reaches:
        scored.append(ForecastArchive.reach_id.in_([int(i) for i in reaches.split(",") if i]))

    def query(*columns):
        return select(*columns).join(Reach, Reach.id == ForecastArchive.reach_id).where(*scored)

    observed = case((ForecastArchive.observed, 1.0), else_=0.0)
    persistence = case((ForecastArchive.persistence, 1.0), else_=0.0)
    first, last, n, replayed, base = session.execute(
        query(
            func.min(ForecastArchive.day),
            func.max(ForecastArchive.day),
            func.count(),
            func.count().filter(ForecastArchive.replay),
            func.avg(observed),
        )
    ).one()

    leads = []
    for lead, count, brier, brier_p in session.execute(
        query(
            ForecastArchive.lead_days,
            func.count(),
            func.avg(func.power(ForecastArchive.p_runnable - observed, 2)),
            func.avg(func.power(persistence - observed, 2)),
        )
        .group_by(ForecastArchive.lead_days)
        .order_by(ForecastArchive.lead_days)
    ):
        skill = 1 - brier / brier_p if brier_p > 0 else None
        leads.append(
            Lead(lead_days=lead, n=count, brier=brier, brier_persistence=brier_p, skill=skill)
        )

    bucket = func.least(func.floor(ForecastArchive.p_runnable * BINS), BINS - 1)
    reliability = [
        Bin(low=b / BINS, high=(b + 1) / BINS, n=count, said=said, ran=ran)
        for b, count, said, ran in session.execute(
            query(bucket, func.count(), func.avg(ForecastArchive.p_runnable), func.avg(observed))
            .group_by(bucket)
            .order_by(bucket)
        )
    ]

    recent = []
    if last is not None:
        rows = session.execute(
            query(ForecastArchive, Reach)
            .where(
                ForecastArchive.lead_days == 1,
                ForecastArchive.day > last - timedelta(days=RECENT_DAYS),
                (ForecastArchive.p_runnable >= 0.5) | ForecastArchive.observed,
            )
            .order_by(ForecastArchive.day.desc(), ForecastArchive.p_runnable.desc())
            .limit(RECENT_LIMIT)
        ).all()
        recent = [
            Call(
                day=kept.day,
                issued_at=kept.issued_at,
                reach_id=reach.id,
                river=reach.river,
                section=reach.section,
                altname=reach.altname,
                p_runnable=kept.p_runnable,
                observed=kept.observed,
            )
            for kept, reach in rows
        ]
    return TrackRecord(
        first_day=first,
        last_day=last,
        scored=n,
        replayed=replayed,
        base_rate=base,
        leads=leads,
        reliability=reliability,
        recent=recent,
    )


CACHED = cache.register("track-record", scores_version, _summarize)
