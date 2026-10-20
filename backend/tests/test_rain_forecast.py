from datetime import UTC, datetime, timedelta

import eccodes
import httpx2
import numpy as np
import pytest
from nbm_fake import NX, NY, FakeNbm, message
from sqlalchemy import func, select

from riverrat.db.models import Basin, Gauge, RainForecast, RainForecastRun
from riverrat.geo.projection import LambertConformal
from riverrat.http import PoliteClient
from riverrat.jobs import rain_forecast as jobs
from riverrat.sources import grib, nbm

NOW = datetime(2026, 10, 20, 16, 30, tzinfo=UTC)
RUN = datetime(2026, 10, 20, 12, tzinfo=UTC)
GRID = grib.decode(message(RUN, 6, 50, np.zeros((NY, NX)))).grid


def add_basin(session, site: str) -> int:
    """A basin covering the middle of the fake grid."""
    dx = GRID.x[1] - GRID.x[0]
    x0, x1 = GRID.x[1] - dx / 2, GRID.x[4] + dx / 2
    y0, y1 = GRID.y[1] - dx / 2, GRID.y[3] + dx / 2
    corners = [GRID.projection.inverse(x, y) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    ring = ", ".join(f"{lon} {lat}" for lon, lat in [*corners, corners[0]])
    gauge = Gauge(source="USGS", source_id=site, name=site)
    session.add(gauge)
    session.flush()
    session.add(
        Basin(
            gauge_id=gauge.id,
            geom=func.ST_GeomFromText(f"MULTIPOLYGON((({ring})))", 4326),
            area_sqmi=1.0,
            source="test",
            fetched_at=NOW,
        )
    )
    session.commit()
    return gauge.id


def run(fake: FakeNbm, session, **kwargs):
    with PoliteClient("test", 0, transport=httpx2.MockTransport(fake.handler)) as client:
        return jobs.archive(client, session, now=NOW, workers=2, **kwargs)


def test_lambert_grid_matches_eccodes():
    handle = eccodes.codes_new_from_message(message(RUN, 6, 50, np.zeros((NY, NX))))
    try:
        lat = eccodes.codes_get_array(handle, "latitudes").reshape(NY, NX)
        lon = eccodes.codes_get_array(handle, "longitudes").reshape(NY, NX)
    finally:
        eccodes.codes_release(handle)
    lons, lats = GRID.projection.inverse(*np.meshgrid(GRID.x, GRID.y))
    np.testing.assert_allclose(lats, lat, atol=1e-9)
    np.testing.assert_allclose(lons, (lon + 180) % 360 - 180, atol=1e-9)
    x, y = GRID.projection.forward(lons, lats)
    np.testing.assert_allclose(x, np.meshgrid(GRID.x, GRID.y)[0], atol=1e-6)


def test_secant_lambert_round_trips():
    projection = LambertConformal(lat_1=33.0, lat_2=45.0, lon_0=-96.0, radius=6371200.0)
    lon, lat = np.array([-120.0, -80.5, -96.0]), np.array([30.0, 38.2, 49.0])
    back = projection.inverse(*projection.forward(lon, lat))
    np.testing.assert_allclose(back, (lon, lat), atol=1e-9)


def test_runs_between_lists_long_range_cycles_newest_first():
    runs = jobs.runs_between(datetime(2026, 10, 19, 5, 30, tzinfo=UTC), NOW)
    assert runs == [RUN - timedelta(hours=h) for h in (0, 6, 12, 18, 24, 30)]


def test_index_parsing_picks_the_period_and_percentiles():
    fake = FakeNbm()
    fake.publish(RUN, leads=[24])
    idx = fake.files[nbm.qmd_url(RUN, 24) + ".idx"].decode()
    ranges = nbm.percentile_ranges(idx, 24)
    assert [r.percentile for r in ranges] == list(nbm.PERCENTILES)
    body = fake.files[nbm.qmd_url(RUN, 24)]
    first = grib.decode(body[ranges[0].start : ranges[0].end + 1])
    assert first.percentile == 5
    with pytest.raises(ValueError, match="lacks percentiles"):
        nbm.percentile_ranges(idx, 30)


def test_archives_basin_percentiles_for_every_period(session):
    gauge_id = add_basin(session, "01234567")
    fake = FakeNbm()
    fake.publish(RUN)

    stats = run(fake, session, max_runs=1)
    assert stats.runs == 1
    assert stats.not_published == 0  # older runs weren't attempted after the cap
    rows = session.scalars(
        select(RainForecast).where(RainForecast.gauge_id == gauge_id).order_by("valid_end")
    ).all()
    assert len(rows) == len(nbm.leads())
    assert rows[0].valid_end == RUN + timedelta(hours=6)
    assert rows[-1].valid_end == RUN + timedelta(hours=nbm.HORIZON_H)
    assert (rows[0].p05, rows[0].p50, rows[0].p95) == pytest.approx((0.5, 5.0, 9.5))
    # only the wanted fields were downloaded
    ranged = [r for r in fake.requests if r[2] is not None]
    assert len(ranged) == len(nbm.leads()) * len(nbm.PERCENTILES)


def test_unpublished_runs_are_skipped_and_retried(session):
    add_basin(session, "01234567")
    fake = FakeNbm()
    fake.publish(RUN, leads=list(nbm.leads())[:-1])  # still uploading
    fake.publish(RUN - timedelta(hours=6))

    stats = run(fake, session, since=RUN - timedelta(hours=6))
    assert (stats.runs, stats.not_published) == (1, 1)
    stored = set(session.scalars(select(RainForecastRun.issued_at)))
    assert stored == {RUN - timedelta(hours=6)}

    fake.publish(RUN)
    fake.requests.clear()
    stats = run(fake, session, since=RUN - timedelta(hours=6))
    assert (stats.runs, stats.not_published) == (1, 0)
    assert all(RUN.strftime("blend.%Y%m%d/%H") in url for _, url, _ in fake.requests)


def test_a_field_for_the_wrong_run_fails_the_whole_run(session):
    add_basin(session, "01234567")
    fake = FakeNbm()
    fake.publish(RUN, wrong_start=True)

    stats = run(fake, session, since=RUN)
    assert (stats.runs, stats.failed) == (0, 1)
    assert session.scalar(select(func.count()).select_from(RainForecast)) == 0
    assert session.scalar(select(func.count()).select_from(RainForecastRun)) == 0
