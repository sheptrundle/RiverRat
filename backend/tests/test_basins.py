import math

import httpx2
import pytest
from sqlalchemy import func, select

from riverrat.db.models import Basin, Gauge
from riverrat.http import PoliteClient
from riverrat.jobs import basins as jobs
from riverrat.sources import nldi


def square(lon: float, lat: float, size: float = 0.1) -> dict:
    ring = [[lon, lat], [lon + size, lat], [lon + size, lat + size], [lon, lat + size], [lon, lat]]
    return {"type": "Polygon", "coordinates": [ring]}


BOW_TIE = {  # self-intersecting ring, as sometimes comes out of delineation tools
    "type": "Polygon",
    "coordinates": [[[-81.0, 38.0], [-80.9, 38.1], [-80.9, 38.0], [-81.0, 38.1], [-81.0, 38.0]]],
}


def square_area_sqmi(lat: float, size: float = 0.1) -> float:
    km_per_deg = 111.32
    km2 = (size * km_per_deg) * (size * km_per_deg * math.cos(math.radians(lat + size / 2)))
    return km2 / 2.58999


class FakeNLDI:
    def __init__(self, basins: dict[str, dict | None]):
        self.basins = basins
        self.requests = 0

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests += 1
        number = request.url.path.split("/")[-2].removeprefix("USGS-")
        if number not in self.basins:
            return httpx2.Response(404, json={"type": "error"})
        geometry = self.basins[number]
        features = [{"type": "Feature", "properties": {}, "geometry": geometry}] if geometry else []
        return httpx2.Response(200, json={"type": "FeatureCollection", "features": features})


def run(fake: FakeNLDI, session, **kwargs):
    with PoliteClient("test", 0, transport=httpx2.MockTransport(fake.handler)) as client:
        return jobs.sync_basins(client, session, **kwargs)


def add_gauge(session, number, drainage=None, source="USGS"):
    gauge = Gauge(source=source, source_id=number, name=number, drainage_area_sqmi=drainage)
    session.add(gauge)
    session.commit()
    return gauge


def test_parse_basin():
    body = {"features": [{"geometry": square(-81, 38)}]}
    assert nldi.parse_basin(body)["type"] == "Polygon"
    assert nldi.parse_basin({"features": []}) is None
    assert nldi.parse_basin({"features": [{"geometry": {"type": "Point"}}]}) is None


def test_basins_are_stored_with_area_and_ratio(session):
    expected = square_area_sqmi(38.0)
    add_gauge(session, "A", drainage=expected)
    add_gauge(session, "B", drainage=expected * 2)  # published area twice ours: mismatch
    add_gauge(session, "C")  # no published drainage area yet
    add_gauge(session, "X", source="CALCULATED")  # not USGS: ignored
    fake = FakeNLDI({"A": square(-81, 38), "B": square(-81, 38), "C": square(-81, 38)})

    stats = run(fake, session)

    assert (stats.fetched, stats.mismatched, stats.failed) == (3, 1, 0)
    rows = dict(
        session.execute(
            select(Gauge.source_id, Basin.area_ratio).join(Basin, Basin.gauge_id == Gauge.id)
        ).all()
    )
    assert rows["A"] == pytest.approx(1.0, abs=0.01)
    assert rows["B"] == pytest.approx(0.5, abs=0.01)
    assert rows["C"] is None
    assert session.scalar(select(func.ST_GeometryType(Basin.geom)).limit(1)) == "ST_MultiPolygon"


def test_only_missing_basins_are_fetched_unless_refreshing(session):
    add_gauge(session, "A")
    fake = FakeNLDI({"A": square(-81, 38)})
    run(fake, session)
    run(fake, session)
    assert fake.requests == 1
    run(fake, session, refresh=True)
    assert fake.requests == 2
    assert session.scalar(select(func.count()).select_from(Basin)) == 1


def test_area_ratios_update_when_drainage_area_arrives_later(session):
    gauge = add_gauge(session, "A")
    fake = FakeNLDI({"A": square(-81, 38)})
    run(fake, session)
    gauge.drainage_area_sqmi = square_area_sqmi(38.0)
    session.commit()
    run(fake, session)  # nothing to fetch, but ratios are recomputed
    assert session.scalar(select(Basin.area_ratio)) == pytest.approx(1.0, abs=0.01)


def test_unknown_gauges_and_empty_basins_are_counted_not_stored(session):
    add_gauge(session, "A")  # 404
    add_gauge(session, "B")  # 200, no features
    stats = run(FakeNLDI({"B": None}), session)
    assert (stats.fetched, stats.missing) == (0, 2)
    assert session.scalar(select(func.count()).select_from(Basin)) == 0


def test_invalid_polygons_are_repaired(session):
    add_gauge(session, "A")
    run(FakeNLDI({"A": BOW_TIE}), session)
    assert session.scalar(select(func.ST_IsValid(Basin.geom))) is True
    assert session.scalar(select(Basin.area_sqmi)) > 0
