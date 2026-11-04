from datetime import timedelta

import numpy as np
import pytest
from test_whatif_api import NOW, client, lake  # noqa: F401

from riverrat.db.models import Gauge, NoaaForecast, Observation, Reach, ReachGauge
from riverrat.forecast import releases
from riverrat.jobs import releases as jobs

SUMMERSVILLE = releases.DAMS["summersville"]


def add_dam(session, readings=1500.0, plan=(1500.0, 1500.0, 2800.0, 2800.0)):
    """Summersville's gauge, its last 6 hours of release, and a plan issued this morning:
    `plan` every 12 hours from noon."""
    dam = jobs.dam_gauge(session, SUMMERSVILLE)
    for h in range(6):
        at = NOW - timedelta(hours=h)
        session.add(
            Observation(gauge_id=dam.id, param="cfs", ts=at, value=readings, approved=False)
        )
    issued = NOW.replace(hour=0, minute=0)
    for n, value in enumerate(plan):
        valid = NOW.replace(hour=12, minute=0) + timedelta(hours=12 * n)
        session.add(
            NoaaForecast(
                gauge_id=dam.id,
                source=releases.FORECAST_SOURCE,
                issued_at=issued,
                valid_time=valid,
                flow_cfs=value,
            )
        )
    session.commit()
    return dam


def test_planned_release_starts_from_the_latest_reading(session):
    dam = add_dam(session, readings=1000.0, plan=(1500.0, 1500.0, 2800.0))
    start = NOW.replace(minute=0)  # 14:00 UTC; the plan's first point was noon
    cfs = releases.planned(session, dam.id, start, 48)
    assert cfs[0] == pytest.approx(1000.0, abs=60)  # the 14:00 reading
    assert cfs[10] == pytest.approx(1500.0)  # the plan at midnight
    assert cfs[22] == pytest.approx(2800.0)  # stepping up by noon tomorrow
    assert cfs[-1] == pytest.approx(2800.0)  # held after the plan ends
    # Nothing issued yet by an earlier start
    assert releases.planned(session, dam.id, NOW - timedelta(days=1), 48) is None


def test_upper_gauley_is_forecast_from_the_release_plan(client, session, lake):  # noqa: F811
    reach = Reach(river="Gauley", section="Summersville Dam to Woods Ferry", states=["USA-WVR"])
    calculated = Gauge(
        source="CALCULATED", source_id="36510909", name="Gauley at Summersville (cfs)"
    )
    session.add_all([reach, calculated])
    session.flush()
    session.add(
        ReachGauge(
            reach_id=reach.id,
            gauge_id=calculated.id,
            is_primary=True,
            metric="cfs",
            range_min=2000,
            range_max=5000,
        )
    )
    add_dam(session)
    session.commit()

    detail = client.get(f"/api/reaches/{reach.id}/whatif").json()["detail"]
    assert detail == "the dam release readings haven't come in"

    stats = jobs._store_composite(
        session, calculated.id, [jobs.dam_gauge(session, SUMMERSVILLE).id], NOW
    )
    session.commit()
    assert stats >= 5
    body = client.get(f"/api/reaches/{reach.id}/whatif?rain_in=2").json()
    assert body["release"] == {"dams": ["Summersville Dam"], "natural": None, "basis": "plan"}
    assert body["now"]["value"] == pytest.approx(1500.0)
    assert body["analogs"] == []
    median = np.array(body["forecast"]["p50"])
    assert median[0] == pytest.approx(1500.0, abs=1) and median[-1] == pytest.approx(2800.0)
    # 1,500 today (too low for a 2,000 minimum); 2,800 once the release steps up
    p = [d["p_runnable"] for d in body["days"]]
    assert p[0] == 0.0 and p[-1] == 1.0
    assert client.get(f"/api/reaches/{reach.id}/rain-response").status_code == 409

    from riverrat.jobs import outlook as outlook_jobs

    outlook_jobs.compute(session, data_dir=lake, now=NOW)
    row = next(r for r in client.get("/api/outlook").json()["reaches"] if r["id"] == reach.id)
    assert row["release"]["dams"] == ["Summersville Dam"] and row["whatif"] is None
    assert row["gauge_site"] == "36510909"


def test_a_reach_below_an_unreadable_dam_isnt_estimated(client, session):  # noqa: F811
    reach = Reach(river="Ocoee", section="Upper", states=["USA-TNN"], aw_id=1781)
    session.add(reach)
    session.commit()
    detail = client.get(f"/api/reaches/{reach.id}/whatif").json()["detail"]
    assert "Ocoee No. 3 Dam" in detail
