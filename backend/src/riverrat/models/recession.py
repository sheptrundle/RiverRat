"""How a gauge falls when it stops raining (DESIGN.md §6.4 M0): the "no more rain" path.

Fitted as a master recession curve: over rain-free spells, the median change in
log(1 + flow) across 24 hours, binned by flow. High water falls fast and low water
slowly, so one decay rate doesn't fit; the binned curve captures that without assuming a
functional form.
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


@dataclass(frozen=True)
class Recession:
    log_flow: np.ndarray  # bin centers, log(1 + cfs), ascending
    daily_change: np.ndarray  # median 24 h change in log(1 + cfs) at each, ≤ 0

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
        return cls(np.array(centers)[order], np.array(rates)[order])

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
