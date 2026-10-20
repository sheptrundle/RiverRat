"""M1, the analog model (DESIGN.md §6.4): "how did this gauge react the other times it got
about X rain at about Y level?"

The library is a gauge's past storm events (features/events.py), each with the hourly
flow that followed. A forecast finds the K events most like the query (flow when the rain
starts, rain total, duration, intensity, how wet the basin was, season), and replays each
one's hydrograph from today's flow:

    log(1 + Q(t)) = log(1 + q_start) + s · [log(1 + Q_a(t)) − log(1 + q0_a)]

where the analog's rise is stretched by s = today's rain ÷ its rain (clipped). Without
the stretch, the biggest storms have few neighbors with even more rain and come out low:
in the 2020–22 hindcast, peaks of 1 in+ storms were 30% low without it and 10% low with.
The K replays, weighted by similarity, form the forecast distribution.

Each analog's hydrograph is observed until its response window ends (the next storm, or
4 days after the rain) and continues on the gauge's recession curve from there, so every
replay is the response to a single storm.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np

from riverrat.features import events
from riverrat.models.recession import Recession

HORIZON_H = 144
K = 60
# Feature weights after standardizing (tuned on 2020–22 storms, library ≤ 2019).
WEIGHTS = {
    "flow": 1.0,
    "rain": 1.0,
    "duration": 0.5,
    "intensity": 0.5,
    "wetness": 0.5,
    "season_sin": 0.5,
    "season_cos": 0.5,
}
RAIN_STRETCH = (0.5, 2.0)
# No replay may send more water past the gauge (above its starting flow, over the
# horizon) than this share of the storm's rain. Stretching a rise in log space can
# otherwise invent impossible floods when the rain exceeds everything in the library.
MAX_RUNOFF_RATIO = 0.8
# Library events must have peaked inside their window and have nearly complete flow.
MIN_LIBRARY_COVERAGE = 0.95


@dataclass(frozen=True)
class State:
    """The gauge now: flow (cfs), antecedent precipitation index (mm), and the time."""

    q: float
    api_mm: float
    at: datetime


@dataclass(frozen=True)
class Storm:
    """Basin rain to come. Its first wet hour ends `start_h + 1` hours from now."""

    rain_mm: float
    duration_h: int
    max_3h_mm: float
    start_h: int = 0


@dataclass(frozen=True)
class Analog:
    start: datetime
    rain_mm: float
    duration_h: int
    q0: float
    qpeak: float
    t_peak_h: int
    weight: float


@dataclass(frozen=True)
class Forecast:
    start: datetime  # hour 0 of the members: now
    members: np.ndarray  # replays × hours 0…HORIZON_H, cfs
    weights: np.ndarray  # sums to 1
    analogs: list[Analog]  # most similar first
    # More rain than any of the matched storms: the replays are stretched beyond what this
    # gauge has been seen to do, so treat the numbers as rough.
    extrapolated: bool

    def quantiles(self, levels) -> np.ndarray:
        """Flow quantiles: levels × hours."""
        return np.stack(
            [
                weighted_quantile(self.members[:, h], self.weights, levels)
                for h in range(self.members.shape[1])
            ],
            axis=1,
        )

    def peak_quantiles(self, levels) -> np.ndarray:
        return weighted_quantile(self.members.max(axis=1), self.weights, levels)


def weighted_quantile(values: np.ndarray, weights: np.ndarray, levels) -> np.ndarray:
    order = np.argsort(values)
    values, weights = values[order], weights[order]
    position = (np.cumsum(weights) - weights / 2) / weights.sum()
    return np.interp(levels, position, values)


def _features(q0, rain_mm, duration_h, max_3h_mm, api_mm, day_of_year) -> np.ndarray:
    angle = 2 * np.pi * np.asarray(day_of_year, float) / 365.25
    columns = {
        "flow": np.log1p(np.maximum(q0, 0)),
        "rain": np.log(rain_mm),
        "duration": np.log(duration_h),
        "intensity": np.log(max_3h_mm),
        "wetness": np.log1p(api_mm),
        "season_sin": np.sin(angle),
        "season_cos": np.cos(angle),
    }
    return np.column_stack([np.asarray(columns[name], float) for name in WEIGHTS])


class AnalogLibrary:
    """One gauge's storm events and the hydrographs that followed them."""

    def __init__(
        self,
        event_rows: list[dict],
        hours: np.ndarray,
        cfs: np.ndarray,
        recession: Recession | None,
        area_sqmi: float | None,
    ):
        """`event_rows` are rows of the events table for this gauge; `hours`/`cfs` its
        hourly table (hour ends as datetime64[s]). Without `area_sqmi`, runoff isn't
        capped."""
        rows = [
            r
            for r in event_rows
            if r["peak_complete"] and r["flow_coverage"] >= MIN_LIBRARY_COVERAGE
        ]
        self.recession = recession
        self.area_sqmi = area_sqmi
        self.events = rows
        first = hours[0].astype("datetime64[s]").astype(np.int64)
        self.trajectories = np.array(
            [
                self._trajectory(cfs, (int(r["start"].timestamp()) - first) // 3600, r["window_h"])
                for r in rows
            ]
        ).reshape(len(rows), HORIZON_H)
        self.q0 = np.array([r["q0"] for r in rows], float)
        self.rain_mm = np.array([r["rain_mm"] for r in rows], float)
        raw = _features(
            self.q0,
            self.rain_mm,
            [r["duration_h"] for r in rows],
            [r["max_3h_mm"] for r in rows],
            [r["api_mm"] for r in rows],
            [r["start"].timetuple().tm_yday for r in rows],
        )
        self._mean = raw.mean(axis=0) if rows else 0.0
        self._scale = raw.std(axis=0) + 1e-9 if rows else 1.0
        self._weights = np.array(list(WEIGHTS.values()))
        self._points = self._standardize(raw)

    def __len__(self) -> int:
        return len(self.events)

    def _standardize(self, raw: np.ndarray) -> np.ndarray:
        return (raw - self._mean) / self._scale * self._weights

    def _trajectory(self, cfs: np.ndarray, start: int, window_h: int) -> np.ndarray:
        n = min(window_h, HORIZON_H, len(cfs) - start)
        observed = cfs[start : start + n].astype(float)
        good = np.isfinite(observed)
        observed = np.interp(np.arange(n), np.flatnonzero(good), observed[good])
        if n == HORIZON_H:
            return observed
        if self.recession is None:
            tail = np.full(HORIZON_H - n, observed[-1])
        else:
            tail = self.recession.project(observed[-1], HORIZON_H - n + 1)[1:]
        return np.concatenate([observed, tail])

    def _cap_runoff(self, replays: np.ndarray, q_start: float, rain_mm: float) -> np.ndarray:
        if not self.area_sqmi:
            return replays
        above = np.maximum(replays - q_start, 0)
        runoff_mm = (
            above.sum(axis=1) * events.CFS_HOURS_TO_M3 / (self.area_sqmi * events.SQMI_TO_M2) * 1000
        )
        shrink = np.minimum(1.0, MAX_RUNOFF_RATIO * rain_mm / np.maximum(runoff_mm, 1e-9))
        return replays - above * (1 - shrink)[:, None]

    def forecast(
        self,
        state: State,
        storm: Storm,
        *,
        k: int | None = K,
        similar: bool = True,
        stretch: bool = True,
    ) -> Forecast:
        """Replays of the `k` most similar events (all when None). `similar=False`
        weights them equally and `stretch=False` skips the rain stretch: the
        no-matching baseline in the hindcast."""
        if not len(self):
            raise ValueError("empty analog library")
        lead = storm.start_h
        if lead and self.recession is not None:
            before = self.recession.project(state.q, lead + 1)
        else:
            before = np.full(lead + 1, state.q)
        q_start = float(before[-1])
        storm_at = state.at + timedelta(hours=lead)
        query = self._standardize(
            _features(
                [q_start],
                [storm.rain_mm],
                [storm.duration_h],
                [storm.max_3h_mm],
                [state.api_mm * events.API_DAILY_DECAY ** (lead / 24)],
                [storm_at.timetuple().tm_yday],
            )
        )[0]

        distance = np.sqrt(((self._points - query) ** 2).sum(axis=1))
        order = np.argsort(distance)[: k or len(self)]
        if similar:
            reach = distance[order[-1]] * 1.0001 + 1e-9
            weights = (1 - (distance[order] / reach) ** 3) ** 3 + 1e-6  # tricube
        else:
            weights = np.ones(len(order))
        weights /= weights.sum()

        s = np.clip(storm.rain_mm / self.rain_mm[order], *RAIN_STRETCH) if stretch else 1.0
        rise = np.log1p(self.trajectories[order]) - np.log1p(self.q0[order])[:, None]
        replays = np.expm1(np.log1p(q_start) + rise * np.reshape(s, (-1, 1)))
        replays = self._cap_runoff(replays, q_start, storm.rain_mm)
        members = np.concatenate(
            [np.broadcast_to(before, (len(order), lead + 1)), replays], axis=1
        )[:, : HORIZON_H + 1]

        by_weight = sorted(zip(weights, order, strict=True), key=lambda pair: -pair[0])
        analogs = [
            Analog(
                start=self.events[i]["start"].astimezone(UTC),
                rain_mm=float(self.events[i]["rain_mm"]),
                duration_h=int(self.events[i]["duration_h"]),
                q0=float(self.events[i]["q0"]),
                qpeak=float(self.events[i]["qpeak"]),
                t_peak_h=int(self.events[i]["t_peak_h"]),
                weight=float(w),
            )
            for w, i in by_weight
        ]
        extrapolated = bool(storm.rain_mm > self.rain_mm[order].max())
        return Forecast(state.at, members, weights, analogs, extrapolated)
