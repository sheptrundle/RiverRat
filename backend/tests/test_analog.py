from datetime import UTC, datetime, timedelta

import numpy as np
import pyarrow.parquet as pq
import pytest

from riverrat.features import events, hourly
from riverrat.jobs import events as event_jobs
from riverrat.jobs import hindcast
from riverrat.models import analog
from riverrat.models.recession import Recession
from riverrat.storage import write_parquet

START = datetime(2016, 1, 1, 1, tzinfo=UTC)
BASE = 50.0
DECAY = 0.05  # per hour, toward BASE


def synthetic_river(years: int = 6, seed: int = 1):
    """A basin that gets a storm every 4–8 days (5–60 mm over 3–12 h) and a river whose
    excess flow is a leaky bucket: each mm of rain adds 0.4·mm² cfs, decaying 5%/hour."""
    rng = np.random.default_rng(seed)
    n = years * 8760
    mm = np.zeros(n)
    t = 48
    while t < n - 24:
        duration = int(rng.integers(3, 13))
        mm[t : t + duration] = rng.uniform(5, 60) / duration
        t += duration + int(rng.integers(96, 192))
    cfs = np.empty(n)
    excess = 0.0
    storm_total = 0.0
    for i in range(n):
        storm_total = storm_total + mm[i] if mm[i] else 0.0
        excess = excess * (1 - DECAY) + 0.4 * (storm_total**2 - (storm_total - mm[i]) ** 2)
        cfs[i] = BASE + excess
    hours = hourly.hour_ends(START, START + timedelta(hours=n - 1))
    return hours, cfs.astype(np.float32), mm.astype(np.float32)


def event_rows(hours, cfs, mm, area=100.0) -> list[dict]:
    found = events.extract_events(cfs, mm, area)
    table = hourly.to_table(hours, cfs, mm)
    return event_jobs.event_table("01234567", table["ts"], found).to_pylist()


@pytest.fixture(scope="module")
def river():
    hours, cfs, mm = synthetic_river()
    return hours, cfs, mm, event_rows(hours, cfs, mm)


# --- recession -----------------------------------------------------------------------


def test_recession_learns_the_decay():
    hours, cfs, mm = synthetic_river()
    recession = Recession.fit(cfs, mm)
    # within the range of flows seen falling after 12 dry hours
    path = recession.project(BASE + 350, 49)
    expected = BASE + 350 * (1 - DECAY) ** np.arange(49)
    np.testing.assert_allclose(path[::24], expected[::24], rtol=0.03)  # fitted daily steps
    np.testing.assert_allclose(path, expected, rtol=0.15)  # hours in between, interpolated
    assert path[0] == pytest.approx(BASE + 350)
    assert recession.project(np.array([500.0, 900.0]), 5).shape == (2, 5)


def test_recession_needs_dry_spells():
    assert Recession.fit(np.full(500, 10.0), np.ones(500)) is None


# --- analog --------------------------------------------------------------------------


def library_for(river, area=100.0) -> analog.AnalogLibrary:
    hours, cfs, mm, rows = river
    return analog.AnalogLibrary(rows, hours, cfs, Recession.fit(cfs, mm), area)


def test_forecast_replays_similar_storms(river):
    library = library_for(river)
    assert len(library) > 200
    state = analog.State(q=BASE, api_mm=10.0, at=datetime(2026, 6, 1, tzinfo=UTC))
    storm = analog.Storm(rain_mm=30.0, duration_h=6, max_3h_mm=15.0)
    forecast = library.forecast(state, storm)

    assert forecast.members.shape == (analog.K, analog.HORIZON_H + 1)
    assert forecast.weights.sum() == pytest.approx(1.0)
    np.testing.assert_array_equal(forecast.members[:, 0], BASE)  # hour 0 is now
    # The true response peaks at 0.4·30² = 360 cfs above base, give or take decay
    # during the storm.
    p50 = forecast.peak_quantiles([0.5])[0]
    assert 250 < p50 - BASE < 400
    weights = [a.weight for a in forecast.analogs]
    assert weights == sorted(weights, reverse=True)
    assert abs(forecast.analogs[0].rain_mm - 30.0) < 10
    assert not forecast.extrapolated
    q = forecast.quantiles([0.1, 0.5, 0.9])
    assert q.shape == (3, analog.HORIZON_H + 1)
    assert np.all(q[0] <= q[1]) and np.all(q[1] <= q[2])


def test_more_rain_means_a_higher_peak(river):
    library = library_for(river)
    state = analog.State(q=BASE, api_mm=10.0, at=datetime(2026, 6, 1, tzinfo=UTC))
    peaks = [
        library.forecast(state, analog.Storm(rain, 6, rain / 2)).peak_quantiles([0.5])[0]
        for rain in (10, 20, 40, 60)
    ]
    assert peaks == sorted(peaks)


def test_storm_later_in_the_week_follows_the_recession_until_then(river):
    library = library_for(river)
    state = analog.State(q=400.0, api_mm=10.0, at=datetime(2026, 6, 1, tzinfo=UTC))
    forecast = library.forecast(state, analog.Storm(30.0, 6, 15.0, start_h=24))
    expected = library.recession.project(400.0, 25)
    np.testing.assert_allclose(forecast.members[:, :25], np.broadcast_to(expected, (analog.K, 25)))
    assert forecast.members[:, 25:].max() > expected[-1]


def test_rain_beyond_the_record_is_flagged_and_capped(river):
    hours, cfs, mm, rows = river
    state = analog.State(q=BASE, api_mm=10.0, at=datetime(2026, 6, 1, tzinfo=UTC))
    storm = analog.Storm(rain_mm=200.0, duration_h=12, max_3h_mm=60.0)

    uncapped = analog.AnalogLibrary(rows, hours, cfs, Recession.fit(cfs, mm), None)
    tiny_basin = analog.AnalogLibrary(rows, hours, cfs, Recession.fit(cfs, mm), 1.0)
    free = uncapped.forecast(state, storm)
    capped = tiny_basin.forecast(state, storm)
    assert free.extrapolated and capped.extrapolated

    runoff_mm = (
        np.maximum(capped.members - BASE, 0).sum(axis=1)
        * events.CFS_HOURS_TO_M3
        / events.SQMI_TO_M2
        * 1000
    )
    assert runoff_mm.max() <= analog.MAX_RUNOFF_RATIO * 200.0 * 1.0001
    assert capped.peak_quantiles([0.5])[0] < free.peak_quantiles([0.5])[0]


def test_library_skips_events_cut_short(river):
    hours, cfs, mm, rows = river
    cut = [{**r, "peak_complete": False} for r in rows[:10]] + rows[10:]
    library = analog.AnalogLibrary(cut, hours, cfs, None, None)
    assert len(library) == len(library_for(river)) - 10
    with pytest.raises(ValueError, match="empty"):
        analog.AnalogLibrary([], hours, cfs, None, None).forecast(
            analog.State(BASE, 0, START), analog.Storm(10, 3, 5)
        )


def test_weighted_quantile():
    values = np.array([3.0, 1.0, 2.0])
    assert analog.weighted_quantile(values, np.ones(3) / 3, [0.5])[0] == 2.0
    heavy = np.array([0.1, 0.8, 0.1])  # the 1.0 covers the bottom 80%
    low, mid = analog.weighted_quantile(values, heavy, [0.3, 0.5])
    assert low == 1.0
    assert 1.0 < mid < 2.0


# --- hindcast ------------------------------------------------------------------------


def test_crps_matches_the_definition():
    rng = np.random.default_rng(0)
    members = rng.normal(size=(7, 3))
    weights = rng.uniform(size=7)
    weights /= weights.sum()
    observed = np.array([0.1, -0.5, 2.0])
    pair_weights = weights[:, None] * weights[None]
    direct = [
        (weights * np.abs(members[:, t] - observed[t])).sum()
        - 0.5 * (pair_weights * np.abs(members[:, t, None] - members[None, :, t])).sum()
        for t in range(3)
    ]
    np.testing.assert_allclose(hindcast.crps(members, weights, observed), direct)
    # one member: plain absolute error
    assert hindcast.crps(np.array([[2.0]]), np.ones(1), np.array([5.0]))[0] == 3.0


def test_hindcast_scores_each_model_on_held_out_storms(tmp_path, river):
    hours, cfs, mm, rows = river
    write_parquet(event_jobs.hourly_path(tmp_path, "01234567"), hourly.to_table(hours, cfs, mm))
    write_parquet(
        event_jobs.events_path(tmp_path),
        event_jobs.event_table(
            "01234567", hourly.to_table(hours, cfs, mm)["ts"], events.extract_events(cfs, mm, 100.0)
        ),
    )

    stats = hindcast.run(
        data_dir=tmp_path, areas={"01234567": 100.0}, train_end=2019, test=(2020, 2021)
    )
    assert stats.gauges == 1 and stats.storms > 50
    scores = pq.read_table(hindcast.skill_path(tmp_path, 2020, 2021)).to_pylist()
    assert {r["start"].year for r in scores} == {2020, 2021}
    mean = {
        model: np.mean([r["crps"] for r in scores if r["model"] == model])
        for model in hindcast.MODELS
    }
    assert mean["analog"] < mean["no_matching"] < mean["persistence"]
