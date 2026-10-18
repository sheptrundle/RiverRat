from datetime import timedelta

import httpx2
import pytest
from aw_pages import correlation, index_page, reach_page, stub
from sqlalchemy import func, select

from riverrat.db.models import Gauge, Reach, ReachGauge
from riverrat.http import PoliteClient
from riverrat.jobs.sync_reaches import sync
from riverrat.sources import aw

WV, VA, CO = "USA-WVR", "USA-VRG", "USA-COL"
ALLOW_ALL = "User-agent: *\nDisallow: /wp-admin/\n"


class FakeAW:
    """Serves synthetic pages at AW's URLs and records what was requested."""

    def __init__(self, index: list[dict], reaches: dict[int, str], robots: str = ALLOW_ALL):
        self.index = index
        self.reaches = reaches
        self.robots = robots
        self.requests: list[str] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        self.requests.append(url)
        if url == f"{aw.BASE_URL}/robots.txt":
            return httpx2.Response(200, text=self.robots)
        if url == aw.INDEX_URL:
            return httpx2.Response(200, text=index_page(self.index))
        for aw_id, html in self.reaches.items():
            if url == aw.reach_url(aw_id):
                return httpx2.Response(200, text=html)
        return httpx2.Response(404)

    def detail_requests(self) -> int:
        return sum("river-detail" in url for url in self.requests)


def run(fake: FakeAW, session, **kwargs):
    client = PoliteClient("test", 0, transport=httpx2.MockTransport(fake.handler))
    options = {"user_agent": "test", "state_codes": {WV, VA}, "max_age": timedelta(days=7)}
    with client:
        return sync(client, session, **(options | kwargs))


def reach(session, aw_id) -> Reach:
    return session.scalars(select(Reach).where(Reach.aw_id == aw_id)).one()


def links(session, aw_id) -> list[tuple]:
    rows = session.execute(
        select(
            Gauge.source,
            Gauge.source_id,
            ReachGauge.is_primary,
            ReachGauge.metric,
            ReachGauge.range_min,
            ReachGauge.range_max,
        )
        .join(ReachGauge.gauge)
        .join(ReachGauge.reach)
        .where(Reach.aw_id == aw_id)
        .order_by(Gauge.source_id)
    )
    return [tuple(row) for row in rows]


def test_index_only_stores_pilot_reaches(session):
    fake = FakeAW(
        index=[
            stub(1, river="Gauley", difficulty="IIItoIVstandoutV", states=[WV]),
            stub(2, river="James", difficulty="ItoII", states=[VA, "USA-NCR"]),
            stub(3, river="Arkansas", states=[CO]),
        ],
        reaches={},
    )
    stats = run(fake, session, index_only=True)

    assert stats.listed == 2
    assert fake.detail_requests() == 0
    gauley = reach(session, 1)
    assert (gauley.class_low, gauley.class_high, gauley.class_standout) == ("III", "IV", "V")
    assert gauley.listed_at is not None and gauley.scraped_at is None
    assert session.scalar(select(func.count()).select_from(Reach)) == 2


def test_full_sync_stores_details_and_gauges(session):
    fake = FakeAW(
        index=[stub(1, states=[WV])],
        reaches={
            1: reach_page(
                1,
                next_release="2027-09-16",
                correlations=[
                    correlation(source="CALCULATED", gauge_id="36510909", low=400, high=5000),
                    correlation(
                        gauge_id="03189600", primary=False, metric="levelFT", low=2, high=None
                    ),
                ],
            )
        },
    )
    stats = run(fake, session)

    assert (stats.listed, stats.scraped, stats.failed) == (1, 1, 0)
    row = reach(session, 1)
    assert row.length_mi == 12 and row.gradient_fpm == 36
    assert str(row.next_release) == "2027-09-16"
    assert session.scalar(select(func.ST_GeometryType(Reach.geom))) == "ST_LineString"
    assert session.scalar(select(func.ST_SRID(Reach.geom))) == 4326
    assert links(session, 1) == [
        ("USGS", "03189600", False, "ft", 2.0, None),
        ("CALCULATED", "36510909", True, "cfs", 400.0, 5000.0),
    ]


def test_fresh_reaches_are_not_refetched(session):
    fake = FakeAW(index=[stub(1), stub(2)], reaches={1: reach_page(1), 2: reach_page(2)})
    run(fake, session)
    run(fake, session)
    assert fake.detail_requests() == 2

    run(fake, session, max_age=timedelta(0))
    assert fake.detail_requests() == 4


def test_limit_caps_detail_fetches(session):
    fake = FakeAW(index=[stub(i) for i in (1, 2, 3)], reaches={i: reach_page(i) for i in (1, 2, 3)})
    assert run(fake, session, limit=2).scraped == 2
    assert run(fake, session, limit=2).scraped == 1  # only the one never fetched is stale


def test_resync_replaces_gauge_links(session):
    fake = FakeAW(
        index=[stub(1)],
        reaches={
            1: reach_page(1, correlations=[correlation(gauge_id="A"), correlation(gauge_id="B")])
        },
    )
    run(fake, session)
    fake.reaches[1] = reach_page(1, correlations=[correlation(gauge_id="B", low=900)])
    run(fake, session, max_age=timedelta(0))

    assert links(session, 1) == [("USGS", "B", True, "cfs", 900.0, 5000.0)]
    assert session.scalar(select(func.count()).select_from(Gauge)) == 2  # gauges are kept


def test_duplicate_gauge_links_are_collapsed(session):
    fake = FakeAW(
        index=[stub(1)],
        reaches={1: reach_page(1, correlations=[correlation(low=100), correlation(low=200)])},
    )
    run(fake, session)
    assert [link[4] for link in links(session, 1)] == [100.0]


def test_manual_reaches_are_never_overwritten(session):
    session.add(Reach(aw_id=1, source="manual", river="My name", section="My section", states=[WV]))
    session.commit()
    fake = FakeAW(index=[stub(1)], reaches={1: reach_page(1)})
    run(fake, session, aw_ids=[1])
    run(fake, session)

    row = reach(session, 1)
    assert (row.river, row.section, row.scraped_at) == ("My name", "My section", None)
    assert links(session, 1) == []


def test_one_bad_page_does_not_stop_the_sync(session):
    fake = FakeAW(
        index=[stub(1), stub(2), stub(3)],
        reaches={1: reach_page(1), 2: "<html>broken</html>", 3: reach_page(3)},
    )
    stats = run(fake, session)
    assert (stats.scraped, stats.failed) == (2, 1)
    assert reach(session, 2).scraped_at is None  # retried on the next run


def test_explicit_reach_ids_skip_the_index(session):
    fake = FakeAW(index=[], reaches={42: reach_page(42, states=[CO])})
    stats = run(fake, session, aw_ids=[42])
    assert stats.scraped == 1
    assert aw.INDEX_URL not in fake.requests
    assert reach(session, 42).states == [CO]


def test_stops_if_robots_txt_disallows(session):
    fake = FakeAW(index=[stub(1)], reaches={}, robots="User-agent: *\nDisallow: /content/\n")
    with pytest.raises(PermissionError):
        run(fake, session)
    assert fake.requests == [f"{aw.BASE_URL}/robots.txt"]
