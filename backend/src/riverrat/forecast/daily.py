"""From forecast flow to what a paddler asks (DESIGN.md §6.3): will the reach be runnable
at some point during daylight on each day, and how high?

Every forecast member is a possible future; each day's numbers are the weighted share of
members that do something during that day's daylight (sunrise to sunset at the gauge).
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np

from riverrat.forecast import bands as band_names
from riverrat.forecast.bands import Bands
from riverrat.forecast.daylight import sun_times
from riverrat.models.analog import weighted_quantile


@dataclass(frozen=True)
class Day:
    day: date
    sunrise: datetime
    sunset: datetime
    p_runnable: float  # in range at any daylight hour
    peak_bands: dict[str, float]  # chance the daylight peak lands in each band
    p_too_high_all_day: float
    peak: tuple[float, float, float]  # 10th, 50th, 90th percentile daylight peak
    hours_in_range: float  # median


def daily(
    values: np.ndarray,
    weights: np.ndarray,
    start: datetime,
    bands: Bands,
    lat: float,
    lon: float,
) -> list[Day]:
    """`values` are members × hours in the bands' units, hour h at `start + h hours`.
    Covers each day from the start's through the last whose daylight the forecast fully
    spans (today may be partial: the hours left)."""
    times = [start + timedelta(hours=h) for h in range(values.shape[1])]
    end = times[-1]
    days = []
    day = start.date()
    while True:
        sunrise, sunset = sun_times(day, lat, lon)
        if sunset > end:
            break
        hours = [h for h, t in enumerate(times) if sunrise <= t <= sunset]
        if hours:
            days.append(_day(day, sunrise, sunset, values[:, hours], weights, bands))
        day += timedelta(days=1)
    return days


def _day(day, sunrise, sunset, values, weights, bands: Bands) -> Day:
    band = bands.classify(values)
    in_range = (band != band_names.TOO_LOW) & (band != band_names.TOO_HIGH)
    peaks = values.max(axis=1)
    peak_band = bands.classify(peaks)
    p10, p50, p90 = weighted_quantile(peaks, weights, [0.1, 0.5, 0.9])
    return Day(
        day=day,
        sunrise=sunrise,
        sunset=sunset,
        p_runnable=float(weights @ in_range.any(axis=1)),
        peak_bands={
            name: float(weights @ (peak_band == b)) for b, name in enumerate(band_names.NAMES)
        },
        p_too_high_all_day=float(weights @ (band == band_names.TOO_HIGH).all(axis=1)),
        peak=(float(p10), float(p50), float(p90)),
        hours_in_range=float(weighted_quantile(in_range.sum(axis=1), weights, [0.5])[0]),
    )
