"""A reach's flow bands (DESIGN.md §8): too low / low / medium / high / too high.

American Whitewater publishes only the bottom and top of a reach's runnable range. Its
low/medium/high labels come from per-reach breakpoints it doesn't publish (no single
split of the range reproduces them), so the middle breakpoints here are ours: thirds of
the range, in log flow for cfs (flow spans orders of magnitude) and linearly for stage
in feet (already compressed). Either can be overridden per reach later.
"""

from dataclasses import dataclass

import numpy as np

TOO_LOW, LOW, MEDIUM, HIGH, TOO_HIGH = range(5)
NAMES = ("too_low", "low", "medium", "high", "too_high")


@dataclass(frozen=True)
class Bands:
    metric: str  # "cfs" or "ft"
    minimum: float | None  # bottom of the runnable range; None when AW gives none
    maximum: float | None

    def __post_init__(self):
        if self.minimum is None and self.maximum is None:
            raise ValueError("a runnable range needs at least one bound")
        if self.minimum is not None and self.maximum is not None:
            if self.maximum <= self.minimum:
                raise ValueError(f"empty range {self.minimum}–{self.maximum}")
            if self.metric == "cfs" and self.minimum <= 0:
                raise ValueError("a cfs range needs a positive minimum")

    @property
    def split(self) -> bool:
        """Whether low/medium/high are defined (both bounds known)."""
        return self.minimum is not None and self.maximum is not None

    def edges(self) -> np.ndarray:
        """Lower edges of low, medium, high, and too high. With one bound missing, the
        whole open range counts as low."""
        low = -np.inf if self.minimum is None else self.minimum
        high = np.inf if self.maximum is None else self.maximum
        if not self.split:
            return np.array([low, high, high, high])
        if self.metric == "cfs":
            ratio = high / low
            return np.array([low, low * ratio ** (1 / 3), low * ratio ** (2 / 3), high])
        step = (high - low) / 3
        return np.array([low, low + step, low + 2 * step, high])

    def classify(self, values) -> np.ndarray:
        """Band of each value (TOO_LOW … TOO_HIGH). The range's bounds are runnable."""
        values = np.asarray(values, float)
        edges = self.edges()
        if self.split:
            band = np.searchsorted(edges[:3], values, side="right")
        else:
            band = np.where(values < edges[0], TOO_LOW, LOW)
        return np.where(values > edges[3], TOO_HIGH, band)

    def runnable(self, values) -> np.ndarray:
        band = self.classify(values)
        return (band != TOO_LOW) & (band != TOO_HIGH)
