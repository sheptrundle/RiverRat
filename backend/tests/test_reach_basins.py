import httpx2
import pytest
from sqlalchemy import func, select
from test_basins import FakeNLDI, square, square_area_sqmi

from riverrat.db.models import Gauge, Reach, ReachBasin, ReachGauge
from riverrat.http import PoliteClient
from riverrat.jobs import reach_basins as jobs


def add_reach(session, line, **kwargs):
    reach = Reach(river="Creek", section="Up to down", states=["USA-NCR"], **kwargs)
    reach.geom = func.ST_GeomFromText(line, 4326)
    session.add(reach)
    session.commit()
    return reach


def run(fake, session, **kwargs):
    with PoliteClient("test", 0, transport=httpx2.MockTransport(fake.handler)) as client:
        return jobs.sync(client, session, **kwargs)


def test_the_end_with_the_larger_watershed_is_the_take_out(session):
    # Drawn downstream-to-upstream: the first end is the take-out.
    reach = add_reach(session, "LINESTRING(-82 35, -81 35)")
    gauged = add_reach(session, "LINESTRING(-82 36, -81 36)")
    gauge = Gauge(source="USGS", source_id="1", name="g")
    session.add(gauge)
    session.flush()
    session.add(ReachGauge(reach_id=gauged.id, gauge_id=gauge.id, is_primary=True))
    session.commit()
    # Points 5% in from each end, as the job snaps them.
    fake = FakeNLDI(
        {},
        comids={"-81.95 35.0": 2, "-81.05 35.0": 1},
        comid_basins={1: square(-81, 35, 0.1), 2: square(-82, 35, 0.3)},
    )

    stats = run(fake, session)
    assert (stats.reaches, stats.fetched) == (1, 1)  # the gauged reach is left alone
    found = session.scalar(select(ReachBasin))
    assert (found.reach_id, found.comid) == (reach.id, 2)
    assert found.area_sqmi == pytest.approx(square_area_sqmi(35, 0.3), rel=0.01)
    assert found.putin_area_sqmi == pytest.approx(square_area_sqmi(35, 0.1), rel=0.01)

    assert run(fake, session).reaches == 0  # done once
    assert run(fake, session, refresh=True).fetched == 1


def test_one_end_off_the_network_still_gives_a_watershed(session):
    add_reach(session, "LINESTRING(-82 35, -81 35)")
    fake = FakeNLDI({}, comids={"-81.95 35.0": 2}, comid_basins={2: square(-82, 35, 0.3)})
    assert run(fake, session).fetched == 1
    assert session.scalar(select(ReachBasin.putin_area_sqmi)) is None

    session.query(ReachBasin).delete()
    session.commit()
    assert run(FakeNLDI({}), session).missing == 1
