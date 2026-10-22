"""A gauge's state right now, for live forecasts (DESIGN.md §6.2): its latest hourly flow
from the live readings, and how wet its basin is from the last 30 days of live rain,
computed the same way as for the storms in the analog library (features/events.py).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from riverrat.db.models import BasinRain, Observation, RainForecast, RainForecastRun
from riverrat.features import events, hourly
from riverrat.jobs.rain import STAGE4
from riverrat.jobs.rain_forecast import NBM
from riverrat.models.analog import State
from riverrat.sources.nbm import PERIOD_H

# Forecasts start from the latest reading up to this old (a laptop may have slept); the
# response says how old it is.
MAX_READING_AGE = timedelta(hours=48)
API_HOURS = events.API_DAYS * 24


@dataclass(frozen=True)
class Current:
    state: State  # hourly flow (cfs) and wetness at the end of the latest hour with readings
    ft: float | None  # stage in that hour, when the gauge reports it
    rain_hours: int  # hours of basin rain behind the wetness index, of API_HOURS
    recent_start: datetime  # end of the first hour of `recent_cfs`
    recent_cfs: np.ndarray  # hourly flow through state.at (NaN = no readings)


def current(session: Session, gauge_id: int, now: datetime) -> Current | None:
    """None when the gauge has no flow reading within MAX_READING_AGE."""
    end = hourly.latest_hour(now) + hourly.HOUR  # the hour in progress counts
    hours = hourly.hour_ends(end - MAX_READING_AGE, end)
    cfs, ft = (_hourly(session, gauge_id, param, hours) for param in ("cfs", "ft"))
    seen = np.flatnonzero(np.isfinite(cfs))
    if not len(seen):
        return None
    last = seen[-1]
    at = hours[last].astype("datetime64[s]").item().replace(tzinfo=UTC)

    rain = np.full(API_HOURS, np.nan)
    rows = session.execute(
        select(BasinRain.ts, BasinRain.mm).where(
            BasinRain.gauge_id == gauge_id,
            BasinRain.source == STAGE4,
            BasinRain.ts > at - timedelta(hours=API_HOURS),
            BasinRain.ts <= at,
        )
    ).all()
    for ts, mm in rows:
        rain[API_HOURS - 1 - int((at - ts).total_seconds()) // 3600] = np.nan if mm is None else mm
    api_mm = float(events._api(np.nan_to_num(rain))[-1])
    return Current(
        state=State(q=float(cfs[last]), api_mm=api_mm, at=at),
        ft=float(ft[last]) if np.isfinite(ft[last]) else None,
        rain_hours=int(np.isfinite(rain).sum()),
        recent_start=hours[0].astype("datetime64[s]").item().replace(tzinfo=UTC),
        recent_cfs=cfs[: last + 1],
    )


def _hourly(session: Session, gauge_id: int, param: str, hours: np.ndarray) -> np.ndarray:
    since = hours[0].astype("datetime64[s]").item().replace(tzinfo=UTC) - hourly.HOUR
    rows = session.execute(
        select(Observation.ts, Observation.value, Observation.qualifier).where(
            Observation.gauge_id == gauge_id, Observation.param == param, Observation.ts > since
        )
    ).all()
    return hourly.hourly_flow(hourly.readings_table(rows), hours).astype(float)


@dataclass(frozen=True)
class RainOutlook:
    issued_at: datetime
    periods: list[tuple[datetime, datetime, float, float, float]]  # start, end, p10, p50, p90 mm


def rain_outlook(session: Session, gauge_id: int, now: datetime) -> RainOutlook | None:
    """The latest stored NBM run's 6-hour basin totals still ahead of `now`."""
    issued = session.scalar(
        select(func.max(RainForecastRun.issued_at)).where(RainForecastRun.source == NBM)
    )
    if issued is None:
        return None
    rows = session.execute(
        select(RainForecast.valid_end, RainForecast.p10, RainForecast.p50, RainForecast.p90)
        .where(
            RainForecast.gauge_id == gauge_id,
            RainForecast.source == NBM,
            RainForecast.issued_at == issued,
            RainForecast.valid_end > now,
        )
        .order_by(RainForecast.valid_end)
    ).all()
    periods = [
        (end - timedelta(hours=PERIOD_H), end, p10, p50, p90)
        for end, p10, p50, p90 in rows
        if None not in (p10, p50, p90)
    ]
    return RainOutlook(issued, periods) if periods else None
