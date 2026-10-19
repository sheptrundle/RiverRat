from datetime import UTC, datetime, timedelta

import eccodes
import httpx2
import numpy as np
import pytest
from sqlalchemy import func, select
from stage4_fake import NX, NY, FakeStage4, grib

from riverrat.db.models import Basin, BasinRain, Gauge, RainHour
from riverrat.http import PoliteClient
from riverrat.jobs import rain as jobs
from riverrat.sources import stage4

NOW = datetime(2026, 10, 19, 14, 20, tzinfo=UTC)
LATEST = datetime(2026, 10, 19, 14, tzinfo=UTC)
FIELD = np.add.outer(10.0 * np.arange(NY), np.arange(NX))  # value = 10·row + column
GRID = stage4.decode(grib(LATEST, FIELD)).grid


def cells_polygon(rows: tuple[float, float], cols: tuple[float, float]) -> str:
    """WKT (lon/lat) of the grid-aligned rectangle spanning these fractional cell edges."""
    dx = GRID.x[1] - GRID.x[0]
    x0, x1 = (GRID.x[0] - dx / 2 + c * dx for c in cols)
    y0, y1 = (GRID.y[0] - dx / 2 + r * dx for r in rows)
    corners = [GRID.projection.inverse(x, y) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    ring = ", ".join(f"{lon} {lat}" for lon, lat in [*corners, corners[0]])
    return f"MULTIPOLYGON((({ring})))"


def add_basin(session, site: str, wkt: str) -> int:
    gauge = Gauge(source="USGS", source_id=site, name=site)
    session.add(gauge)
    session.flush()
    session.add(
        Basin(
            gauge_id=gauge.id,
            geom=func.ST_GeomFromText(wkt, 4326),
            area_sqmi=1.0,
            source="test",
            fetched_at=NOW,
        )
    )
    session.commit()
    return gauge.id


def serve(fake: FakeStage4, hours: list[datetime], field=FIELD, modified=NOW) -> None:
    for hour in hours:
        fake.put(stage4.url_for(hour, NOW), grib(hour, field), modified)


def run(fake: FakeStage4, session, start: datetime, **kwargs):
    with PoliteClient("test", 0, transport=httpx2.MockTransport(fake.handler)) as client:
        return jobs.update_recent(client, session, start=start, now=NOW, **kwargs)


def rain(session, gauge_id: int) -> dict[datetime, float | None]:
    rows = session.execute(
        select(BasinRain.ts, BasinRain.mm).where(BasinRain.gauge_id == gauge_id)
    ).all()
    return dict(rows)


@pytest.fixture
def basins(session):
    return {
        "block": add_basin(session, "A", cells_polygon((1, 3), (1, 4))),  # rows 1–2, cols 1–3
        "half": add_basin(session, "B", cells_polygon((0, 1), (0, 0.5))),  # half of cell (0, 0)
        "away": add_basin(session, "C", "MULTIPOLYGON(((-70 30,-69 30,-69 31,-70 31,-70 30)))"),
    }


def hours_back(n: int) -> list[datetime]:
    return [LATEST - timedelta(hours=k) for k in range(n)]


def test_grid_coordinates_match_eccodes():
    handle = eccodes.codes_new_from_message(grib(LATEST, FIELD))
    try:
        lat = eccodes.codes_get_array(handle, "latitudes").reshape(NY, NX)
        lon = eccodes.codes_get_array(handle, "longitudes").reshape(NY, NX)
    finally:
        eccodes.codes_release(handle)
    x, y = GRID.projection.forward(lon, lat)
    assert x == pytest.approx(np.broadcast_to(GRID.x, (NY, NX)), abs=0.01)
    assert y == pytest.approx(np.broadcast_to(GRID.y[:, None], (NY, NX)), abs=0.01)
    back_lon, back_lat = GRID.projection.inverse(x, y)
    assert back_lat == pytest.approx(lat) and back_lon % 360 == pytest.approx(lon % 360)


def test_urls_switch_from_nomads_to_iem_after_a_few_days():
    recent, old = LATEST - timedelta(hours=3), LATEST - timedelta(days=10)
    assert stage4.url_for(recent, NOW) == (
        f"{stage4.NOMADS_URL}/pcpanl.20261019/st4_conus.2026101911.01h.grb2"
    )
    assert stage4.url_for(old, NOW) == f"{stage4.IEM_URL}/2026/10/09/stage4/ST4.2026100914.01h.grib"


def test_stores_area_weighted_basin_means_for_each_hour(session, basins):
    fake = FakeStage4()
    serve(fake, hours_back(4))
    stats = run(fake, session, start=LATEST - timedelta(hours=3))

    assert (stats.hours, stats.failed, stats.unavailable) == (4, 0, 0)
    block = rain(session, basins["block"])
    assert set(block) == set(hours_back(4))
    assert block[LATEST] == pytest.approx(10 * 1.5 + 2.0, abs=1e-3)
    assert rain(session, basins["half"])[LATEST] == pytest.approx(0.0, abs=1e-3)
    assert rain(session, basins["away"])[LATEST] is None
    assert session.scalar(select(func.count()).select_from(RainHour)) == 4


def test_missing_cells_count_only_while_coverage_suffices(session, basins):
    field = FIELD.copy()
    field[1, 1:3] = np.nan  # 2 of the block basin's 6 cells
    fake = FakeStage4()
    serve(fake, [LATEST], field)
    run(fake, session, start=LATEST)
    remaining = (13 + 21 + 22 + 23) / 4
    assert rain(session, basins["block"])[LATEST] == pytest.approx(remaining, abs=1e-3)

    field[2, 1:3] = np.nan  # now only 2 of 6 remain
    serve(fake, [LATEST], field, modified=NOW + timedelta(minutes=5))
    run(fake, session, start=LATEST, refresh=True)
    assert rain(session, basins["block"])[LATEST] is None


def test_reruns_only_refetch_revised_hours(session, basins):
    fake = FakeStage4()
    serve(fake, hours_back(3))
    run(fake, session, start=LATEST - timedelta(hours=2))
    fake.gets.clear()

    stats = run(fake, session, start=LATEST - timedelta(hours=2))
    assert (stats.hours, fake.gets, len(fake.heads)) == (0, [], 3)

    revised_hour = LATEST - timedelta(hours=1)
    serve(fake, [revised_hour], FIELD + 1, modified=NOW + timedelta(hours=1))
    stats = run(fake, session, start=LATEST - timedelta(hours=2))
    assert (stats.hours, stats.revised) == (1, 1)
    assert rain(session, basins["block"])[revised_hour] == pytest.approx(18.0, abs=1e-3)


def test_unpublished_hours_are_retried_next_run(session, basins):
    fake = FakeStage4()
    serve(fake, [LATEST - timedelta(hours=1)])  # the latest hour isn't out yet
    stats = run(fake, session, start=LATEST - timedelta(hours=1))
    assert (stats.hours, stats.unavailable) == (1, 1)

    serve(fake, [LATEST])
    assert run(fake, session, start=LATEST - timedelta(hours=1)).hours == 1
    assert LATEST in rain(session, basins["block"])


def test_max_hours_takes_the_newest_first(session, basins):
    fake = FakeStage4()
    serve(fake, hours_back(5))
    stats = run(fake, session, start=LATEST - timedelta(hours=4), max_hours=2)
    assert (stats.hours, stats.remaining) == (2, 3)
    assert set(rain(session, basins["block"])) == set(hours_back(2))


def test_a_file_for_the_wrong_hour_is_rejected(session, basins):
    fake = FakeStage4()
    fake.put(stage4.url_for(LATEST, NOW), grib(LATEST - timedelta(hours=1), FIELD), NOW)
    stats = run(fake, session, start=LATEST)
    assert (stats.hours, stats.failed) == (0, 1)
    assert session.scalar(select(func.count()).select_from(RainHour)) == 0
