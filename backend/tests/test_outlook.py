from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from river_fake import BASE, event_rows, synthetic_river

from riverrat.forecast import outlook
from riverrat.forecast.live import RainOutlook
from riverrat.models import analog
from riverrat.models.recession import Recession

NOW = datetime(2026, 6, 1, 14, tzinfo=UTC)
PERCENTILES = np.array([0.0, 0.0, 1.0, 2.0, 4.0, 6.0, 8.0])  # p05 … p95


@pytest.fixture(scope="module")
def library():
    hours, cfs, mm = synthetic_river()
    return analog.AnalogLibrary(
        event_rows(hours, cfs, mm), hours, cfs, Recession.fit(cfs, mm), 100.0
    )


def rain_outlook(totals: dict[int, float], issued=NOW - timedelta(hours=2)) -> RainOutlook:
    """6-hour periods n = 1, 2, … after `issued`, each PERCENTILES × totals.get(n, 0)."""
    last = max(totals)
    ends = [issued + timedelta(hours=6 * n) for n in range(1, last + 1)]
    values = np.array([PERCENTILES * totals.get(n, 0.0) for n in range(1, last + 1)])
    periods = [
        (e - timedelta(hours=6), e, v[1], v[3], v[5]) for e, v in zip(ends, values, strict=True)
    ]
    return RainOutlook(issued, periods, values)


def test_scenarios_take_one_level_through_every_period():
    rain = outlook.scenario_rain(rain_outlook({1: 1.0, 2: 3.0}), NOW)
    assert rain.shape == (len(outlook.LEVELS), outlook.HOURS)
    # period 1 ends 4 hours after NOW: hours 1–4 get a sixth of it each
    median = list(outlook.LEVELS).index(0.45)
    np.testing.assert_allclose(rain[median, :4], np.interp(45, [25, 50], [1, 2]) / 6)
    np.testing.assert_allclose(rain[median, 4:10], np.interp(45, [25, 50], [1, 2]) * 3 / 6)
    assert rain[:, 10:].sum() == 0
    assert np.all(np.diff(rain.sum(axis=1)) >= 0)  # wetter levels, more rain


def test_a_dry_forecast_follows_the_recession(library):
    state = analog.State(q=500.0, api_mm=5.0, at=NOW)
    found = outlook.ensemble(library, state, None)
    assert found.weights.sum() == pytest.approx(1)
    assert np.all(np.diff(found.members, axis=1) <= 1e-9)
    assert found.scenario_rain_mm.max() == 0


def medians(found, hours=slice(None)) -> np.ndarray:
    """Each scenario's median peak flow over `hours`."""
    peaks = found.members[:, hours].max(axis=1)
    return np.array([np.median(peaks[found.scenario == n]) for n in range(len(outlook.LEVELS))])


def test_wetter_scenarios_run_higher(library):
    state = analog.State(q=BASE, api_mm=5.0, at=NOW)
    found = outlook.ensemble(library, state, rain_outlook({2: 10.0}))  # median 20 mm
    assert found.weights.sum() == pytest.approx(1)
    peak = medians(found)
    assert peak[0] == pytest.approx(BASE, rel=0.01)  # the 5% level is dry
    assert peak[-1] > peak[len(peak) // 2] > BASE


def test_a_second_storm_adds_to_the_first(library):
    state = analog.State(q=BASE, api_mm=5.0, at=NOW)
    one = outlook.ensemble(library, state, rain_outlook({2: 10.0}))
    two = outlook.ensemble(library, state, rain_outlook({2: 10.0, 12: 10.0}))  # 2.5 days apart
    wet = slice(len(outlook.LEVELS) // 2, None)  # scenarios with rain in both periods
    late = slice(70, 100)
    assert np.all(medians(two, late)[wet] > 1.2 * medians(one, late)[wet])
    # before the second storm the paths are the same
    np.testing.assert_allclose(two.members[:, :60], one.members[:, :60])


def test_the_whatif_storm_starts_the_next_evening_with_some_lead():
    from riverrat.jobs.outlook import whatif_storm_start

    assert whatif_storm_start(datetime(2026, 6, 1, 14, tzinfo=UTC)) == datetime(
        2026, 6, 1, 22, tzinfo=UTC
    )
    assert whatif_storm_start(datetime(2026, 6, 1, 20, 30, tzinfo=UTC)) == datetime(
        2026, 6, 2, 22, tzinfo=UTC
    )
