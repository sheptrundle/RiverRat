"""Reaches below dams, forecast from the dams' own release plans.

A dam's release isn't rain-driven the way a river is: the Corps fills the lake in spring,
holds it in summer, releases on a schedule in the fall, and draws it down in November.
Summersville held the Gauley at 100–250 cfs through a 17,000 cfs inflow in April 2025
while filling. So for the days it covers, the Corps' published forecast of each dam's
hourly release is the forecast (jobs/releases.py keeps it, and the observed release).

AW rates these reaches on its "calculated" gauges. Each one listed here is the sum of
some dams' releases and, where the reach also gets undammed water, a USGS gauge on that
water, which River Rat forecasts from rain as usual: the Russell Fork through the Breaks
is the river at Haysi plus the Pound River's release from Flannagan Dam.

The New River below Bluestone Dam is rated on real USGS gauges (Hinton, Thurmond), which
stay the record: there the reach is Bluestone's release, lagged to the gauge, plus the
Greenbrier at Hilldale scaled up for the side creeks. Fit on 2020–2026 hourly flows, the
sum is within 3.8% of Thurmond (11 h and 8 h lags, ×1.25) and 3.9% of Hinton (1 h and
2 h, ×0.82) in the median hour.

TVA publishes each dam's last 8 hours of discharge, a 3-day forecast of each day's
average outflow, and notices (spills, debris flushes, outages). Below Apalachia and South
Holston, the river runs when the powerhouse generates (one unit about 1,400–1,500 cfs):
each day's average says roughly how many hours it will, and not when, so the forecast
spreads that many hours across the day (TvaReach). The Ocoee's riverbeds run on TVA's
recreation calendar instead (the dams' discharge is mostly the flumes'), so those reaches
follow the published schedule. The Hiwassee Dries run only when Apalachia spills, which
nothing published forecasts.

Some dams publish no plan at all (Philpott's is a phone recording). Below Philpott, the
Smith runs in long steady states, ~250 cfs for months and ~80 cfs for months, switching a
few times a year whatever the rain (the lake takes it), so the forecast is the dam
holding its last day's pattern, with the measured chance it switches by each day (Held).
Replayed on 2025–26 mornings, plain persistence beat the rain model, even given the rain
that fell, at every lead (Brier 0.007 vs 0.011 today, 0.075 vs 0.092 five days out); the
switch chances, fit leaving each year out in turn, match or beat plain persistence at
every lead over 2021–26 (0.016 vs 0.016 today, 0.098 vs 0.102 five days out).
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from riverrat.db.models import NoaaForecast, Observation
from riverrat.sources.tva import EASTERN

FORECAST_SOURCE = "usace"  # noaa_forecast.source for the Corps' release forecasts


@dataclass(frozen=True)
class Dam:
    office: str  # CWMS office, e.g. LRH (Huntington District); "TVA" for TVA dams
    location: str  # CWMS location prefix, e.g. "Summersville"; TVA's ID, e.g. "OCCT1"
    name: str
    lat: float
    lon: float

    @property
    def agency(self) -> str:
        return "TVA" if self.office == "TVA" else "USACE"

    @property
    def source_id(self) -> str:
        return f"{self.office}/{self.location}"

    @property
    def forecast_source(self) -> str:
        return "tva" if self.agency == "TVA" else FORECAST_SOURCE

    @property
    def observed(self) -> str:
        return f"{self.location}-Outflow.Flow.Inst.15Minutes.0.OBS"

    @property
    def forecast(self) -> str:
        return f"{self.location}-Outflow.Flow.Inst.1Hour.0.CELRH-FCST-DAILY"


@dataclass(frozen=True)
class Recipe:
    dams: tuple[str, ...]  # keys of DAMS
    natural: str | None = None  # USGS site for the undammed part, forecast from rain
    # Where its live readings come from when USGS no longer publishes them: (CWMS office,
    # series). The model still learns from the USGS record.
    natural_live: tuple[str, str] | None = None
    # Only these reaches (AW IDs), when AW also rates reaches above the dam on the gauge
    reaches: frozenset[int] | None = None
    dam_lag_h: int = 0  # hours from the dams to the reach's gauge
    natural_scale: float = 1.0  # the undammed part's gauge, scaled for creeks it misses
    natural_lag_h: int = 0
    # False when the gauge is a live USGS gauge of its own, not a sum of the parts
    composite: bool = True


DAMS = {
    "summersville": Dam("LRH", "Summersville", "Summersville Dam", 38.2208, -80.8889),
    "flannagan": Dam("LRH", "JWFlannagan", "John W. Flannagan Dam", 37.2347, -82.3444),
    "bluestone": Dam("LRH", "Bluestone", "Bluestone Dam", 37.6439, -80.8867),
    "ocoee2": Dam("TVA", "OCBT1", "Ocoee No. 2 Dam", 35.095, -84.5347),
    "ocoee3": Dam("TVA", "OCCT1", "Ocoee No. 3 Dam", 35.0406, -84.4667),
    "apalachia": Dam("TVA", "HADT1", "Apalachia Dam", 35.1681, -84.2953),
    "south_holston": Dam("TVA", "SHDT1", "South Holston Dam", 36.5203, -82.0953),
}

# AW's gauge (source, ID) → how its flow is made up. Mostly AW "calculated" gauges; also
# USGS gauges right below a dam that USGS no longer publishes flow for.
RECIPES = {
    ("CALCULATED", "36510909"): Recipe(("summersville",)),  # Gauley at Summersville: Upper Gauley
    # Pound River below Flannagan Dam (USGS stopped publishing its flow in October 2024)
    # (AW also rates section 1, above the lake, on it)
    ("USGS", "03209000"): Recipe(("flannagan",), reaches=frozenset({1988})),
    # Russell Fork Gorge: the river at Haysi, whose flow USGS stopped publishing in October
    # 2024 and the Corps now reports, plus the Pound River's release from Flannagan
    ("CALCULATED", "35795407"): Recipe(
        ("flannagan",), natural="03208500", natural_live=("LRH", "Haysi.Flow.Inst.15Minutes.0.OBS")
    ),
    # The New River: Bluestone's release plus the Greenbrier (see above)
    ("USGS", "03184500"): Recipe(  # at Hinton: Bluestone Dam to Sandstone
        ("bluestone",),
        natural="03184000",
        dam_lag_h=1,
        natural_scale=0.82,
        natural_lag_h=2,
        composite=False,
    ),
    ("USGS", "03185400"): Recipe(  # at Thurmond: Sandstone to Fayette Station
        ("bluestone",),
        natural="03184000",
        dam_lag_h=11,
        natural_scale=1.25,
        natural_lag_h=8,
        composite=False,
    ),
}


@dataclass(frozen=True)
class TvaReach:
    """A reach below a TVA dam. AW gives these no range in cfs, so River Rat sets one."""

    dam: str  # key of DAMS
    range_cfs: tuple[float, float] | None  # None: no chance of running can be given
    schedule: dict[date, tuple[time, time]] | None = None  # recreation releases, local time
    release_cfs: float = 0.0  # a scheduled release
    unit_cfs: float = 0.0  # one generating unit, when recent readings don't show it
    why_not: str = ""  # with no range: why


def _calendar(spans: dict[tuple[time, time], str], year: int) -> dict[date, tuple[time, time]]:
    """{(start, end): "M/D M/D …"} → {date: (start, end)}."""
    days = {}
    for hours, text in spans.items():
        for item in text.split():
            month, day = item.split("/")
            days[date(year, int(month), int(day))] = hours
    return days


_6H2, _6H3, _8H2 = (time(10), time(16)), (time(9), time(15)), (time(9), time(17))
_10H, _7H, _5H2 = (time(9), time(19)), (time(9), time(16)), (time(10), time(15))
_8H3, _5H3 = (time(8, 30), time(16, 30)), (time(9), time(14))

# TVA's 2026 recreation release calendars (Ocoee No. 2 and No. 3), by length of day. Add
# each year's when TVA publishes it (usually by February).
OCOEE2_2026 = _calendar(
    {
        _6H2: "3/21 3/22 3/28 3/29 5/28 5/29 6/1 6/4 6/5 6/8 6/11 6/12 6/15 6/18 6/19 6/22 "
        "6/25 6/26 6/29 8/3 9/26 9/27 9/28 9/29 9/30 10/1 10/2 10/3 10/4 10/10 10/11 "
        "10/17 10/18 10/24 10/25 10/31 11/1",
        _8H2: "4/4 4/5 4/11 4/12 4/18 4/19 4/25 4/26 5/2 5/3 5/9 5/10 5/16 5/17 5/23 5/24 "
        "5/25 5/30 5/31 7/2 7/3 7/9 7/10 7/16 7/17 7/23 7/24 7/30 7/31 8/7 9/7 9/13 9/20",
        _10H: "6/6 6/7 6/13 6/14 6/20 6/21 6/27 6/28 7/4 7/5 7/11 7/12 7/18 7/19 7/25 7/26 "
        "8/1 8/2 8/8 8/9 8/15 8/16 8/22 8/23 8/29 8/30 9/5 9/6 9/12 9/19",
        _7H: "7/6 7/13 7/20 7/27 8/6",
        _5H2: "8/10 8/13 8/14 8/17 8/20 8/21 8/24 8/27 8/28 8/31 9/3 9/4",
    },
    2026,
)
OCOEE3_2026 = _calendar(
    {
        _6H3: "5/16 5/23 5/30 5/31 6/6 6/13 6/20 6/27 9/12",
        _8H3: "5/24 7/4 7/11 7/18 7/25 8/1 8/8 8/15 8/22 8/29 9/5 9/6",
        _5H3: "6/7 6/14 6/21 6/28 7/5 7/12 7/19 7/26 8/2 8/9 8/16 8/23 8/30",
    },
    2026,
)

# Reaches (AW IDs) below TVA dams. Without this they'd fall through to an estimate from a
# nearby creek, which says nothing about release days.
TVA_REACHES = {
    1780: TvaReach("ocoee2", (1000, 5000), schedule=OCOEE2_2026, release_cfs=1250),  # Middle
    1781: TvaReach("ocoee3", (1000, 5000), schedule=OCOEE3_2026, release_cfs=1250),  # Upper
    1751: TvaReach("apalachia", (1000, 10000), unit_cfs=1450),  # Hiwassee below the powerhouse
    1752: TvaReach(  # Hiwassee Dries
        "apalachia",
        None,
        why_not="the Dries run only when Apalachia Dam spills, which TVA doesn't forecast",
    ),
    1753: TvaReach("south_holston", (1000, 10000), unit_cfs=1480),  # Holston below the dam
}


@dataclass(frozen=True)
class Held:
    """A gauge just below a dam with no published plan that runs in long steady states:
    the dam holds its last day's pattern or switches to the other state."""

    dam: str  # its name
    # The chance the state has switched by each day ahead (0 = today), fit on 2021–26
    # daily runnability: leaving the range from inside it, and coming in from outside
    leave: tuple[float, ...]
    enter: tuple[float, ...]
    in_cfs: float  # its usual flow in each state (median over the last year)
    out_cfs: float


# AW's gauge (source, ID) → held release
HELD = {
    ("USGS", "02072000"): Held(  # Smith River near Philpott: Philpott Dam to North Bassett
        "Philpott Dam",
        leave=(0.001, 0.022, 0.038, 0.052, 0.062, 0.073),
        enter=(0.044, 0.081, 0.112, 0.138, 0.157, 0.175),
        in_cfs=253,
        out_cfs=83,
    ),
}

SCHEDULE_KEPT = 0.95  # a scheduled release can still be called off (power system, weather)
PLACEMENTS = 13  # where in the day TVA's generating hours might fall, as forecast members


def reach_dams(aw_id: int | None, gauges: list[tuple[str, str]]) -> list[str]:
    """Keys of the dams behind a reach: TVA's by reach, the Corps' by the reach's gauges'
    recipes."""
    if aw_id in TVA_REACHES:
        return [TVA_REACHES[aw_id].dam]
    keys = []
    for gauge in gauges:
        recipe = RECIPES.get(gauge)
        if recipe and (recipe.reaches is None or aw_id in recipe.reaches):
            keys += [key for key in recipe.dams if key not in keys]
    return keys


def planned(
    session: Session,
    gauge_id: int,
    start: datetime,
    hours: int,
    source: str = FORECAST_SOURCE,
    lag_h: int = 0,
) -> np.ndarray | None:
    """A dam's release (cfs) for each hour from `start - lag_h` (what reaches a gauge
    `lag_h` hours downstream from `start` on): the observed release up to `start`, then the
    latest forecast issued by `start`, holding its last value after it ends. None without a
    forecast."""
    issued = session.scalar(
        select(func.max(NoaaForecast.issued_at)).where(
            NoaaForecast.gauge_id == gauge_id,
            NoaaForecast.source == source,
            NoaaForecast.issued_at <= start,
        )
    )
    if issued is None:
        return None
    points = session.execute(
        select(NoaaForecast.valid_time, NoaaForecast.flow_cfs)
        .where(
            NoaaForecast.gauge_id == gauge_id,
            NoaaForecast.source == source,
            NoaaForecast.issued_at == issued,
            NoaaForecast.valid_time > start,
            NoaaForecast.flow_cfs.is_not(None),
        )
        .order_by(NoaaForecast.valid_time)
    ).all()
    if not points:
        return None
    origin = start - timedelta(hours=lag_h)
    observed = session.execute(
        select(Observation.ts, Observation.value)
        .where(
            Observation.gauge_id == gauge_id,
            Observation.param == "cfs",
            Observation.ts <= start,
            Observation.ts > origin - timedelta(days=2),
        )
        .order_by(Observation.ts)
    ).all()
    # The readings from the last one before `origin` on
    before = [i for i, (t, _) in enumerate(observed) if t <= origin]
    observed = observed[before[-1] if before else 0 :]
    times = [(t - origin) / timedelta(hours=1) for t, _ in [*observed, *points]]
    values = [v for _, v in [*observed, *points]]
    return np.interp(np.arange(hours), times, values)


def scheduled(reach: TvaReach, start: datetime, hours: int) -> np.ndarray:
    """The reach's flow (cfs) each hour from `start` on TVA's recreation calendar: the
    release in scheduled hours, nothing otherwise (the riverbed is bypassed)."""
    cfs = np.zeros(hours)
    for h in range(hours):
        local = (start + timedelta(hours=h)).astimezone(EASTERN)
        span = (reach.schedule or {}).get(local.date())
        if span and span[0] <= local.time() < span[1]:
            cfs[h] = reach.release_cfs
    return cfs


def generating(
    daily_cfs: np.ndarray, start: datetime, unit_cfs: float, base_cfs: float
) -> np.ndarray:
    """PLACEMENTS × hours of flow below a peaking powerhouse, from the planned average for
    each hour's local day (`daily_cfs`, hourly from `start`). A day averaging A runs one
    unit (`unit_cfs`) for (A − base) / (unit − base) of its 24 hours, a block placed from
    the day's start to its end across the members; A at or above a unit runs all day."""
    paths = np.full((PLACEMENTS, len(daily_cfs)), base_cfs)
    local_days = [(start + timedelta(hours=h)).astimezone(EASTERN) for h in range(len(daily_cfs))]
    for h, local in enumerate(local_days):
        average = daily_cfs[h]
        if average >= unit_cfs:
            paths[:, h] = average
            continue
        span = max(unit_cfs - base_cfs, 1.0)
        on_hours = int(np.clip(np.ceil((average - base_cfs) / span * 24), 0, 24))
        if on_hours == 0:
            continue
        for j in range(PLACEMENTS):
            first = round(j / (PLACEMENTS - 1) * (24 - on_hours))
            if first <= local.hour < first + on_hours:
                paths[j, h] = unit_cfs
    return paths


def held(
    profile: np.ndarray, other_cfs: float, switched: tuple[float, ...], start: datetime, hours: int
) -> tuple[np.ndarray, np.ndarray]:
    """Paths (with weights) for a dam holding its last 24 hours' `profile` (the last value
    at `start`) or switching to `other_cfs` at the start of a local day: each day ahead's
    path weighted by the chance of switching on it (`switched`, cumulative), and one that
    never switches."""
    hold = profile[(np.arange(hours) + len(profile) - 1) % len(profile)]
    first_day = start.astimezone(EASTERN).date()
    days = np.array(
        [
            ((start + timedelta(hours=h)).astimezone(EASTERN).date() - first_day).days
            for h in range(hours)
        ]
    )
    by_day = np.maximum.accumulate(np.clip(switched, 0, 1))
    paths, weights = [hold], [1 - by_day[-1]]
    for day, chance in enumerate(by_day):
        path = hold.copy()
        path[days >= day] = other_cfs
        paths.append(path)
        weights.append(chance - (by_day[day - 1] if day else 0.0))
    return np.stack(paths), np.array(weights)
