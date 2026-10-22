import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from river_fake import BASE, event_rows, synthetic_river
from sqlalchemy import func

from riverrat.api import reaches
from riverrat.api.main import app
from riverrat.db.models import (
    BasinRain,
    Gauge,
    Observation,
    RainForecast,
    RainForecastRun,
    Reach,
    ReachGauge,
)
from riverrat.features import hourly
from riverrat.forecast import live
from riverrat.forecast.rating import Rating
from riverrat.jobs import events as event_jobs
from riverrat.jobs import ratings as rating_jobs
from riverrat.jobs import usgs as usgs_jobs
from riverrat.storage import write_parquet

SITE = "01234567"
NOW = datetime(2026, 6, 1, 14, 20, tzinfo=UTC)  # 10:20 am EDT
LAST_HOUR = datetime(2026, 6, 1, 14, tzinfo=UTC)


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    """A data dir with the synthetic river's hourly table and storm library."""
    data_dir = tmp_path_factory.mktemp("data")
    hours, cfs, mm = synthetic_river()
    write_parquet(event_jobs.hourly_path(data_dir, SITE), hourly.to_table(hours, cfs, mm))
    rows = event_rows(hours, cfs, mm)
    table = pa.Table.from_pylist(rows, schema=event_jobs.EVENT_SCHEMA)
    write_parquet(event_jobs.events_path(data_dir), table)
    return data_dir


def add_reach(session, metric="cfs", low=150.0, high=400.0, readings=True) -> int:
    gauge = Gauge(
        source="USGS",
        source_id=SITE,
        name="Synthetic Creek",
        geom=func.ST_GeomFromText("POINT(-81.08 38.06)", 4326),
    )
    reach = Reach(river="Synthetic Creek", section="Gorge", states=["USA-WVR"], aw_id=99)
    session.add_all([gauge, reach])
    session.flush()
    session.add(
        ReachGauge(
            reach_id=reach.id,
            gauge_id=gauge.id,
            is_primary=True,
            metric=metric,
            range_min=low,
            range_max=high,
        )
    )
    if readings:
        for minutes in range(0, 180, 15):
            ts = NOW - timedelta(minutes=minutes)
            session.add(
                Observation(gauge_id=gauge.id, param="cfs", ts=ts, value=BASE, approved=False)
            )
            session.add(
                Observation(gauge_id=gauge.id, param="ft", ts=ts, value=1.5, approved=False)
            )
    # 10 mm yesterday morning
    session.add(
        BasinRain(gauge_id=gauge.id, source="stage4", ts=LAST_HOUR - timedelta(hours=28), mm=10.0)
    )
    session.commit()
    return reach.id


@pytest.fixture
def client(session, lake):
    app.dependency_overrides[reaches.get_session] = lambda: session
    app.dependency_overrides[reaches.get_data_dir] = lambda: lake
    app.dependency_overrides[reaches.get_now] = lambda: NOW
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_current_state_from_live_readings(session):
    add_reach(session)
    gauge_id = session.query(Gauge.id).scalar()
    found = live.current(session, gauge_id, NOW)
    assert found.state.at == LAST_HOUR + timedelta(hours=1)  # the hour in progress
    assert found.state.q == pytest.approx(BASE)
    assert found.ft == pytest.approx(1.5)
    assert found.state.api_mm == pytest.approx(10 * 0.9 ** (29 / 24), rel=1e-6)
    assert found.rain_hours == 1
    assert live.current(session, gauge_id, NOW + timedelta(hours=49)) is None


def test_whatif_forecasts_one_storm(client, session):
    reach_id = add_reach(session)
    small = client.get(f"/api/reaches/{reach_id}/whatif", params={"rain_in": 0.25})
    big = client.get(f"/api/reaches/{reach_id}/whatif", params={"rain_in": 2, "start_h": 6})
    assert small.status_code == big.status_code == 200
    small, big = small.json(), big.json()

    assert big["now"]["cfs"] == pytest.approx(BASE)
    assert big["now"]["band"] == "too_low"
    assert big["storm"] == {"rain_in": 2.0, "type": "typical", "start_h": 6, "duration_h": 18}
    assert len(big["forecast"]["p50"]) == 145
    assert big["forecast"]["p50"][6] == pytest.approx(BASE, rel=0.01)  # before the storm
    assert max(big["forecast"]["p90"]) > max(small["forecast"]["p90"])
    assert big["days"][0]["day"] == "2026-06-01"
    assert max(d["p_runnable"] for d in big["days"]) > max(d["p_runnable"] for d in small["days"])
    assert 0 < len(big["analogs"]) <= reaches.SHOWN_ANALOGS
    assert big["bands"]["edges"][0] == 150


def test_reach_list_and_observed_hours(client, session):
    reach_id = add_reach(session)
    listed = client.get("/api/reaches").json()
    assert [(r["id"], r["gauge_site"], r["range_min"]) for r in listed] == [(reach_id, SITE, 150)]

    body = client.get(f"/api/reaches/{reach_id}/whatif").json()
    observed = body["observed"]
    assert observed["values"][-1] == pytest.approx(BASE)
    assert observed["values"][0] is None  # readings only cover the last 3 hours
    assert len(observed["values"]) == 49  # 48 hours back through now.at


def test_no_rain_follows_the_recession(client, session):
    reach_id = add_reach(session)
    body = client.get(f"/api/reaches/{reach_id}/whatif", params={"rain_in": 0}).json()
    assert body["analogs"] == []
    assert all(d["p_runnable"] == 0 for d in body["days"])


def test_rain_response_marks_the_forecast_range(client, session):
    reach_id = add_reach(session)
    gauge_id = session.query(Gauge.id).scalar()
    issued = datetime(2026, 6, 1, 6, tzinfo=UTC)
    session.add(RainForecastRun(source="nbm", issued_at=issued, fetched_at=issued))
    for n in range(1, 17):  # 4 days of 6-hour periods: 2 mm median each
        end = issued + timedelta(hours=6 * n)
        session.add(
            RainForecast(
                gauge_id=gauge_id,
                source="nbm",
                issued_at=issued,
                valid_end=end,
                p10=0.0,
                p50=2.0,
                p90=6.0,
            )
        )
    session.commit()

    body = client.get(f"/api/reaches/{reach_id}/rain-response").json()
    assert body["rain_in"][0] == 0 and body["rain_in"][-1] == 4
    assert body["forecast_issued_at"].startswith("2026-06-01T06:00")
    today, tomorrow = body["days"][:2]
    assert len(today["p_runnable"]) == len(body["rain_in"])
    assert today["rain_needed_in"] is not None
    # from 14:20 to sunset (~00:50 UTC): ~10.5 h of 2 mm / 6 h
    assert today["forecast_rain_in"]["p50"] == pytest.approx(10.5 / 6 * 2 / 25.4, rel=0.05)
    assert tomorrow["forecast_rain_in"]["p50"] > today["forecast_rain_in"]["p50"]
    assert body["days"][-1]["forecast_rain_in"] is None  # past the forecast's end


def test_ft_reaches_use_the_stage_rating(client, session, lake):
    reach_id = add_reach(session, metric="ft", low=1.0, high=3.0)
    url = f"/api/reaches/{reach_id}/whatif"
    assert client.get(url).status_code == 409  # no rating yet

    rating = Rating(np.log([10.0, 1000.0]), np.array([0.0, 4.0]), 0.0)  # 50 cfs → 1.4 ft
    rating_jobs.ratings_path(lake).parent.mkdir(exist_ok=True)
    rating_jobs.ratings_path(lake).write_text(json.dumps({SITE: rating.to_dict()}))
    try:
        body = client.get(url).json()
    finally:
        rating_jobs.ratings_path(lake).unlink()
    assert body["bands"]["metric"] == "ft"
    assert body["now"]["value"] == pytest.approx(4 * np.log(5) / np.log(100), rel=1e-6)
    assert body["now"]["band"] == "low"
    assert body["now"]["ft_observed"] == pytest.approx(1.5)


def test_a_stand_in_gauge_on_the_same_river_is_preferred(client, session):
    reach_id = add_reach(session)  # "Synthetic Creek" on SITE, primary
    link = session.query(ReachGauge).one()
    link.is_primary = False
    other = Gauge(
        source="USGS",
        source_id="09999999",
        name="OTHER RIVER NEAR SOMEWHERE",
        geom=func.ST_GeomFromText("POINT(-81 38)", 4326),
    )
    calculated = Gauge(source="CALCULATED", source_id="1", name="Synthetic Calculated")
    session.add_all([other, calculated])
    session.flush()
    session.add_all(
        [
            ReachGauge(
                reach_id=reach_id,
                gauge_id=other.id,
                is_primary=False,
                metric="cfs",
                range_min=1,
                range_max=9,
            ),
            ReachGauge(
                reach_id=reach_id,
                gauge_id=calculated.id,
                is_primary=True,
                metric="cfs",
                range_min=1,
                range_max=9,
            ),
        ]
    )
    session.commit()
    gauge = client.get(f"/api/reaches/{reach_id}/whatif").json()["gauge"]
    assert (gauge["site"], gauge["primary"]) == (SITE, False)


def test_reaches_that_cant_be_forecast(client, session):
    assert client.get("/api/reaches/12345/whatif").status_code == 404
    reach_id = add_reach(session, readings=False)
    response = client.get(f"/api/reaches/{reach_id}/whatif")
    assert response.status_code == 409
    assert "no reading" in response.json()["detail"]


def test_ratings_job_fits_history_plus_live_readings(session, tmp_path):
    add_reach(session, metric="ft", low=1.0, high=3.0, readings=False)
    gauge_id = session.query(Gauge.id).scalar()
    now = datetime(2026, 6, 1, 14, tzinfo=UTC)
    rng = np.random.default_rng(0)

    def flows(n):
        return np.exp(rng.uniform(np.log(20), np.log(2000), n))

    # history through 40 days ago; since then the datum is 0.4 ft higher (live readings only)
    start = now - timedelta(days=200)
    ts = [start + timedelta(hours=h) for h in range(160 * 24)]
    cfs = flows(len(ts))
    for unit, values in (("cfs", cfs), ("ft", np.log(cfs) - 2)):
        table = pa.table(
            {
                "ts": pa.array(ts, pa.timestamp("us", tz="UTC")),
                "value": values,
                "approved": [True] * len(ts),
                "qualifier": pa.array([None] * len(ts), pa.string()),
            }
        )
        path = usgs_jobs.history_path(tmp_path, unit, SITE, 2026)
        path.parent.mkdir(parents=True)
        pq.write_table(table, path)
    recent = flows(9 * 24)
    for h, q in enumerate(recent):
        at = now - timedelta(hours=h)
        session.add(Observation(gauge_id=gauge_id, param="cfs", ts=at, value=q, approved=False))
        session.add(
            Observation(gauge_id=gauge_id, param="ft", ts=at, value=np.log(q) - 1.6, approved=False)
        )
    session.commit()

    stats = rating_jobs.fit_all(session, data_dir=tmp_path, now=now)
    assert (stats.gauges, stats.fitted) == (1, 1)
    saved = json.loads(rating_jobs.ratings_path(tmp_path).read_text())
    assert Rating.from_dict(saved[SITE]).to_ft(100.0) == pytest.approx(np.log(100) - 1.6, abs=0.05)
    assert saved[SITE]["fitted_at"] == now.isoformat()
