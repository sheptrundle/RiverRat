"""How a gauge falls when it stops raining (DESIGN.md §6.4 M0): the "no more rain" path.

Fitted as a master recession curve: over rain-free spells, the median change in
log(1 + flow) across 24 hours, binned by flow. High water falls fast and low water
slowly, so one decay rate doesn't fit; the binned curve captures that without assuming a
functional form.

That curve continues storm hydrographs. On its own it falls too fast for a dry week:
it's fitted mostly on the days right after storms, when quick runoff is still draining,
while a river that has been dry for days runs on groundwater and falls far more slowly
at the same flow. In 2020–22 dry spells it left the observed flow above every path on
53% of day-6 forecasts.

So the "no more rain" forecast (DrySpells) is read straight from history: starting
from each past moment followed by six rain-free days, binned by flow and by whether it
had rained in the 72 h before, the quantiles of the observed change after 1…6 days.
Its paths (one per SPREAD_LEVELS quantile) carry the real spread, so a reach near a band
edge fades from "in" to "out" instead of flipping the hour one line crosses it.
"""

from dataclasses import dataclass

import numpy as np

BINS = 20
MIN_PER_BIN = 20
# A 24 h spell counts as recession when the flow is already falling, with less than this
# much basin rain in the 12 h before (so the last storm's water isn't still arriving) and
# during it (so the next hasn't started). Requiring a longer dry lead-in would leave
# only low water in flashy basins and miss how fast high water drops.
DRY_BEFORE_MM = 1.0
DRY_BEFORE_H = 12
DRY_DURING_MM = 0.5
# The dry-spell forecast's paths: quantiles of the observed change, equally weighted.
SPREAD_LEVELS = (0.05, 0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 0.95)
DRY_DAYS = 6
WET_LOOKBACK_H = 72
WET_MM = 1.0  # this much basin rain in the 72 h before: "recently wet"
DRY_AHEAD_MM = 1.0  # at most this much over the following six days: a dry spell
DRY_STEP_H = 6  # sample a start every 6 hours
DRY_BINS = 12


@dataclass(frozen=True)
class DrySpells:
    """Observed changes in log(1 + cfs) over rain-free days, per class (0: dry for 72 h,
    1: recently wet): bin centers, and bins × DRY_DAYS × SPREAD_LEVELS changes."""

    log_flow: tuple[np.ndarray, ...]
    change: tuple[np.ndarray, ...]

    @classmethod
    def fit(cls, level: np.ndarray, rain: np.ndarray) -> "DrySpells | None":
        cumulative = np.concatenate([[0.0], np.cumsum(rain)])
        horizon = 24 * DRY_DAYS
        i = np.arange(WET_LOOKBACK_H, len(level) - horizon, DRY_STEP_H)
        before = cumulative[i + 1] - cumulative[i + 1 - WET_LOOKBACK_H]
        ahead = cumulative[i + 1 + horizon] - cumulative[i + 1]
        now = cumulative[i + 1] - cumulative[i + 1 - DRY_BEFORE_H]
        later = level[i[:, None] + 24 * np.arange(1, DRY_DAYS + 1)]
        ok = (
            (ahead < DRY_AHEAD_MM)
            & (now < DRY_BEFORE_MM)
            & np.isfinite(level[i])
            & np.isfinite(later).all(axis=1)
        )
        centers, changes = [], []
        for wet in (False, True):
            pick = ok & ((before >= WET_MM) == wet)
            x, change = level[i][pick], later[pick] - level[i][pick][:, None]
            if len(x) < DRY_BINS * MIN_PER_BIN:
                return None
            edges = np.quantile(x, np.linspace(0, 1, DRY_BINS + 1))
            bins = np.clip(np.searchsorted(edges, x, side="right") - 1, 0, DRY_BINS - 1)
            kept = [b for b in range(DRY_BINS) if (bins == b).sum() >= MIN_PER_BIN]
            centers.append(np.array([np.median(x[bins == b]) for b in kept]))
            # slowest fall (highest change) first
            changes.append(
                np.stack(
                    [np.quantile(change[bins == b], SPREAD_LEVELS[::-1], axis=0).T for b in kept]
                )
            )
        return cls(tuple(centers), tuple(changes))

    def paths(self, q0: float, hours: int, wet: bool) -> np.ndarray:
        """SPREAD_LEVELS paths × `hours` hours from `q0` (slowest fall first); past six
        days each path holds its last daily rate."""
        c = int(wet)
        level = np.log1p(max(q0, 0.0))
        by_day = np.array(
            [
                [
                    np.interp(level, self.log_flow[c], self.change[c][:, d, n])
                    for d in range(DRY_DAYS)
                ]
                for n in range(len(SPREAD_LEVELS))
            ]
        )
        days = np.concatenate([np.zeros((len(by_day), 1)), by_day], axis=1)
        h = np.arange(hours) / 24
        out = np.stack([np.interp(h, np.arange(DRY_DAYS + 1), path) for path in days])
        beyond = h > DRY_DAYS
        if beyond.any():
            rate = days[:, -1:] - days[:, -2:-1]
            out[:, beyond] = days[:, -1:] + rate * (h[beyond] - DRY_DAYS)
        return np.expm1(level + out)


@dataclass(frozen=True)
class Recession:
    log_flow: np.ndarray  # bin centers, log(1 + cfs), ascending
    daily_change: np.ndarray  # median 24 h change in log(1 + cfs) at each, ≤ 0
    dry: DrySpells | None = None  # the "no more rain" forecast

    @classmethod
    def fit(cls, cfs: np.ndarray, mm: np.ndarray) -> "Recession | None":
        """None when the record has too few rain-free spells."""
        rain = np.nan_to_num(mm)
        before = np.convolve(rain, np.ones(DRY_BEFORE_H))[: len(rain)]  # through this hour
        during = np.convolve(rain[::-1], np.ones(24))[: len(rain)][::-1]  # from the next
        level = np.log1p(np.maximum(cfs, 0))
        i = np.arange(1, max(len(cfs) - 24, 1))
        ok = (
            (before[i] < DRY_BEFORE_MM)
            & (during[i + 1] < DRY_DURING_MM)
            & (level[i] < level[i - 1])  # falling (False where either is missing)
            & np.isfinite(level[i + 24])
        )
        x, change = level[i][ok], level[i + 24][ok] - level[i][ok]
        if len(x) < BINS * MIN_PER_BIN:
            return None
        edges = np.quantile(x, np.linspace(0, 1, BINS + 1))
        centers, rates = [], []
        for low, high in zip(edges[:-1], edges[1:], strict=True):
            in_bin = (x >= low) & (x <= high)
            if in_bin.sum() >= MIN_PER_BIN:
                centers.append(np.median(x[in_bin]))
                rates.append(min(np.median(change[in_bin]), 0.0))
        if len(centers) < 2:
            return None
        order = np.argsort(centers)
        return cls(np.array(centers)[order], np.array(rates)[order], DrySpells.fit(level, rain))

    def project(self, q0, hours: int) -> np.ndarray:
        """Flow for `hours` hours starting at `q0` (hour 0 is `q0` itself). Works on a
        scalar or an array of starting flows; time is the last axis.

        Steps a whole day at a time, as the curve was fitted, and interpolates the hours
        in between (in log flow)."""
        level = np.log1p(np.maximum(np.asarray(q0, float), 0))
        days = [level]
        for _ in range(-(-hours // 24)):
            days.append(days[-1] + np.interp(days[-1], self.log_flow, self.daily_change))
        days = np.stack(days, axis=-1)
        h = np.arange(hours)
        day, frac = h // 24, (h % 24) / 24
        return np.expm1(days[..., day] * (1 - frac) + days[..., day + 1] * frac)
