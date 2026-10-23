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


RAIN_PERCENTILES = {
    "p05": 0.0,
    "p10": 0.0,
    "p25": 1.0,
    "p50": 2.0,
    "p75": 4.0,
    "p90": 6.0,
    "p95": 8.0,
}


def add_rain_forecast(session, gauge_id, issued, periods, scale=1.0):
    """An NBM run of `periods` 6-hour totals (mm), each RAIN_PERCENTILES × `scale`."""
    session.add(RainForecastRun(source="nbm", issued_at=issued, fetched_at=issued))
    for n in range(1, periods + 1):
        session.add(
            RainForecast(
                gauge_id=gauge_id,
                source="nbm",
                issued_at=issued,
                valid_end=issued + timedelta(hours=6 * n),
                **{k: v * scale for k, v in RAIN_PERCENTILES.items()},
            )
        )


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
    add_rain_forecast(session, gauge_id, issued, periods=16)  # 4 days: 2 mm median each
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


def test_a_range_on_an_nws_location_uses_its_usgs_twin(client, session):
    reach_id = add_reach(session)
    link = session.query(ReachGauge).one()
    twin = session.get(Gauge, link.gauge_id)
    nws = Gauge(source="NOAA", source_id="synw2", name="Synthetic Creek NWS", same_as_id=twin.id)
    session.add(nws)
    session.flush()
    link.gauge_id = nws.id  # AW's range is on the NWS location
    session.commit()
    body = client.get(f"/api/reaches/{reach_id}/whatif").json()
    assert (body["gauge"]["site"], body["gauge"]["primary"]) == (SITE, True)


def test_reaches_that_cant_be_forecast(client, session):
    assert client.get("/api/reaches/12345/whatif").status_code == 404
    reach_id = add_reach(session, readings=False)
    response = client.get(f"/api/reaches/{reach_id}/whatif")
    assert response.status_code == 409
    assert "no reading" in response.json()["detail"]

    gauge = session.query(Gauge).one()
    session.add(Observation(gauge_id=gauge.id, param="ft", ts=NOW, value=1.0, approved=False))
    session.commit()
    detail = client.get(f"/api/reaches/{reach_id}/whatif").json()["detail"]
    assert "stage only" in detail

    gauge.cfs_end = NOW - timedelta(days=400)
    session.commit()
    detail = client.get(f"/api/reaches/{reach_id}/whatif").json()["detail"]
    assert detail == f"gauge {SITE} stopped reporting flow in 2025 (USGS discontinued it)"


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


# --- the Outlook ---------------------------------------------------------------------


def test_outlook_job_and_endpoint(client, session, lake):
    from riverrat.jobs import outlook as outlook_jobs

    reach_id = add_reach(session)
    unforecastable = Reach(river="Dry Fork", section="Nowhere", states=["USA-WVR"])
    session.add(unforecastable)
    gauge_id = session.query(Gauge.id).scalar()
    add_rain_forecast(session, gauge_id, NOW - timedelta(hours=2), periods=24, scale=5.0)
    session.commit()

    assert client.get("/api/outlook").json()["computed_at"] is None
    stats = outlook_jobs.compute(session, data_dir=lake, now=NOW)
    assert (stats.reaches, stats.gauges) == (1, 1)
    assert stats.skipped == {"the reach has no USGS gauge with a runnable range": 1}
    assert outlook_jobs._reason("gauge 0123 stopped reporting flow in 2009 (x)") == (
        "stopped reporting flow in YYYY (x)"
    )

    body = client.get("/api/outlook").json()
    assert body["rain_issued_at"] == "2026-06-01T12:20:00Z"
    assert body["days"][0] == "2026-06-01" and len(body["days"]) == 6
    (reach,) = body["reaches"]
    assert (reach["id"], reach["gauge_site"], reach["now_band"]) == (reach_id, SITE, "too_low")
    days = reach["days"]
    # 10 mm median every 6 hours: by tomorrow the river is up past the range's minimum
    assert days[0]["rain_in"]["p50"] > 0
    assert days[1]["peak"]["p50"] > 150
    assert days[1]["peak_bands"]["too_low"] < 0.5
    assert days[2]["rain_in"]["p50"] > days[1]["rain_in"]["p50"]

    # the region-wide what-if: a storm the same evening (22:00 UTC), 0–3 in
    assert body["whatif_storm_start"] == "2026-06-01T22:00:00Z"
    assert body["whatif_rain_in"][0] == 0 and body["whatif_rain_in"][-1] == 3
    tomorrow = reach["whatif"][1]["p_runnable"]
    assert len(tomorrow) == len(body["whatif_rain_in"])
    assert tomorrow[0] == 0 and max(tomorrow) > 0.5

    # a rerun replaces the rows rather than adding to them
    outlook_jobs.compute(session, data_dir=lake, now=NOW)
    assert len(client.get("/api/outlook").json()["reaches"]) == 1


def test_range_history_and_ranges_that_look_wrong(client, session):
    reach_id = add_reach(session)  # 150–400 cfs on a river that idles at 50
    history = client.get(f"/api/reaches/{reach_id}/whatif").json()["history"]
    assert history["days"] >= 365
    assert 0.01 < history["share_in_range"] < 0.5
    assert history["note"] is None

    link = session.query(ReachGauge).one()
    link.range_min, link.range_max = 1.0, 10.0  # the river never drops that low
    session.commit()
    history = client.get(f"/api/reaches/{reach_id}/whatif").json()["history"]
    assert (history["share_in_range"], history["note"]) == (0.0, "below_record")
