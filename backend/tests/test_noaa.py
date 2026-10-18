from datetime import UTC, datetime

import httpx2
from sqlalchemy import select

from riverrat.db.models import Gauge, NoaaForecast
from riverrat.http import PoliteClient
from riverrat.jobs import noaa as jobs
from riverrat.sources import nldi, nwps


def nwps_gauge(lid="BVAW2", usgs="03192000", reach="4552334", forecast="HGIFE"):
    return {
        "lid": lid,
        "usgsId": usgs,
        "reachId": reach,
        "name": "Test River",
        "pedts": {"observed": "HGIRG", "forecast": forecast},
    }


def rfc_forecast(issued="2026-10-18T14:48:00Z", unit="kcfs"):
    return {
        "pedts": "HGIFF",
        "issuedTime": issued,
        "primaryName": "Stage",
        "primaryUnits": "ft",
        "secondaryName": "Flow",
        "secondaryUnits": unit,
        "data": [
            {"validTime": "2026-10-18T18:00:00Z", "primary": 4.1, "secondary": 2.72},
            {"validTime": "2026-10-19T00:00:00Z", "primary": 4.0, "secondary": -999},
        ],
    }


def nwm_short(reference="2026-10-18T18:00:00Z", flows=(2801.9, 2795.0)):
    data = [{"validTime": f"2026-10-18T{19 + i}:00:00Z", "flow": f} for i, f in enumerate(flows)]
    return {
        "reach": {"reachId": "4552334"},
        "analysisAssimilation": {},
        "mediumRange": {},
        "shortRange": {"series": {"referenceTime": reference, "units": "ft³/s", "data": data}},
    }


def test_parse_gauge():
    assert nwps.parse_gauge(nwps_gauge()) == nwps.NwpsGauge("BVAW2", 4552334, True)
    assert not nwps.parse_gauge(nwps_gauge(forecast="")).has_rfc_forecast


def test_parse_rfc_forecast_converts_kcfs_and_missing_values():
    forecast = nwps.parse_rfc_forecast(rfc_forecast())
    assert forecast.issued_at == datetime(2026, 10, 18, 14, 48, tzinfo=UTC)
    assert forecast.points == [
        (datetime(2026, 10, 18, 18, tzinfo=UTC), 2720.0, 4.1),
        (datetime(2026, 10, 19, 0, tzinfo=UTC), None, 4.0),
    ]


def test_parse_rfc_forecast_without_issuance():
    assert nwps.parse_rfc_forecast({"issuedTime": "", "data": []}) is None


def test_parse_nwm():
    forecast = nwps.parse_nwm(nwm_short())
    assert forecast.issued_at == datetime(2026, 10, 18, 18, tzinfo=UTC)
    assert forecast.points[0] == (datetime(2026, 10, 18, 19, tzinfo=UTC), 2801.9, None)


def test_parse_nwm_empty_series():
    assert nwps.parse_nwm({"shortRange": {}}) is None


def test_parse_comid():
    body = {
        "type": "FeatureCollection",
        "features": [{"properties": {"identifier": "USGS-02186699", "comid": 11749361}}],
    }
    assert nldi.parse_comid(body) == 11749361


class FakeNOAA:
    def __init__(self):
        self.gauges: dict[str, dict] = {}
        self.comids: dict[str, int] = {}
        self.rfc: dict[str, dict] = {}
        self.nwm: dict[int, dict] = {}
        self.requests: list[str] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        url = request.url
        self.requests.append(str(url))
        parts = url.path.strip("/").split("/")
        if url.host == "api.water.noaa.gov":
            if parts[2] == "gauges" and len(parts) == 4 and parts[3] in self.gauges:
                return httpx2.Response(200, json=self.gauges[parts[3]])
            if parts[2] == "gauges" and parts[-1] == "forecast" and parts[3] in self.rfc:
                return httpx2.Response(200, json=self.rfc[parts[3]])
            if parts[2] == "reaches" and int(parts[3]) in self.nwm:
                return httpx2.Response(200, json=self.nwm[int(parts[3])])
        if url.host == "api.water.usgs.gov":
            number = parts[-1].removeprefix("USGS-")
            if number in self.comids:
                return httpx2.Response(
                    200, json={"features": [{"properties": {"comid": self.comids[number]}}]}
                )
        return httpx2.Response(404, json={"code": 5, "message": "not found"})


def client_for(fake: FakeNOAA) -> PoliteClient:
    return PoliteClient("test", 0, transport=httpx2.MockTransport(fake.handler))


def add_gauge(session, number, source="USGS", **fields) -> Gauge:
    gauge = Gauge(source=source, source_id=number, name=number, **fields)
    session.add(gauge)
    session.commit()
    return gauge


def test_link_uses_nwps_then_falls_back_to_nldi(session):
    add_gauge(session, "03192000")
    add_gauge(session, "02186699")  # not an NWS location
    add_gauge(session, "00000000")  # unknown everywhere
    add_gauge(session, "36510909", source="CALCULATED")  # ignored
    fake = FakeNOAA()
    fake.gauges["03192000"] = nwps_gauge()
    fake.comids["02186699"] = 11749361

    with client_for(fake) as client:
        stats = jobs.link_gauges(client, session)

    assert (stats.gauges, stats.forecast_points, stats.with_reach) == (3, 1, 2)
    rows = session.execute(
        select(Gauge.source_id, Gauge.nws_lid, Gauge.nwm_reach_id, Gauge.has_rfc_forecast)
        .where(Gauge.source == "USGS")
        .order_by(Gauge.source_id)
    ).all()
    assert [tuple(r) for r in rows] == [
        ("00000000", None, None, False),
        ("02186699", None, 11749361, False),
        ("03192000", "BVAW2", 4552334, True),
    ]
    with client_for(fake) as client:
        assert jobs.link_gauges(client, session).gauges == 0  # fresh links aren't redone


def linked(session, number, lid=None, reach=None, rfc=False) -> Gauge:
    return add_gauge(
        session,
        number,
        nws_lid=lid,
        nwm_reach_id=reach,
        has_rfc_forecast=rfc,
        noaa_linked_at=datetime.now(UTC),
    )


def stored(session):
    return [
        tuple(r)
        for r in session.execute(
            select(
                NoaaForecast.source,
                NoaaForecast.issued_at,
                NoaaForecast.valid_time,
                NoaaForecast.flow_cfs,
                NoaaForecast.stage_ft,
            ).order_by(NoaaForecast.source, NoaaForecast.issued_at, NoaaForecast.valid_time)
        )
    ]


def test_archive_stores_rfc_and_nwm_forecasts(session):
    linked(session, "02037500", lid="RMDV2", reach=8574021, rfc=True)
    linked(session, "02186699", reach=11749361)
    fake = FakeNOAA()
    fake.rfc["RMDV2"] = rfc_forecast()
    fake.nwm[8574021] = nwm_short()
    fake.nwm[11749361] = nwm_short(flows=(12.4,))

    with client_for(fake) as client:
        stats = jobs.archive(client, session)

    assert (stats.rfc_new, stats.nwm_new, stats.failed) == (1, 2, 0)
    rows = stored(session)
    assert len(rows) == 2 + 2 + 1
    assert rows[-2:] == [
        (
            "rfc",
            datetime(2026, 10, 18, 14, 48, tzinfo=UTC),
            datetime(2026, 10, 18, 18, tzinfo=UTC),
            2720.0,
            4.1,
        ),
        (
            "rfc",
            datetime(2026, 10, 18, 14, 48, tzinfo=UTC),
            datetime(2026, 10, 19, 0, tzinfo=UTC),
            None,
            4.0,
        ),
    ]


def test_archive_keeps_every_issuance_and_skips_repeats(session):
    linked(session, "02037500", lid="RMDV2", reach=8574021, rfc=True)
    fake = FakeNOAA()
    fake.rfc["RMDV2"] = rfc_forecast()
    fake.nwm[8574021] = nwm_short()
    with client_for(fake) as client:
        jobs.archive(client, session)
        repeat = jobs.archive(client, session)
        fake.nwm[8574021] = nwm_short(reference="2026-10-18T19:00:00Z")
        newer = jobs.archive(client, session)

    assert (repeat.unchanged, repeat.nwm_new, repeat.rfc_new) == (2, 0, 0)
    assert (newer.nwm_new, newer.unchanged) == (1, 1)
    issuances = {(source, issued) for source, issued, *_ in stored(session)}
    assert len(issuances) == 3


def test_archive_continues_after_a_failed_gauge(session):
    linked(session, "1", reach=1)  # NWPS has nothing for this reach -> 404
    linked(session, "2", reach=2)
    fake = FakeNOAA()
    fake.nwm[2] = nwm_short()
    with client_for(fake) as client:
        stats = jobs.archive(client, session)
    assert (stats.failed, stats.nwm_new) == (1, 1)


def test_archive_counts_empty_forecasts(session):
    linked(session, "03185400", lid="THMW2", reach=6920810, rfc=True)
    fake = FakeNOAA()
    fake.rfc["THMW2"] = {"issuedTime": "", "data": []}
    fake.nwm[6920810] = {"shortRange": {}}
    with client_for(fake) as client:
        stats = jobs.archive(client, session)
    assert (stats.empty, stats.rfc_new, stats.nwm_new) == (2, 0, 0)
