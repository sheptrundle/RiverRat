"""Reaches below Army Corps dams, forecast from the Corps' own release plans.

A dam's release isn't rain-driven the way a river is: the Corps fills the lake in spring,
holds it in summer, releases on a schedule in the fall, and draws it down in November.
Summersville held the Gauley at 100–250 cfs through a 17,000 cfs inflow in April 2025
while filling. So for the days it covers, the Corps' published forecast of each dam's
hourly release is the forecast (jobs/releases.py keeps it, and the observed release).

AW rates these reaches on its "calculated" gauges. Each one listed here is the sum of
some dams' releases and, where the reach also gets undammed water, a USGS gauge on that
water, which River Rat forecasts from rain as usual: the Russell Fork through the Breaks
is the river at Haysi plus the Pound River's release from Flannagan Dam.

TVA publishes each dam's last 8 hours of discharge, a 3-day forecast of each day's
average outflow, and notices (spills, debris flushes, outages). Its reaches don't get a
chance of running yet: AW gives the Hiwassee and Holston tailwaters no range in cfs, and
the Ocoee's bypassed riverbeds run on a recreation schedule TVA's numbers don't show (the
dams' discharge is mostly the powerhouses'). Their pages show TVA's releases and notices.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from riverrat.db.models import NoaaForecast, Observation

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


DAMS = {
    "summersville": Dam("LRH", "Summersville", "Summersville Dam", 38.2208, -80.8889),
    "flannagan": Dam("LRH", "JWFlannagan", "John W. Flannagan Dam", 37.2347, -82.3444),
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
}


# Reaches (AW IDs) below TVA dams: their pages show the dam's releases and notices, but no
# chance of running yet (see above). Without this they'd fall through to an estimate from
# a nearby creek, which says nothing about release days.
TVA_REACHES = {
    1780: "ocoee2",  # Middle Ocoee
    1781: "ocoee3",  # Upper Ocoee
    1751: "apalachia",  # Hiwassee below the powerhouse
    1752: "apalachia",  # Hiwassee Dries: runs only when the dam spills
    1753: "south_holston",  # Holston below the dam
}


def reach_dams(aw_id: int | None, gauges: list[tuple[str, str]]) -> list[str]:
    """Keys of the dams behind a reach: TVA's by reach, the Corps' by the reach's gauges'
    recipes."""
    if aw_id in TVA_REACHES:
        return [TVA_REACHES[aw_id]]
    keys = []
    for gauge in gauges:
        recipe = RECIPES.get(gauge)
        if recipe and (recipe.reaches is None or aw_id in recipe.reaches):
            keys += [key for key in recipe.dams if key not in keys]
    return keys


def planned(session: Session, gauge_id: int, start: datetime, hours: int) -> np.ndarray | None:
    """A dam's release (cfs) for each hour from `start`: the Corps' latest forecast
    issued by then, the latest observed release before it begins, and the forecast's last
    value after it ends. None without a forecast."""
    issued = session.scalar(
        select(func.max(NoaaForecast.issued_at)).where(
            NoaaForecast.gauge_id == gauge_id,
            NoaaForecast.source == FORECAST_SOURCE,
            NoaaForecast.issued_at <= start,
        )
    )
    if issued is None:
        return None
    points = session.execute(
        select(NoaaForecast.valid_time, NoaaForecast.flow_cfs)
        .where(
            NoaaForecast.gauge_id == gauge_id,
            NoaaForecast.source == FORECAST_SOURCE,
            NoaaForecast.issued_at == issued,
            NoaaForecast.valid_time > start,
            NoaaForecast.flow_cfs.is_not(None),
        )
        .order_by(NoaaForecast.valid_time)
    ).all()
    if not points:
        return None
    observed = session.execute(
        select(Observation.ts, Observation.value)
        .where(
            Observation.gauge_id == gauge_id,
            Observation.param == "cfs",
            Observation.ts <= start,
            Observation.ts > start - timedelta(days=2),
        )
        .order_by(Observation.ts.desc())
        .limit(1)
    ).first()
    times = [(t - start) / timedelta(hours=1) for t, _ in points]
    values = [v for _, v in points]
    if observed is not None:
        times.insert(0, (observed[0] - start) / timedelta(hours=1))
        values.insert(0, observed[1])
    return np.interp(np.arange(hours), times, values)
