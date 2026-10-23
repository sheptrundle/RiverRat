"""The Outlook (DESIGN.md §6.2–6.3): each gauge's flow over the next 6 days under NOAA's
rain forecast, as a weighted set of possible futures.

Rain scenarios. NBM gives each 6-hour period's basin rain as percentiles. A scenario
takes the same percentile level in every period (a wet period implies wet neighbors,
DESIGN.md §6.2 approximation 2) and spreads each period's rain evenly over its hours.
Ten equally weighted levels from 5% to 95% stand for the forecast's range. Taking one
level through every period overstates the spread of the total, so the ranges come out on
the wide side until calibration on archived forecasts corrects them.

Several storms. The analog model forecasts one storm at a time, so a scenario's rain is
split into storms the same way the library's were (features/events.py). The first storm
is forecast from the current state. Each later one is forecast from the median flow
when it starts, and its rise above the no-storm recession is added, in log flow, to
every path so far. Paths are paired by rank (the wettest path so far gets the biggest
rise), which keeps a wet scenario wet rather than averaging it out.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import numpy as np

from riverrat.features import events
from riverrat.forecast.live import RainOutlook
from riverrat.forecast.response import no_rain
from riverrat.models import analog
from riverrat.sources.nbm import PERCENTILES

LEVELS = np.round(np.arange(0.05, 1.0, 0.1), 2)  # 5%, 15%, …, 95%
HOURS = analog.HORIZON_H


@dataclass(frozen=True)
class Ensemble:
    start: datetime  # hour 0 of the members: the gauge's latest reading
    members: np.ndarray  # paths × hours 0…HORIZON_H, cfs
    weights: np.ndarray  # sums to 1
    scenario: np.ndarray  # each path's scenario (index into LEVELS)
    scenario_rain_mm: np.ndarray  # each scenario's total basin rain over the horizon
    extrapolated: bool  # some storm was bigger than any matched past storm
    rain_through: datetime | None  # end of the forecast's last period (later hours: dry)


def scenario_rain(outlook: RainOutlook | None, start: datetime) -> np.ndarray:
    """Hourly basin rain (mm) for each level: levels × HOURS, where column i is the hour
    ending at start + i + 1 hours. Hours the forecast doesn't cover are dry."""
    rain = np.zeros((len(LEVELS), HOURS))
    if outlook is None:
        return rain
    for (period_start, period_end, *_), values in zip(
        outlook.periods, outlook.percentiles, strict=True
    ):
        at_levels = np.interp(LEVELS * 100, PERCENTILES, values)
        hours = int((period_end - period_start) / timedelta(hours=1))
        for h in range(hours):
            ends = period_start + timedelta(hours=h + 1)
            i = int((ends - start) / timedelta(hours=1)) - 1
            if 0 <= i < HOURS:
                rain[:, i] = at_levels / hours
    return rain


def ensemble(
    library: analog.AnalogLibrary, state: analog.State, outlook: RainOutlook | None
) -> Ensemble:
    rain = scenario_rain(outlook, state.at)
    members, weights, scenario, extrapolated = [], [], [], False
    for n, scenario_mm in enumerate(rain):
        paths, path_weights, stretched = _scenario(library, state, scenario_mm)
        members.append(paths)
        weights.append(path_weights / len(rain))
        scenario.append(np.full(len(paths), n))
        extrapolated |= stretched
    return Ensemble(
        start=state.at,
        members=np.concatenate(members),
        weights=np.concatenate(weights),
        scenario=np.concatenate(scenario),
        scenario_rain_mm=rain.sum(axis=1),
        extrapolated=extrapolated,
        rain_through=outlook.periods[-1][1] if outlook else None,
    )


def _scenario(
    library: analog.AnalogLibrary, state: analog.State, rain: np.ndarray
) -> tuple[np.ndarray, np.ndarray, bool]:
    storms = events.find_storms(rain)
    if not storms:
        forecast = no_rain(library, state)
        return forecast.members, forecast.weights, False
    api = _api_path(state.api_mm, rain)
    paths, weights, extrapolated = None, None, False
    for first, last in storms:
        storm = analog.Storm(
            rain_mm=float(rain[first : last + 1].sum()),
            duration_h=last - first + 1,
            max_3h_mm=float(_max_3h(rain[first : last + 1])),
        )
        if paths is None:
            forecast = library.forecast(state, replace(storm, start_h=first))
            paths, weights = forecast.members, forecast.weights
        else:
            # hour `first` of the paths is the start of this storm's first wet hour
            q = float(analog.weighted_quantile(paths[:, first], weights, [0.5])[0])
            at = state.at + timedelta(hours=first)
            forecast = library.forecast(analog.State(q, api[first], at), storm)
            paths = _add_rise(paths, weights, forecast, library, q, first)
        extrapolated |= forecast.extrapolated
    return paths, weights, extrapolated


def _add_rise(paths, weights, forecast, library, q, offset) -> np.ndarray:
    """Add the storm's rise above the no-storm recession (from flow `q` at hour `offset`)
    to each path from that hour on, pairing paths and replays by rank."""
    span = HOURS + 1 - offset
    if library.recession is not None:
        baseline = library.recession.project(q, span)
    else:
        baseline = np.full(span, q)
    rise = np.maximum(np.log1p(forecast.members[:, :span]) - np.log1p(baseline), 0)
    # each path's rank among the paths, by flow when the storm starts
    rank = _ranks(paths[:, offset], weights)
    order = np.argsort(rise.max(axis=1))
    position = _ranks(rise.max(axis=1)[order], forecast.weights[order], presorted=True)
    pick = order[np.clip(np.searchsorted(position, rank), 0, len(order) - 1)]
    out = paths.copy()
    out[:, offset:] = np.expm1(np.log1p(paths[:, offset:]) + rise[pick])
    return out


def _ranks(values: np.ndarray, weights: np.ndarray, presorted: bool = False) -> np.ndarray:
    """Each value's weighted rank in (0, 1): the cumulative weight at its midpoint."""
    order = np.arange(len(values)) if presorted else np.argsort(values, kind="stable")
    w = weights[order] / weights.sum()
    position = np.empty(len(values))
    position[order] = np.cumsum(w) - w / 2
    return position


def _api_path(api_now: float, rain: np.ndarray) -> np.ndarray:
    """The antecedent index at the start of each hour, from today's and the scenario's
    rain."""
    decay = events.API_DAILY_DECAY ** (1 / 24)
    out = np.empty(len(rain))
    level = api_now
    for i, mm in enumerate(rain):
        out[i] = level
        level = level * decay + mm
    return out


def _max_3h(rain: np.ndarray) -> float:
    if len(rain) < 3:
        return float(rain.sum())
    return float(np.convolve(rain, np.ones(3), "valid").max())
