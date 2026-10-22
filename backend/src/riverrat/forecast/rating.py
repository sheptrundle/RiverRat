"""Stage from flow, for reaches whose runnable range is in feet.

Our models forecast discharge (DESIGN.md §6.6), but AW rates about a third of pilot
reach gauges in feet. Most of those gauges record both, so each gets an empirical
rating fitted from its own paired hourly readings: the median stage in bins of log flow
over the last two years, then shifted by its median error over the last 30 days, which
follows datum changes and shifting channels. On held-out recent months that's a median
error of 0.015 ft (worst gauge 0.42 ft; 2026-10-20, 112 gauges).

Above the highest flows seen, stage keeps rising along the slope of the top of the
curve, so a big forecast can still read "too high".
"""

from dataclasses import dataclass

import numpy as np

FIT_DAYS = 730
OFFSET_DAYS = 30
BINS = 40
MIN_PAIRS = 24 * 30
TOP_SHARE = 0.2  # of the flow range, for the slope above it


@dataclass(frozen=True)
class Rating:
    log_cfs: np.ndarray  # ascending
    ft: np.ndarray  # nondecreasing
    slope_above: float  # ft per unit of log(cfs) beyond the last point

    @classmethod
    def fit(cls, cfs: np.ndarray, ft: np.ndarray) -> "Rating | None":
        """From consecutive hourly readings (oldest first, NaN = missing); None with too
        few paired hours in the last two years."""
        recent = np.zeros(len(cfs), bool)
        recent[-24 * FIT_DAYS :] = True
        paired = recent & np.isfinite(cfs) & np.isfinite(ft) & (cfs > 0)
        if paired.sum() < MIN_PAIRS:
            return None
        x, y = np.log(cfs[paired]), ft[paired]
        edges = np.unique(np.quantile(x, np.linspace(0, 1, BINS + 1)))
        if len(edges) < 3:
            return None
        bins = np.clip(np.searchsorted(edges, x, side="right") - 1, 0, len(edges) - 2)
        centers, stages = [], []
        for b in range(len(edges) - 1):
            if (bins == b).any():
                centers.append(np.median(x[bins == b]))
                stages.append(np.median(y[bins == b]))
        stages = np.maximum.accumulate(stages)

        top = x >= x.max() - TOP_SHARE * (x.max() - x.min())
        slope = np.polyfit(x[top], y[top], 1)[0] if np.ptp(x[top]) > 0 else 0.0
        rating = cls(np.array(centers), np.array(stages), max(float(slope), 0.0))

        latest = paired.copy()
        latest[: -24 * OFFSET_DAYS] = False
        if latest.sum() >= 24 * 7:
            offset = float(np.median(ft[latest] - rating.to_ft(cfs[latest])))
            rating = cls(rating.log_cfs, rating.ft + offset, rating.slope_above)
        return rating

    def to_dict(self) -> dict:
        return {
            "log_cfs": self.log_cfs.tolist(),
            "ft": self.ft.tolist(),
            "slope_above": self.slope_above,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Rating":
        return cls(np.array(data["log_cfs"]), np.array(data["ft"]), float(data["slope_above"]))

    def to_ft(self, cfs) -> np.ndarray:
        x = np.log(np.maximum(np.asarray(cfs, float), 1e-3))
        above = np.maximum(x - self.log_cfs[-1], 0)
        return np.interp(x, self.log_cfs, self.ft) + above * self.slope_above
