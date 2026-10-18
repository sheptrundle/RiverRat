from datetime import UTC, datetime, timedelta

import httpx2
import pyarrow.parquet as pq
import pytest
from sqlalchemy import func, select
from usgs_fake import FakeUSGS, reading, series_feature, site_feature

from riverrat.db.models import Gauge, Observation, Reach, ReachGauge
from riverrat.http import PoliteClient
from riverrat.jobs import usgs as jobs


def client_for(fake: FakeUSGS) -> PoliteClient:
    return PoliteClient("test", 0, transport=httpx2.MockTransport(fake.handler))


def add_gauge(session, number: str, source="USGS", metric="cfs") -> Gauge:
    gauge = Gauge(source=source, source_id=number, name=f"AW name {number}")
    reach = Reach(source="manual", river="R", section=number, states=["USA-WVR"])
    session.add_all([gauge, reach])
    session.flush()
    session.add(ReachGauge(reach_id=reach.id, gauge_id=gauge.id, is_primary=True, metric=metric))
    session.commit()
    return gauge


def recent(hours_ago: float) -> str:
    ts = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(hours=hours_ago)
    return ts.isoformat()


def test_sync_sites_fills_metadata_and_periods(session):
    add_gauge(session, "03192000")
    add_gauge(session, "99999999")  # not a real site
    add_gauge(session, "36510909", source="CALCULATED")  # not a USGS gauge: ignored
    fake = FakeUSGS(
        sites=[site_feature("03192000", lon=-81.18, lat=38.23)],
        series=[
            series_feature(
                "03192000", "00060", begin="1996-10-01T04:30:00", end="2003-09-30T00:00:00"
            ),
            series_feature(
                "03192000", "00060", begin="2007-10-01T00:00:00", end="2026-10-18T09:00:00"
            ),
            series_feature(
                "03192000", "00065", begin="2026-06-15T00:00:00", end="2026-10-18T09:00:00"
            ),
            series_feature("03192000", "00060", stat="00003", begin="1928-10-20T00:00:00"),
        ],
    )
    with client_for(fake) as client:
        assert jobs.sync_sites(client, session) == 2

    gauge = session.scalars(select(Gauge).where(Gauge.source_id == "03192000")).one()
    assert gauge.agency_name == "TEST RIVER AT SITE 03192000"
    assert gauge.drainage_area_sqmi == 1316.0
    assert session.scalar(select(func.ST_AsText(Gauge.geom)).where(Gauge.id == gauge.id)) == (
        "POINT(-81.18 38.23)"
    )
    assert (gauge.cfs_begin.year, gauge.cfs_end.year) == (1996, 2026)  # spans both series
    assert gauge.ft_begin.year == 2026
    assert gauge.metadata_synced_at is not None

    with client_for(fake) as client:
        assert jobs.sync_sites(client, session) == 0  # fresh metadata isn't refetched


def synced_gauge(session, number: str, lon=-81.18, lat=38.23, metric="cfs") -> Gauge:
    gauge = add_gauge(session, number, metric=metric)
    now = datetime.now(UTC)
    gauge.geom = f"SRID=4326;POINT({lon} {lat})"
    gauge.cfs_begin, gauge.cfs_end = datetime(1996, 10, 1, tzinfo=UTC), now
    gauge.ft_begin, gauge.ft_end = datetime(2025, 6, 1, tzinfo=UTC), now
    session.commit()
    return gauge


def test_live_stores_tracked_gauges_only(session):
    synced_gauge(session, "03192000", lon=-81.18, lat=38.23)
    synced_gauge(session, "02035000", lon=-78.09, lat=37.67)  # different 3-degree tile
    fake = FakeUSGS(
        readings=[
            reading("03192000", recent(1), "1290"),
            reading("03192000", recent(0.75), "1270"),
            reading("03192000", recent(0.75), "4.1", code="00065"),
            reading("02035000", recent(1), "3500"),
            reading("01234567", recent(1), "10"),  # untracked gauge in the same tile
            reading("03192000", recent(0.5), None),  # missing value
        ]
    )
    with client_for(fake) as client:
        stats = jobs.ingest_live(client, session, hours=3)

    assert stats.tiles == 2
    rows = session.execute(
        select(Gauge.source_id, Observation.param, Observation.value)
        .join(Gauge, Gauge.id == Observation.gauge_id)
        .order_by(Gauge.source_id, Observation.param, Observation.ts)
    ).all()
    assert [tuple(r) for r in rows] == [
        ("02035000", "cfs", 3500.0),
        ("03192000", "cfs", 1290.0),
        ("03192000", "cfs", 1270.0),
        ("03192000", "ft", 4.1),
    ]


def test_live_updates_revised_readings_and_prunes_old_ones(session):
    gauge = synced_gauge(session, "03192000")
    session.add(
        Observation(
            gauge_id=gauge.id,
            param="cfs",
            ts=datetime(2020, 1, 1, tzinfo=UTC),
            value=1.0,
            approved=True,
        )
    )
    session.commit()
    ts = recent(1)
    fake = FakeUSGS(readings=[reading("03192000", ts, "1290")])
    with client_for(fake) as client:
        jobs.ingest_live(client, session)
        fake.readings = [reading("03192000", ts, "1300", status="Approved")]
        stats = jobs.ingest_live(client, session)

    assert stats.pruned == 0  # the 2020 row was already pruned on the first run
    (row,) = session.scalars(select(Observation)).all()
    assert (row.value, row.approved) == (1300.0, True)


def test_live_skips_gauges_that_stopped_reporting(session):
    gauge = synced_gauge(session, "03189600")
    gauge.cfs_end = gauge.ft_end = datetime(2003, 10, 1, tzinfo=UTC)
    session.commit()
    fake = FakeUSGS()
    with client_for(fake) as client:
        assert jobs.ingest_live(client, session).tiles == 0
    assert fake.requests == []


def every_six_hours(start: datetime, end: datetime):
    rows, ts = [], start
    while ts < end:
        rows.append((ts, 100.0))
        ts += timedelta(hours=6)  # sparse, to keep tests fast
    return rows


@pytest.fixture
def history_gauge(session):
    gauge = synced_gauge(session, "03192000", metric="ft")
    gauge.cfs_begin = datetime(2000, 5, 1, tzinfo=UTC)
    gauge.cfs_end = datetime(2004, 3, 1, tzinfo=UTC)
    gauge.ft_begin = datetime(2003, 6, 1, tzinfo=UTC)
    gauge.ft_end = datetime(2004, 3, 1, tzinfo=UTC)
    session.commit()
    return gauge


def test_backfill_writes_one_file_per_year(session, tmp_path, history_gauge):
    fake = FakeUSGS(
        history={("03192000", "00060"): every_six_hours, ("03192000", "00065"): lambda s, e: []}
    )
    with client_for(fake) as client:
        stats = jobs.backfill(client, session, data_dir=tmp_path, start_year=2002)

    # cfs: 2002-2004 (history starts at start_year); ft: 2003-2004 (AW rates it in feet)
    assert stats.files_written == 5
    table = pq.read_table(jobs.history_path(tmp_path, "cfs", "03192000", 2003))
    assert table.num_rows == 365 * 4
    assert table.schema == jobs.HISTORY_SCHEMA
    assert table.column("ts")[0].as_py() == datetime(2003, 1, 1, tzinfo=UTC)
    empty = pq.read_table(jobs.history_path(tmp_path, "ft", "03192000", 2004))
    assert empty.num_rows == 0  # an empty file marks the year as done


def test_backfill_resumes_without_refetching_finished_years(session, tmp_path, history_gauge):
    fake = FakeUSGS(history={("03192000", "00060"): every_six_hours})
    with client_for(fake) as client:
        jobs.backfill(client, session, data_dir=tmp_path, start_year=2002)
        first = fake.single_site_history_requests()
        stats = jobs.backfill(client, session, data_dir=tmp_path, start_year=2002)
    assert first == 5
    assert stats.files_written == 0 and stats.files_skipped == 5


def test_backfill_holds_no_transaction_while_downloading(session, tmp_path, history_gauge):
    """An open transaction during a multi-hour download blocks migrations, and a waiting
    migration blocks every other job (this happened once)."""
    in_transaction = []

    def history(start, end):
        in_transaction.append(session.in_transaction())
        return []

    fake = FakeUSGS(history={("03192000", "00060"): history, ("03192000", "00065"): history})
    with client_for(fake) as client:
        jobs.backfill(client, session, data_dir=tmp_path, start_year=2002)
    assert in_transaction and not any(in_transaction)
