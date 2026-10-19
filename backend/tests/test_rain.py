from datetime import UTC, datetime

import httpx2
import numpy as np
import pyarrow.parquet as pq
import pytest
from aorc_fake import FakeAorc
from sqlalchemy import func

from riverrat.db.models import Basin, Gauge
from riverrat.http import PoliteClient
from riverrat.jobs import rain as jobs
from riverrat.sources import aorc

HOURS = 6  # two time chunks of 4 hours, the second ragged

# Basin A: 2 × 2 cells inside spatial chunk (0, 0).
# Basin B: one row of 2 cells straddling chunks (0, 0) and (0, 1).
BASIN_A = "MULTIPOLYGON(((-82 38,-81.8 38,-81.8 38.2,-82 38.2,-82 38)))"
BASIN_B = "MULTIPOLYGON(((-81.7 38,-81.5 38,-81.5 38.1,-81.7 38.1,-81.7 38)))"


def field(hours: int = HOURS) -> np.ndarray:
    """Rain that grows with the hour and with longitude: (h + 1) · (j + 1) · 0.5 mm."""
    h = np.arange(hours)[:, None, None] + 1
    j = np.arange(8)[None, None, :] + 1
    return np.broadcast_to(h * j * 0.5, (hours, 8, 8)).astype(float).copy()


def add_basin(session, site: str, wkt: str) -> None:
    gauge = Gauge(source="USGS", source_id=site, name=site)
    session.add(gauge)
    session.flush()
    session.add(
        Basin(
            gauge_id=gauge.id,
            geom=func.ST_GeomFromText(wkt, 4326),
            area_sqmi=1.0,
            source="test",
            fetched_at=datetime.now(UTC),
        )
    )
    session.commit()


def run(fake: FakeAorc, session, tmp_path, **kwargs):
    with PoliteClient("test", 0, transport=httpx2.MockTransport(fake.handler)) as client:
        return jobs.backfill(client, session, data_dir=tmp_path, start_year=2002, **kwargs)


def series(tmp_path, year: int, site: str) -> list:
    table = pq.read_table(jobs.rain_path(tmp_path, year)).to_pylist()
    return [row for row in table if row["site"] == site]


def test_parse_year_listing():
    xml = (
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        "<CommonPrefixes><Prefix>2003.zarr/</Prefix></CommonPrefixes>"
        "<CommonPrefixes><Prefix>1979.zarr/</Prefix></CommonPrefixes>"
        "<CommonPrefixes><Prefix>docs/</Prefix></CommonPrefixes></ListBucketResult>"
    )
    assert aorc.parse_year_listing(xml) == [1979, 2003]


def test_year_store_decodes_scale_fill_and_padding():
    rain = field()
    rain[0, 0, 0] = np.nan
    fake = FakeAorc({2010: rain})
    with PoliteClient("test", 0, transport=httpx2.MockTransport(fake.handler)) as client:
        store = aorc.Year(client, 2010)
        assert store.lat == pytest.approx(np.round(38.05 + 0.1 * np.arange(8), 4))
        assert len(store.time) == HOURS
        assert store.time[0] == np.datetime64("2010-01-01T01:00:00")
        chunk = store.rain_chunk(0, 0, 1)
        assert chunk.shape == (4, 4, 4)
        assert chunk[1, 0, 0] == pytest.approx(2 * 5 * 0.5)
        assert np.isnan(store.rain_chunk(0, 0, 0)[0, 0, 0])
        assert np.isnan(store.rain_chunk(1, 0, 0)[2:]).all()  # padding past the last hour


def test_backfill_writes_hourly_basin_means(session, tmp_path):
    add_basin(session, "A", BASIN_A)
    add_basin(session, "B", BASIN_B)
    fake = FakeAorc({2001: field(), 2010: field()})

    stats = run(fake, session, tmp_path)

    assert (stats.years_done, stats.basins_computed) == (1, 2)  # 2001 is before start_year
    assert not jobs.rain_path(tmp_path, 2001).exists()
    a, b = series(tmp_path, 2010, "A"), series(tmp_path, 2010, "B")
    assert [r["ts"] for r in a] == [datetime(2010, 1, 1, h + 1, tzinfo=UTC) for h in range(HOURS)]
    assert [r["mm"] for r in a] == pytest.approx([(h + 1) * 0.75 for h in range(HOURS)])
    assert [r["mm"] for r in b] == pytest.approx([(h + 1) * 2.25 for h in range(HOURS)])
    # Only the two spatial chunks that touch a basin, for each of the two time chunks.
    assert len(fake.chunk_requests()) == 4


def test_missing_cells_are_skipped_until_coverage_is_too_low(session, tmp_path):
    add_basin(session, "B", BASIN_B)
    rain = field()
    rain[0, 0, 3] = np.nan  # hour 0: half of B missing, the other half still counts
    rain[1, 0, 3:5] = np.nan  # hour 1: all of B missing
    run(FakeAorc({2010: rain}), session, tmp_path)
    mm = [r["mm"] for r in series(tmp_path, 2010, "B")]
    assert mm[0] == pytest.approx(5 * 0.5)
    assert mm[1] is None
    assert mm[2] == pytest.approx(3 * 2.25)


def test_reruns_compute_only_new_basins(session, tmp_path):
    add_basin(session, "A", BASIN_A)
    fake = FakeAorc({2010: field()})
    run(fake, session, tmp_path)
    before = len(fake.chunk_requests())

    stats = run(fake, session, tmp_path)
    assert (stats.years_skipped, len(fake.chunk_requests())) == (1, before)

    add_basin(session, "B", BASIN_B)
    stats = run(fake, session, tmp_path)
    assert stats.basins_computed == 1
    assert {r["site"] for r in pq.read_table(jobs.rain_path(tmp_path, 2010)).to_pylist()} == {
        "A",
        "B",
    }
    assert len(series(tmp_path, 2010, "A")) == HOURS

    stats = run(fake, session, tmp_path, refresh=True)
    assert stats.basins_computed == 2
    assert len(series(tmp_path, 2010, "B")) == HOURS


def test_year_filter(session, tmp_path):
    add_basin(session, "A", BASIN_A)
    run(FakeAorc({2010: field(), 2011: field()}), session, tmp_path, years=[2011])
    assert not jobs.rain_path(tmp_path, 2010).exists()
    assert jobs.rain_path(tmp_path, 2011).exists()
