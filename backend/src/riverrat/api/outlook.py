"""The Outlook endpoint (DESIGN.md §9): every forecast reach's chance of running over the
next days, as last computed by jobs/outlook.py."""

from datetime import date, datetime

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from riverrat.api.reaches import Peak, RainRange, SessionDep
from riverrat.db.models import Gauge, Reach, ReachGauge, ReachOutlook

router = APIRouter(prefix="/api/outlook", tags=["outlook"])


class OutlookDay(BaseModel):
    day: date
    p_runnable: float
    peak_bands: dict[str, float]
    p_too_high_all_day: float
    peak: Peak
    hours_in_range: float
    rain_in: RainRange | None  # NBM basin rain from now through this day's sunset


class OutlookReach(BaseModel):
    id: int
    aw_id: int | None
    river: str
    section: str
    difficulty: str | None
    states: list[str]
    gauge_site: str
    primary: bool  # False: a stand-in for AW's primary gauge
    metric: str
    range_min: float | None
    range_max: float | None
    now_value: float
    now_band: str
    as_of: datetime
    extrapolated: bool
    days: list[OutlookDay]


class Outlook(BaseModel):
    computed_at: datetime | None  # None: the Outlook hasn't been computed yet
    rain_issued_at: datetime | None
    days: list[date]
    reaches: list[OutlookReach]


@router.get("")
def get_outlook(session: SessionDep) -> Outlook:
    rows = session.execute(
        select(ReachOutlook, Reach, Gauge.source_id, ReachGauge.range_min, ReachGauge.range_max)
        .join(Reach, Reach.id == ReachOutlook.reach_id)
        .join(Gauge, Gauge.id == ReachOutlook.gauge_id)
        .join(
            ReachGauge,
            (ReachGauge.reach_id == ReachOutlook.reach_id)
            & (ReachGauge.gauge_id == ReachOutlook.gauge_id),
        )
        .order_by(Reach.river, Reach.section)
    ).all()
    reaches = [
        OutlookReach(
            id=reach.id,
            aw_id=reach.aw_id,
            river=reach.river,
            section=reach.section,
            difficulty=reach.difficulty_raw,
            states=reach.states,
            gauge_site=site,
            primary=found.primary,
            metric=found.metric,
            range_min=low,
            range_max=high,
            now_value=found.now_value,
            now_band=found.now_band,
            as_of=found.as_of,
            extrapolated=found.extrapolated,
            days=found.days,
        )
        for found, reach, site, low, high in rows
    ]
    return Outlook(
        computed_at=max((r[0].computed_at for r in rows), default=None),
        rain_issued_at=max(
            (r[0].rain_issued_at for r in rows if r[0].rain_issued_at), default=None
        ),
        days=sorted({d.day for r in reaches for d in r.days}),
        reaches=reaches,
    )
