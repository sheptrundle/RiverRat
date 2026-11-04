from datetime import UTC, date, datetime, timedelta

import numpy as np
from test_whatif_api import NOW, client, lake  # noqa: F401

from riverrat.db.models import DamNotice, NoaaForecast, Observation, Reach
from riverrat.forecast import releases
from riverrat.jobs import releases as jobs
from riverrat.sources import tva


def test_parse_tva_payloads():
    observed = tva.parse_observed(
        [
            {"Day": "11/02/2026", "Time": "Noon EST", "AverageHourlyDischarge": "1,460"},
            {"Day": "07/04/2026", "Time": "3 PM EDT", "AverageHourlyDischarge": "10"},
            {"Day": "11/02/2026", "Time": "Midnight EST", "AverageHourlyDischarge": "0"},
            {"Day": "11/02/2026", "Time": "1 PM EST", "AverageHourlyDischarge": "n/a"},
        ]
    )
    assert observed == [
        (datetime(2026, 11, 2, 17, tzinfo=UTC), 1460.0),
        (datetime(2026, 7, 4, 19, tzinfo=UTC), 10.0),
        (datetime(2026, 11, 3, 5, tzinfo=UTC), 0.0),
    ]
    assert tva.parse_predicted([{"Day": "11/03/2026", "AverageOutflow": "1,215"}]) == [
        (date(2026, 11, 3), 1215.0)
    ]
    notice = tva.parse_notice(
        [{"EffectiveSince": "11/02/2026 03:15 PM EST", "Message": "Spilling."}]
    )
    assert notice == tva.Notice(datetime(2026, 11, 2, 20, 15, tzinfo=UTC), "Spilling.")
    assert tva.parse_notice([{"EffectiveSince": "02/05/2026 10:30 AM EST", "Message": ""}]) is None


def test_a_tva_reach_shows_its_dams_releases_and_notice(client, session):  # noqa: F811
    reach = Reach(river="Ocoee", section="Upper", states=["USA-TNN"], aw_id=1781)
    session.add(reach)
    dam = jobs.dam_gauge(session, releases.DAMS["ocoee3"])
    for h in range(3):
        at = NOW.replace(minute=0) - timedelta(hours=h)
        session.add(
            Observation(gauge_id=dam.id, param="cfs", ts=at, value=1000.0 + h, approved=False)
        )
    issued = NOW.replace(hour=0, minute=0)
    for day, cfs in [(date(2026, 6, 1), 495.0), (date(2026, 6, 2), 1200.0)]:
        start, end = tva.day_span(day)
        for at in (start, end - timedelta(hours=1)):
            session.add(
                NoaaForecast(
                    gauge_id=dam.id, source="tva", issued_at=issued, valid_time=at, flow_cfs=cfs
                )
            )
    session.add(
        DamNotice(gauge_id=dam.id, since=NOW, message="Ocoee 3 will be spilling.", fetched_at=NOW)
    )
    session.commit()

    # Monday June 1: the Upper Ocoee's first release this week is Saturday, 9 am – 3 pm
    whatif = client.get(f"/api/reaches/{reach.id}/whatif").json()
    assert whatif["release"]["basis"] == "schedule" and whatif["now"]["value"] == 0.0
    p = {d["day"]: d["p_runnable"] for d in whatif["days"]}
    assert p["2026-06-01"] == 0.0 and p["2026-06-05"] == 0.0
    assert p["2026-06-06"] == releases.SCHEDULE_KEPT
    body = client.get(f"/api/reaches/{reach.id}/releases").json()
    assert body["reach"]["aw_id"] == 1781
    (shown,) = body["dams"]
    assert (shown["name"], shown["agency"], shown["now_cfs"]) == ("Ocoee No. 3 Dam", "TVA", 1000.0)
    assert [d["cfs"] for d in shown["forecast"]] == [495.0, 1200.0]
    assert shown["notice"]["message"] == "Ocoee 3 will be spilling."


def test_a_reach_without_dams_has_none(client, session):  # noqa: F811
    reach = Reach(river="Free Creek", section="All", states=["USA-WVR"])
    session.add(reach)
    session.commit()
    assert client.get(f"/api/reaches/{reach.id}/releases").json()["dams"] == []


def test_the_ocoee_calendars():
    middle = releases.TVA_REACHES[1780]
    assert len(releases.OCOEE2_2026) == 117 and len(releases.OCOEE3_2026) == 34
    # Saturday August 15 is a ten-hour day, 9 am – 7 pm EDT
    flow = releases.scheduled(middle, datetime(2026, 8, 15, 12, tzinfo=UTC), 12)
    assert list(flow) == [0.0] + [1250.0] * 10 + [0.0]
    assert not releases.scheduled(middle, datetime(2026, 11, 2, tzinfo=UTC), 24 * 7).any()


def test_generating_spreads_the_days_hours_across_it():
    start = datetime(2026, 11, 3, 5, tzinfo=UTC)  # Eastern midnight
    daily = np.array([1450.0 / 2] * 24 + [0.0] * 24 + [3000.0] * 24)
    paths = releases.generating(daily, start, unit_cfs=1450, base_cfs=0)
    assert paths.shape == (releases.PLACEMENTS, 72)
    # half the day on: 12 hours of one unit in every member, first thing or last
    assert all((path[:24] == 1450).sum() == 12 for path in paths)
    assert paths[0, 0] == 1450 and paths[-1, 23] == 1450 and paths[0, 23] == 0
    assert not paths[:, 24:48].any()  # nothing planned
    assert (paths[:, 48:] == 3000).all()  # more than a unit: all day


def test_a_tva_powerhouse_reach_is_forecast_from_the_daily_plan(client, session):  # noqa: F811
    reach = Reach(river="Holston", section="Below the dam", states=["USA-TNN"], aw_id=1753)
    session.add(reach)
    dam = jobs.dam_gauge(session, releases.DAMS["south_holston"])
    for h in range(8):
        at = NOW.replace(minute=0) - timedelta(hours=h)
        session.add(
            Observation(
                gauge_id=dam.id, param="cfs", ts=at, value=1480.0 if h < 4 else 10.0, approved=False
            )
        )
    issued = NOW.replace(hour=0, minute=0)
    for day, cfs in [
        (date(2026, 6, 1), 1480.0),
        (date(2026, 6, 2), 10.0),
        (date(2026, 6, 3), 200.0),
    ]:
        start, end = tva.day_span(day)
        for at in (start, end - timedelta(hours=1)):
            session.add(
                NoaaForecast(
                    gauge_id=dam.id, source="tva", issued_at=issued, valid_time=at, flow_cfs=cfs
                )
            )
    session.commit()

    body = client.get(f"/api/reaches/{reach.id}/whatif").json()
    assert body["release"]["basis"] == "tva" and body["bands"]["minimum"] == 1000
    p = {d["day"]: d["p_runnable"] for d in body["days"]}
    assert p["2026-06-01"] == 1.0  # generating all day
    assert p["2026-06-02"] == 0.0  # not at all
    assert 0.5 < p["2026-06-03"] < 1.0  # about 4 hours, sometimes all at night
