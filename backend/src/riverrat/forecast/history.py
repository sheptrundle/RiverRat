"""How often a reach has been in range: its gauge's daily flows over the last ten years
judged against the reach's bands (a day counts when its low and high bracket any part
of the range).

It also catches ranges that can't be right for the gauge. A few AW ranges were written
for another gauge or datum: Tygart Valley 6 runs 3.7–4.7 ft on a gauge that hasn't
dropped below 8 ft in years. Those would otherwise read 0% or 100% forever.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from riverrat.forecast.target import Target
from riverrat.models import store

MIN_DAYS = 365
MIN_MONTH_DAYS = 60  # a month's shares need about two years of that month
# The range is out of the gauge's reach when its bottom is above this quantile of daily
# highs, or its top below this quantile of daily lows.
RECORD_QUANTILE = 0.999


@dataclass(frozen=True)
class RangeHistory:
    share_in_range: float  # of days
    days: int
    # "above_record": the gauge (almost) never gets that high; "below_record": it
    # (almost) never gets that low. Either suggests a range meant for another gauge.
    note: str | None
    # January to December: the share of that month's days in range, too low all day,
    # and too high all day (None: too few days of that month on record)
    months: list["MonthShare | None"]


@dataclass(frozen=True)
class MonthShare:
    in_range: float
    too_low: float
    too_high: float
    days: int


def range_history(found: Target, data_dir: Path) -> RangeHistory | None:
    """None for an estimate: its range is a guess, so its history would be too."""
    if found.estimate is not None or found.release is not None:
        return None
    daily = store.daily_range(data_dir, found.gauge.source_id)
    if daily is None or len(daily[0]) < MIN_DAYS:
        return None
    lows, highs = (np.asarray(found.to_units(v), float) for v in daily[:2])
    months = daily[2]
    bands = found.bands
    bottom = -np.inf if bands.minimum is None else bands.minimum
    top = np.inf if bands.maximum is None else bands.maximum
    note = None
    if np.quantile(highs, RECORD_QUANTILE) < bottom:
        note = "above_record"
    elif np.quantile(lows, 1 - RECORD_QUANTILE) > top:
        note = "below_record"
    in_range = (highs >= bottom) & (lows <= top)
    too_low = highs < bottom
    by_month = []
    for month in range(1, 13):
        days = months == month
        n = int(days.sum())
        by_month.append(
            MonthShare(
                float(in_range[days].mean()),
                float(too_low[days].mean()),
                float((~in_range[days] & ~too_low[days]).mean()),
                n,
            )
            if n >= MIN_MONTH_DAYS
            else None
        )
    return RangeHistory(float(in_range.mean()), len(lows), note, by_month)
