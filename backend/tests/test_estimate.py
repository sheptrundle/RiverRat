import numpy as np
import pytest

from riverrat.forecast.estimate import SPREAD_LEVELS, RangeModel


def synthetic(n=400, seed=1):
    rng = np.random.default_rng(seed)
    area = np.exp(rng.uniform(np.log(5), np.log(1000), n))
    gradient = np.exp(rng.uniform(np.log(10), np.log(300), n))
    noise = rng.normal(0, 0.3, n)
    minimum = np.exp(3.0 + 0.6 * np.log(area) - 0.4 * np.log(gradient) + noise)
    return area, gradient, minimum, minimum * 8


def test_fit_recovers_size_and_gradient():
    area, gradient, low, high = synthetic()
    model = RangeModel.fit(area, gradient, low, high)
    _, b, c = model.area_gradient
    assert (b, c) == pytest.approx((0.6, -0.4), abs=0.1)
    assert model.ratio == pytest.approx(8)
    # residual spread: the noise's quantiles, in order
    assert len(model.spread_area_gradient) == len(SPREAD_LEVELS)
    assert np.all(np.diff(model.spread_area_gradient) > 0)
    assert model.spread_area_gradient[-1] == pytest.approx(0.3 * 1.645, abs=0.1)


def test_minimum_falls_back_to_size_alone():
    area, gradient, low, high = synthetic()
    model = RangeModel.fit(area, gradient, low, high)
    with_gradient, strays = model.minimum(100, 50)
    assert with_gradient == pytest.approx(
        np.exp(3 + 0.6 * np.log(100) - 0.4 * np.log(50)), rel=0.15
    )
    assert len(strays) == len(SPREAD_LEVELS)
    without, wider = model.minimum(100, None)
    assert without > 0 and np.ptp(np.log(wider)) > np.ptp(np.log(strays))
    assert RangeModel.from_dict(model.to_dict()) == model


def test_too_few_reaches():
    area, gradient, low, high = synthetic(n=20)
    assert RangeModel.fit(area, gradient, low, high) is None
