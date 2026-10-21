from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from river_fake import BASE, event_rows, synthetic_river

from riverrat.forecast import response
from riverrat.forecast.bands import Bands
from riverrat.models import analog
from riverrat.models.recession import Recession

LAT, LON = 38.06, -81.08
NOW = datetime(2026, 6, 1, 14, tzinfo=UTC)  # 10 am EDT


@pytest.fixture(scope="module")
def library():
    hours, cfs, mm = synthetic_river()
    rows = event_rows(hours, cfs, mm)
    return analog.AnalogLibrary(rows, hours, cfs, Recession.fit(cfs, mm), 100.0)


def test_isotonic_pools_violators():
    pooled = response.isotonic(np.array([1, 3, 2, 4, 3, 5.0]))
    np.testing.assert_allclose(pooled, [1, 2.5, 2.5, 3.5, 3.5, 5])
    np.testing.assert_allclose(response.isotonic(np.array([3, 2, 1.0])), [2, 2, 2])
    increasing = np.array([0.0, 0.1, 0.5])
    np.testing.assert_array_equal(response.isotonic(increasing), increasing)


def test_storm_types_set_duration_and_intensity():
    burst = response.storm(1.0, "burst", start_h=6)
    assert (burst.duration_h, burst.max_3h_mm, burst.start_h) == (3, 25.4, 6)
    soaker = response.storm(2.0, "soaker", start_h=0)
    assert soaker.rain_mm == pytest.approx(50.8)
    assert soaker.max_3h_mm == pytest.approx(50.8 * 0.31)


def test_curve_rises_with_rain_and_blows_out(library):
    state = analog.State(q=BASE, api_mm=10.0, at=NOW)
    bands = Bands("cfs", 150, 400)  # the synthetic river reaches 50 + 0.4·mm² cfs
    curve = response.rain_response(library, state, bands, LAT, LON, storm_type="typical")

    assert len(curve.rain_in) == len(response.RAIN_IN)
    assert np.all(np.diff(curve.peak, axis=0) >= 0)  # more rain, higher peak
    assert np.all(curve.peak[:, 0] <= curve.peak[:, 1])
    assert curve.peak[0, 1] == pytest.approx(BASE, rel=0.01)  # no rain: stays at base

    today = curve.days[0]
    assert today.day == NOW.date()
    assert np.all(np.diff(today.p_reach) >= 0)
    assert np.all(np.diff(today.p_too_high_all_day) >= 0)
    np.testing.assert_allclose(today.p_runnable, today.p_reach - today.p_too_high_all_day)
    assert today.p_runnable[0] == 0  # no rain, no run
    assert today.p_runnable.max() > 0.5
    # ~0.6 in (15 mm) gives 50 + 0.4·15² = 140 cfs: the likely threshold is just above
    assert 0.25 < today.rain_needed_in < 1.25


def test_no_rain_follows_the_recession(library):
    state = analog.State(q=500.0, api_mm=10.0, at=NOW)
    forecast = response.no_rain(library, state)
    assert forecast.members.shape == (1, analog.HORIZON_H + 1)
    assert forecast.members[0, 0] == pytest.approx(500.0)
    assert np.all(np.diff(forecast.members[0]) <= 0)


def test_rain_needed():
    rain = np.array([0, 0.5, 1.0, 1.5])
    assert response._rain_needed(rain, np.array([0.6, 0.7, 0.8, 0.9])) == 0.0
    assert response._rain_needed(rain, np.array([0.0, 0.2, 0.6, 0.9])) == pytest.approx(0.875)
    assert response._rain_needed(rain, np.array([0.0, 0.1, 0.2, 0.3])) is None


def test_forecast_total_prorates_periods():
    now = datetime(2026, 10, 20, 15, tzinfo=UTC)
    periods = [
        (now - timedelta(hours=3), now + timedelta(hours=3), 0.0, 6.0, 12.0),  # half ahead
        (now + timedelta(hours=3), now + timedelta(hours=9), 1.0, 2.0, 3.0),
        (now + timedelta(hours=9), now + timedelta(hours=15), 5.0, 5.0, 5.0),  # after
    ]
    assert response.forecast_total(periods, now, now + timedelta(hours=9)) == pytest.approx(
        (1.0, 5.0, 9.0)
    )
