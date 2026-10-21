"""The What-if slider's curve (DESIGN.md §6.2–6.3, §8 rain_response): for a storm of each
size on the same timing, the chance of the reach running each day, the expected peak,
and the least rain that would likely bring it in.

Each rain amount is an independent analog forecast, so the raw curve wobbles a little
as the matched storms change. Quantities that can only grow with more rain are made
nondecreasing (pool-adjacent-violators): the peak's percentiles, the chance of reaching
the bottom of the range, and the chance of being too high all day. P(runnable) itself
rises and then can fall (too much rain blows the reach out), so it's derived from those:
P(reach the minimum) − P(too high all day). That's exact unless a member jumps from
below the range to above it within one hour, which hourly flows almost never do.
"""

from dataclasses import dataclass
from datetime import date, datetime

import numpy as np

from riverrat.forecast.bands import Bands
from riverrat.forecast.daily import daily
from riverrat.models import analog

MM_PER_IN = 25.4
RAIN_IN = tuple(np.round(np.arange(0, 4.001, 0.25), 2))
# From 2002–2025 storms of 0.5 in+ in the pilot: duration and the share of the rain in
# the wettest 3 hours (median 18 h storm: 45%).
STORM_TYPES = {
    "burst": (3, 1.0),  # a thunderstorm
    "typical": (18, 0.45),
    "soaker": (36, 0.31),
}
REACH_LIKELY = 0.5  # "rain needed" is the least rain giving this chance of reaching min


@dataclass(frozen=True)
class DayCurve:
    day: date
    p_reach: np.ndarray  # P(daylight peak ≥ the range's minimum), per rain amount
    p_too_high_all_day: np.ndarray
    p_runnable: np.ndarray
    rain_needed_in: float | None  # None when even the most rain isn't enough


@dataclass(frozen=True)
class Response:
    rain_in: np.ndarray
    peak: np.ndarray  # rain amounts × (10th, 50th, 90th percentile) peak in band units
    days: list[DayCurve]


def isotonic(values: np.ndarray) -> np.ndarray:
    """The closest nondecreasing sequence (least squares), by pooling adjacent
    violators."""
    blocks: list[list[float]] = []  # [mean, count]
    for v in values:
        blocks.append([float(v), 1.0])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            mean, count = blocks.pop()
            blocks[-1] = [
                (blocks[-1][0] * blocks[-1][1] + mean * count) / (blocks[-1][1] + count),
                blocks[-1][1] + count,
            ]
    return np.concatenate([np.full(int(count), mean) for mean, count in blocks])


def storm(rain_in: float, storm_type: str, start_h: int) -> analog.Storm:
    duration, peak_share = STORM_TYPES[storm_type]
    rain_mm = rain_in * MM_PER_IN
    return analog.Storm(rain_mm, duration, rain_mm * peak_share, start_h=start_h)


def no_rain(library: analog.AnalogLibrary, state: analog.State) -> analog.Forecast:
    """The "no more rain" path: the gauge's recession, as a one-member forecast."""
    hours = analog.HORIZON_H + 1
    if library.recession is not None:
        path = library.recession.project(state.q, hours)
    else:
        path = np.full(hours, state.q)
    return analog.Forecast(state.at, path[None], np.ones(1), [], False)


def rain_response(
    library: analog.AnalogLibrary,
    state: analog.State,
    bands: Bands,
    lat: float,
    lon: float,
    *,
    storm_type: str = "typical",
    start_h: int = 0,
    to_units=None,
    rain_in=RAIN_IN,
) -> Response:
    """`to_units` converts forecast cfs into the bands' units (a rating's `to_ft` for
    reaches rated in feet)."""
    rain_in = np.asarray(rain_in, float)
    convert = to_units or (lambda cfs: cfs)
    peaks, reach, too_high, day_list = [], [], [], None
    for amount in rain_in:
        if amount > 0:
            forecast = library.forecast(state, storm(amount, storm_type, start_h))
        else:
            forecast = no_rain(library, state)
        values = convert(forecast.members)
        days = daily(values, forecast.weights, forecast.start, bands, lat, lon)
        day_list = day_list or [d.day for d in days]
        peak = values.max(axis=1)
        peaks.append(analog.weighted_quantile(peak, forecast.weights, [0.1, 0.5, 0.9]))
        reach.append([1 - d.peak_bands["too_low"] for d in days])
        too_high.append([d.p_too_high_all_day for d in days])

    peak = np.column_stack([isotonic(column) for column in np.array(peaks).T])
    reach_arr, high_arr = np.array(reach), np.array(too_high)
    curves = []
    for n, day in enumerate(day_list or []):
        p_reach = np.clip(isotonic(reach_arr[:, n]), 0, 1)
        p_high = np.minimum(np.clip(isotonic(high_arr[:, n]), 0, 1), p_reach)
        curves.append(
            DayCurve(
                day=day,
                p_reach=p_reach,
                p_too_high_all_day=p_high,
                p_runnable=p_reach - p_high,
                rain_needed_in=_rain_needed(rain_in, p_reach),
            )
        )
    return Response(rain_in, peak, curves)


def _rain_needed(rain_in: np.ndarray, p_reach: np.ndarray) -> float | None:
    if p_reach[0] >= REACH_LIKELY:
        return 0.0
    above = np.flatnonzero(p_reach >= REACH_LIKELY)
    if not len(above):
        return None
    i = above[0]
    # linear between the grid points either side of the threshold
    frac = (REACH_LIKELY - p_reach[i - 1]) / (p_reach[i] - p_reach[i - 1])
    return float(rain_in[i - 1] + frac * (rain_in[i] - rain_in[i - 1]))


def forecast_total(
    periods: list[tuple[datetime, datetime, float, float, float]], now: datetime, through: datetime
) -> tuple[float, float, float]:
    """Total of (start, end, p10, p50, p90) periods in mm between `now` and `through`,
    prorating partial periods. Adding percentiles across periods widens the range (the
    10th percentile of a sum is above the sum of 10th percentiles), so the totals'
    p10–p90 is a conservative range."""
    totals = np.zeros(3)
    for start, end, *values in periods:
        overlap = (min(end, through) - max(start, now)).total_seconds()
        if overlap > 0:
            totals += np.array(values) * overlap / (end - start).total_seconds()
    return tuple(float(t) for t in totals)
