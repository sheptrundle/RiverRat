from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest

from riverrat.forecast import bands as b
from riverrat.forecast.bands import Bands
from riverrat.forecast.daily import daily
from riverrat.forecast.daylight import sun_times
from riverrat.forecast.rating import Rating

FAYETTEVILLE = (38.06, -81.08)


# --- bands ---------------------------------------------------------------------------


def test_cfs_bands_split_the_range_in_thirds_of_log_flow():
    bands = Bands("cfs", 100, 10_000)
    np.testing.assert_allclose(bands.edges(), np.geomspace(100, 10_000, 4))
    assert list(bands.classify([50, 100, 300, 1000, 9999, 10_000, 10_001])) == [
        b.TOO_LOW, b.LOW, b.LOW, b.MEDIUM, b.HIGH, b.HIGH, b.TOO_HIGH,
    ]  # fmt: skip


def test_ft_bands_split_linearly():
    bands = Bands("ft", 2.0, 5.0)
    np.testing.assert_allclose(bands.edges(), [2, 3, 4, 5])
    assert list(bands.classify([1.9, 2.5, 3.5, 4.5, 5.1])) == [0, 1, 2, 3, 4]


def test_one_sided_ranges_have_a_single_runnable_band():
    only_min = Bands("cfs", 300, None)
    assert list(only_min.classify([299, 300, 1e6])) == [b.TOO_LOW, b.LOW, b.LOW]
    only_max = Bands("cfs", None, 2000)
    assert list(only_max.classify([1, 2000, 2001])) == [b.LOW, b.LOW, b.TOO_HIGH]
    assert not only_min.split


def test_bands_reject_unusable_ranges():
    for low, high in [(None, None), (500, 400), (0, 400)]:
        with pytest.raises(ValueError):
            Bands("cfs", low, high)


# --- rating --------------------------------------------------------------------------


def test_rating_follows_the_recent_relation_and_extends_above_it():
    rng = np.random.default_rng(0)
    n = 24 * 800
    cfs = np.exp(rng.uniform(np.log(50), np.log(5000), n))
    ft = 1.2 * np.log(cfs) - 3 + rng.normal(0, 0.02, n)
    ft[-24 * 40 :] += 0.5  # the gauge datum moved recently
    cfs[::7] = np.nan

    rating = Rating.fit(cfs, ft)
    for q in (100.0, 1000.0, 4000.0):
        assert rating.to_ft(q) == pytest.approx(1.2 * np.log(q) - 3 + 0.5, abs=0.05)
    # beyond the record: still rising, at about the curve's slope
    assert rating.to_ft(20_000) == pytest.approx(1.2 * np.log(20_000) - 3 + 0.5, abs=0.2)


def test_rating_needs_enough_paired_readings():
    assert Rating.fit(np.full(100, 10.0), np.full(100, 1.0)) is None
    assert Rating.fit(np.full(24 * 60, np.nan), np.ones(24 * 60)) is None


# --- daylight ------------------------------------------------------------------------


def test_sun_times_match_published_tables():
    # From api.sunrise-sunset.org for Fayetteville, WV.
    for day, expected in [
        (date(2026, 10, 20), ("2026-10-20T11:36:46", "2026-10-20T22:41:21")),
        (date(2026, 6, 21), ("2026-06-21T10:00:21", "2026-06-22T00:52:02")),
    ]:
        for got, want in zip(sun_times(day, *FAYETTEVILLE), expected, strict=True):
            want = datetime.fromisoformat(want).replace(tzinfo=UTC)
            assert abs(got - want) < timedelta(minutes=4)


# --- daily ---------------------------------------------------------------------------


def test_daily_products_from_members():
    start = datetime(2026, 10, 20, 15, tzinfo=UTC)  # 11 am EDT
    hours = np.arange(73)
    times = [start + timedelta(hours=int(h)) for h in hours]
    sunrise1, sunset1 = sun_times(date(2026, 10, 21), *FAYETTEVILLE)
    tomorrow = np.array([sunrise1 <= t <= sunset1 for t in times])

    low_all_week = np.full(73, 50.0)
    in_tomorrow = np.where(tomorrow, 800.0, 50.0)  # medium tomorrow only
    huge = np.full(73, 50_000.0)  # too high throughout
    members = np.stack([low_all_week, in_tomorrow, huge])
    weights = np.array([0.5, 0.3, 0.2])

    days = daily(members, weights, start, Bands("cfs", 100, 10_000), *FAYETTEVILLE)
    assert [d.day for d in days] == [date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22)]
    today, tomorrow_day, after = days
    assert today.p_runnable == pytest.approx(0.0)
    assert tomorrow_day.p_runnable == pytest.approx(0.3)
    assert tomorrow_day.peak_bands == pytest.approx(
        {"too_low": 0.5, "low": 0.0, "medium": 0.3, "high": 0.0, "too_high": 0.2}
    )
    assert tomorrow_day.p_too_high_all_day == pytest.approx(0.2)
    assert tomorrow_day.hours_in_range == 0  # the median member is never in range
    assert after.p_runnable == pytest.approx(0.0)
    assert today.sunrise < start  # today counts only the daylight that's left


def test_daily_drops_days_the_forecast_doesnt_cover():
    start = datetime(2026, 10, 20, 23, tzinfo=UTC)  # after sunset
    members = np.full((1, 20), 500.0)  # ends the next afternoon
    days = daily(members, np.ones(1), start, Bands("cfs", 100, 1000), *FAYETTEVILLE)
    assert days == []


def test_reliability_bins():
    from riverrat.jobs.hindcast import brier, reliability

    p = np.array([0.1, 0.1, 0.9, 0.9])
    outcome = np.array([0.0, 0.0, 1.0, 0.0])
    assert brier(p, outcome) == pytest.approx((0.01 + 0.01 + 0.01 + 0.81) / 4)
    low, high = reliability(p, outcome)
    assert low[2:] == (2, pytest.approx(0.1), 0.0)
    assert high[2:] == (2, pytest.approx(0.9), 0.5)
