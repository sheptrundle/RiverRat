from datetime import date, timedelta

import pytest
from test_whatif_api import NOW, add_rain_forecast, add_reach, client, lake  # noqa: F401

from riverrat.db.models import ForecastArchive, Gauge, Observation
from riverrat.jobs import outlook as outlook_jobs
from riverrat.jobs import track_record as jobs


def forecast(session, lake):  # noqa: F811
    reach_id = add_reach(session)  # 150–400 cfs; the creek sits at 50 (too low)
    gauge_id = session.query(Gauge.id).scalar()
    add_rain_forecast(session, gauge_id, NOW - timedelta(hours=2), periods=24, scale=5.0)
    session.commit()
    outlook_jobs.compute(session, data_dir=lake, now=NOW)
    return reach_id, gauge_id


def test_the_morning_outlook_is_kept_once(session, lake):  # noqa: F811
    reach_id, _ = forecast(session, lake)
    assert jobs.archive(session, NOW.replace(hour=9)) == 0  # before the morning's run
    kept = jobs.archive(session, NOW)
    assert kept >= 5
    rows = session.query(ForecastArchive).order_by(ForecastArchive.day).all()
    assert [r.lead_days for r in rows] == list(range(len(rows)))
    assert rows[0].day == NOW.date() and rows[0].reach_id == reach_id
    assert not rows[0].persistence  # too low that morning
    assert (rows[0].range_min, rows[0].range_max, rows[0].replay) == (150, 400, False)
    assert jobs.archive(session, NOW + timedelta(hours=1)) == 0  # one a morning


def test_a_day_is_scored_once_it_is_over(session, lake):  # noqa: F811
    _, gauge_id = forecast(session, lake)
    jobs.archive(session, NOW)
    # It came up into range in the afternoon (EDT), and readings stop that evening.
    day = NOW.replace(hour=12, minute=0)
    for quarter in range(4 * 12):
        at = day + timedelta(minutes=15 * quarter)
        value = 50.0 if at.hour < 18 else 220.0
        session.add(Observation(gauge_id=gauge_id, param="cfs", ts=at, value=value, approved=True))
    session.commit()

    assert jobs.score(session, NOW).scored == 0  # the day isn't over
    stats = jobs.score(session, NOW + timedelta(days=1))
    assert stats.scored == 1  # day 0; tomorrow has no readings yet
    first = session.query(ForecastArchive).filter_by(lead_days=0).one()
    assert first.observed is True and first.scored_at is not None

    later = jobs.score(session, NOW + timedelta(days=10))
    assert later.scored == 0 and later.unscorable >= 4  # no readings: given up on


def test_track_record_endpoint(client, session, lake):  # noqa: F811
    _, gauge_id = forecast(session, lake)
    jobs.archive(session, NOW)
    for kept in session.query(ForecastArchive):
        kept.observed = kept.lead_days % 2 == 0
        kept.scored_at = NOW
    session.commit()

    body = client.get("/api/track-record").json()
    assert body["scored"] == session.query(ForecastArchive).count()
    assert body["first_day"] == NOW.date().isoformat() and body["replayed"] == 0
    lead0 = body["leads"][0]
    assert lead0["lead_days"] == 0 and lead0["n"] == 1
    assert lead0["brier_persistence"] == 1.0  # said too low; it ran
    assert sum(b["n"] for b in body["reliability"]) == body["scored"]
    assert {c["day"] for c in body["recent"]} <= {(NOW.date() + timedelta(days=1)).isoformat()}
    assert client.get("/api/track-record?state=USA-NCR").json()["scored"] == 0
    assert client.get("/api/track-record?reaches=999").json()["scored"] == 0


def test_replay_uses_only_what_was_known(session, lake):  # noqa: F811
    forecast(session, lake)
    morning = date(2026, 6, 2)  # after the readings and the rain forecast
    kept = jobs.replay(session, data_dir=lake, start=morning, end=morning)
    assert kept >= 5
    rows = session.query(ForecastArchive).all()
    assert all(r.replay for r in rows)
    assert {r.issued_at.hour for r in rows} == {jobs.ARCHIVE_HOUR_UTC}
    # Replaying again replaces, rather than adds to, the same morning.
    assert jobs.replay(session, data_dir=lake, start=morning, end=morning) == kept
    assert session.query(ForecastArchive).count() == kept
    # Before the rain forecast was issued there's nothing to replay from.
    assert jobs.replay(session, data_dir=lake, start=date(2026, 5, 31), end=date(2026, 5, 31)) == 0


@pytest.fixture(autouse=True)
def _client_fixture_used(client):  # noqa: F811
    """test_whatif_api's client fixture also points the API at `lake`."""
