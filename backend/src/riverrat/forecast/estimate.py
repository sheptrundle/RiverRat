"""Estimates for reaches without a gauge.

Such a reach has no flow record and no runnable range, so both are estimated:

- **Flow:** a similar gauge nearby (a basin 1/4–4× the size, the nearest by basin
  centroid), scaled by watershed area. Its forecasts, what-ifs and history carry over.
- **Range:** AW's ranges on gauged reaches follow watershed size and gradient: the
  bottom of the range, in cfs, from a least-squares fit of log minimum on log area (and
  log gradient when AW gives one), with the top a fixed multiple of it (the median
  max/min). The fit's residuals are how far a real range strays from the fit; each
  forecast member is judged against SPREAD_LEVELS of them, so a reach right at an
  uncertain threshold reads as uncertain.

Pretending 489 gauged reaches (basins under 500 mi²) were ungauged, with the donor never
the reach's own gauge or one on the same stretch, the daily "runnable" call this makes
from the donor's observed flow scored a Brier skill of +34% over a flat 22% and was
reliable without recalibration (said 64% → ran 64%; DESIGN.md, 2026-11-02).
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

# Residual quantiles each member is judged at (equally weighted)
SPREAD_LEVELS = (0.05, 0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 0.95)
MIN_REACHES = 50
DONOR_RATIO = 4.0  # donor basin within this factor of the reach's
DONOR_MAX_KM = 80.0


@dataclass(frozen=True)
class RangeModel:
    # log(minimum cfs) = a + b·log(area mi²) [+ c·log(gradient ft/mi)]
    area: tuple[float, float]
    area_gradient: tuple[float, float, float]
    spread_area: tuple[float, ...]  # residual quantiles at SPREAD_LEVELS
    spread_area_gradient: tuple[float, ...]
    ratio: float  # top of the range ÷ bottom
    reaches: int

    @classmethod
    def fit(cls, area: np.ndarray, gradient: np.ndarray, minimum: np.ndarray, maximum: np.ndarray):
        """From gauged reaches' basin areas, gradients (NaN when unknown) and AW ranges in
        cfs. None with too few reaches."""
        la, lmin = np.log(area), np.log(minimum)
        lg = np.log(np.where(gradient > 0, gradient, np.nan))
        if len(la) < MIN_REACHES:
            return None
        coef_a, spread_a = _fit(np.column_stack([np.ones_like(la), la]), lmin)
        ok = np.isfinite(lg)
        if ok.sum() < MIN_REACHES:
            return None
        coef_ag, spread_ag = _fit(np.column_stack([np.ones(ok.sum()), la[ok], lg[ok]]), lmin[ok])
        return cls(
            tuple(coef_a),
            tuple(coef_ag),
            tuple(spread_a),
            tuple(spread_ag),
            float(np.exp(np.median(np.log(maximum / minimum)))),
            len(la),
        )

    def minimum(self, area_sqmi: float, gradient_fpm: float | None) -> tuple[float, np.ndarray]:
        """The fitted bottom of the range (cfs) and the factors real ranges stray by."""
        if gradient_fpm and gradient_fpm > 0:
            a, b, c = self.area_gradient
            centre = a + b * np.log(area_sqmi) + c * np.log(gradient_fpm)
            spread = self.spread_area_gradient
        else:
            a, b = self.area
            centre = a + b * np.log(area_sqmi)
            spread = self.spread_area
        return float(np.exp(centre)), np.exp(np.array(spread))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RangeModel":
        return cls(**{k: tuple(v) if isinstance(v, list) else v for k, v in data.items()})


def _fit(X: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    coef = np.linalg.lstsq(X, y, rcond=None)[0]
    return coef, np.quantile(y - X @ coef, SPREAD_LEVELS)


def range_model_path(data_dir: Path) -> Path:
    return data_dir / "estimates" / "range_model.json"


def load(path: Path) -> RangeModel:
    return RangeModel.from_dict(json.loads(path.read_text()))
