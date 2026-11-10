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
    """Basins by gauge number; `comids` maps a "lon lat" point to a COMID whose basin is
    `comid_basins[comid]`."""

    def __init__(self, basins: dict[str, dict | None], comids=None, comid_basins=None):
        self.basins = basins
        self.comids = comids or {}
        self.comid_basins = comid_basins or {}
        self.requests = 0

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests += 1
        path = request.url.path
        if path.endswith("/comid/position"):
            point = request.url.params["coords"].removeprefix("POINT(").removesuffix(")")
            comid = self.comids.get(point)
            if comid is None:
                return httpx2.Response(404, json={"type": "error"})
            return httpx2.Response(200, json={"features": [{"properties": {"comid": str(comid)}}]})
        if "/comid/" in path:
            geometry = self.comid_basins[int(path.split("/")[-2])]
            return httpx2.Response(200, json={"features": [{"geometry": geometry}]})
        number = path.split("/")[-2].removeprefix("USGS-")
        if number not in self.basins:
            return httpx2.Response(404, json={"type": "error"})
        geometry = self.basins[number]
        features = [{"type": "Feature", "properties": {}, "geometry": geometry}] if geometry else []
        return httpx2.Response(200, json={"type": "FeatureCollection", "features": features})


def run(fake: FakeNLDI, session, **kwargs):
    with PoliteClient("test", 0, transport=httpx2.MockTransport(fake.handler)) as client:
        return jobs.sync_basins(client, session, **kwargs)


def add_gauge(session, number, drainage=None, source="USGS", at=None):
    gauge = Gauge(source=source, source_id=number, name=number, drainage_area_sqmi=drainage)
    if at:
        gauge.geom = func.ST_SetSRID(func.ST_MakePoint(*at), 4326)
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


def test_unindexed_gauges_fall_back_to_the_reach_at_their_location(session):
    area = square_area_sqmi(38.0)
    add_gauge(session, "A", drainage=area, at=(-80.95, 38.05))
    add_gauge(session, "B", drainage=area * 3, at=(-79.95, 38.05))  # snapped to a side stream
    fake = FakeNLDI(
        {},
        comids={"-80.95 38.05": 11, "-79.95 38.05": 22},
        comid_basins={11: square(-81, 38), 22: square(-80, 38)},
    )

    stats = run(fake, session)

    assert (stats.fetched, stats.by_position, stats.missing) == (1, 1, 1)
    stored = session.execute(select(Gauge.source_id).join(Basin, Basin.gauge_id == Gauge.id)).all()
    assert [s for (s,) in stored] == ["A"]


# --- repairs from StreamStats -----------------------------------------------------------


def streamstats_transport(polygons):
    """StreamStats: `polygons` maps a "lon lat" point to the watershed there."""
    calls = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        point = f"{request.url.params['lon']} {request.url.params['lat']}"
        calls.append(point)
        polygon = polygons.get(point)
        if polygon is None:
            return httpx2.Response(200, json={"bcrequest": {"wsresp": {"featurecollection": []}}})
        feature = {"type": "Feature", "properties": {"GlobalWshd": 1}, "geometry": polygon}
        return httpx2.Response(
            200, json={"bcrequest": {"wsresp": {"featurecollection": [["w", feature]]}}}
        )

    return httpx2.MockTransport(handler), calls


def add_mismatched(session, number="01234567", at=(-80.0, 38.0)):
    """A gauge whose NLDI basin is 4 times the published area."""
    drainage = square_area_sqmi(38.0)
    gauge_id = add_gauge(session, number, drainage=drainage, at=at).id
    run(FakeNLDI({number: square(-80.0, 38.0, 0.2)}), session)
    session.query(Gauge).filter_by(id=gauge_id).update({"state_code": "51"})
    session.commit()
    return gauge_id


def repair(session, transport):
    with PoliteClient("test", 0, transport=transport) as client:
        return jobs.repair_basins(client, session)


def test_a_wrong_basin_is_repaired_and_a_refresh_leaves_it(session):
    gauge_id = add_mismatched(session)
    transport, calls = streamstats_transport({"-80.0 38.0": square(-80.0, 38.0)})
    stats = repair(session, transport)
    assert (stats.tried, stats.repaired, stats.repaired_sites) == (1, 1, ["01234567"])
    assert len(calls) == 1  # good on the first try
    basin = session.get(Basin, gauge_id)
    session.refresh(basin)
    assert basin.source == "streamstats"
    assert basin.area_ratio == pytest.approx(1, abs=0.02)

    fake = FakeNLDI({"01234567": square(-80.0, 38.0, 0.2)})
    assert run(fake, session, refresh=True).fetched == 0
    session.refresh(basin)
    assert basin.source == "streamstats"


def test_a_point_off_the_stream_grid_is_nudged(session):
    add_mismatched(session)
    transport, calls = streamstats_transport(
        {"-80.0 38.0": square(-80.0, 38.0, 0.001), "-79.999 38.0": square(-80.0, 38.0)}
    )
    assert repair(session, transport).repaired == 1
    assert len(calls) == 2


def test_a_basin_stays_when_nothing_is_closer(session):
    gauge_id = add_mismatched(session)
    transport, calls = streamstats_transport({})
    stats = repair(session, transport)
    assert (stats.repaired, stats.unchanged) == (0, 1)
    assert len(calls) == 1 + len(jobs.NUDGES)
    assert session.get(Basin, gauge_id).source == "nldi"
