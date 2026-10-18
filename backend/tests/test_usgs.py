from datetime import UTC, datetime, timedelta

import httpx2
from usgs_fake import FakeUSGS, csv_body, reading, series_feature, site_feature

from riverrat.http import PoliteClient
from riverrat.sources import usgs


def client_for(fake: FakeUSGS) -> PoliteClient:
    return PoliteClient("test", 0, transport=httpx2.MockTransport(fake.handler))


def test_parse_site():
    site = usgs.parse_site(site_feature("03192000", lon=-81.2, lat=38.2, area=1316.0))
    assert site == usgs.Site(
        number="03192000",
        name="TEST RIVER AT SITE 03192000",
        lon=-81.2,
        lat=38.2,
        drainage_area_sqmi=1316.0,
        state_code="54",
        huc="050500050809",
    )


def test_parse_series_keeps_only_15_minute_flow_and_stage():
    flow = usgs.parse_series(series_feature("1", "00060"))
    assert (flow.unit, flow.begin) == ("cfs", datetime(1996, 10, 1, 4, 30, 0, 1, tzinfo=UTC))
    assert usgs.parse_series(series_feature("1", "00065")).unit == "ft"
    assert usgs.parse_series(series_feature("1", "00060", stat="00003")) is None  # daily mean
    assert usgs.parse_series(series_feature("1", "00095")) is None  # conductance


def test_parse_observation():
    obs = usgs.parse_observation(
        reading("03192000", "2026-10-17T00:15:00+00:00", "1270", status="Approved")["properties"]
    )
    assert obs == usgs.Observation(
        number="03192000",
        unit="cfs",
        ts=datetime(2026, 10, 17, 0, 15, tzinfo=UTC),
        value=1270.0,
        approved=True,
        qualifier=None,
    )


def test_parse_observation_skips_missing_values():
    assert (
        usgs.parse_observation(reading("1", "2026-10-17T00:15:00+00:00", None)["properties"])
        is None
    )
    assert (
        usgs.parse_observation(reading("1", "2026-10-17T00:15:00+00:00", "")["properties"]) is None
    )


def test_parse_csv_skips_blank_values():
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    rows = usgs.parse_csv(csv_body([(t0, 4120), (t0 + timedelta(minutes=15), "")]))
    assert rows == [(t0, 4120.0, True, None)]


def test_features_follows_next_links():
    fake = FakeUSGS(sites=[site_feature(str(n)) for n in range(5)], page_size=2)
    url = usgs.items_url(
        "monitoring-locations", id=",".join(usgs.site_id(str(n)) for n in range(5))
    )
    with client_for(fake) as client:
        numbers = [
            f["properties"]["monitoring_location_number"] for f in usgs.features(client, url)
        ]
    assert numbers == ["0", "1", "2", "3", "4"]
    assert len(fake.requests) == 3


def every_15_minutes(start: datetime, end: datetime):
    rows, ts = [], start
    while ts <= end:  # inclusive end, like the real API; history() must trim it
        rows.append((ts, 100.0))
        ts += timedelta(minutes=15)
    return rows


def test_history_splits_windows_that_fill_a_page():
    fake = FakeUSGS(history={("1", "00060"): every_15_minutes})
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=2)  # 192 readings
    with client_for(fake) as client:
        rows = usgs.history(client, "1", "cfs", start, end, page_size=100)
    assert len(rows) == 192
    assert rows[0][0] == start and rows[-1][0] == end - timedelta(minutes=15)
    assert len({ts for ts, *_ in rows}) == 192  # no duplicates at split boundaries
    assert fake.single_site_history_requests() > 1
