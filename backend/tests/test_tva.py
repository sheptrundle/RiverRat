from datetime import UTC, date, datetime, timedelta

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

    assert "TVA's Ocoee No. 3 Dam" in client.get(f"/api/reaches/{reach.id}/whatif").json()["detail"]
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
