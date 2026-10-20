from datetime import UTC, datetime, timedelta

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import func

from riverrat.db.models import Basin, BasinRain, Gauge
from riverrat.features import events, hourly
from riverrat.jobs import events as jobs
from riverrat.jobs import rain as rain_jobs
from riverrat.jobs import usgs as usgs_jobs
from riverrat.storage import write_parquet

T0 = datetime(2024, 1, 1, 1, tzinfo=UTC)


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def readings(rows: list[tuple[datetime, float, str | None]]) -> pa.Table:
    return pa.table(
        {
            "ts": pa.array([r[0] for r in rows], pa.timestamp("us", tz="UTC")),
            "value": pa.array([r[1] for r in rows], pa.float64()),
            "approved": pa.array([True] * len(rows)),
            "qualifier": pa.array([r[2] for r in rows], pa.string()),
        }
    )


# --- hourly flow ---------------------------------------------------------------------


def test_hour_ends_include_both_ends():
    hours = hourly.hour_ends(T0, at(3))
    assert len(hours) == 4
    assert hours[-1] == np.datetime64("2024-01-01T04:00:00")


def test_hourly_flow_averages_each_hour_ending_on_the_label():
    hours = hourly.hour_ends(T0, at(3))
    table = readings(
        [
            (at(0), 10.0, None),  # exactly 01:00 → hour ending 01:00
            (at(0.25), 20.0, None),  # hour ending 02:00
            (at(1), 40.0, None),
            (at(1) + timedelta(microseconds=3), 999.0, "['ICE']"),
            (at(2.5), 50.0, "['EQUIP', 'ESTIMATED']"),
            (at(3), 70.0, "['ESTIMATED']"),
        ]
    )
    cfs = hourly.hourly_flow(table, hours)
    assert cfs[0] == 10.0
    assert cfs[1] == 30.0
    assert np.isnan(cfs[2])  # its only reading was flagged
    assert cfs[3] == 70.0


def test_hourly_flow_ignores_readings_outside_the_hours():
    hours = hourly.hour_ends(T0, at(1))
    table = readings([(at(-1), 5.0, None), (at(1), 7.0, None), (at(2), 9.0, None)])
    np.testing.assert_array_equal(hourly.hourly_flow(table, hours), [np.nan, 7.0])


# --- storms --------------------------------------------------------------------------


def test_storms_split_on_long_dry_spells_and_drop_small_ones():
    mm = np.zeros(200)
    mm[10:13] = 3.0  # 9 mm
    mm[24] = 1.0  # 11 dry hours later: same storm
    mm[25] = 0.1  # drizzle inside the storm still counts toward its total
    mm[38] = 4.0  # 12 dry hours later: a new storm
    mm[39] = 4.0
    mm[100] = 5.0  # too small on its own
    assert events.find_storms(mm) == [(10, 24), (38, 39)]


def test_storms_treat_missing_rain_as_dry():
    mm = np.full(50, np.nan)
    mm[5:8] = 3.0
    assert events.find_storms(mm) == [(5, 7)]


# --- events --------------------------------------------------------------------------


def hydrograph(n: int = 300, base: float = 100.0) -> np.ndarray:
    return np.full(n, base, np.float32)


def test_event_records_the_storm_and_the_response():
    mm = np.zeros(300)
    mm[50] = 2.0  # antecedent rain, too small to be a storm
    mm[100:103] = [5.0, 10.0, 5.0]
    cfs = hydrograph()
    cfs[103:110] = np.linspace(200, 500, 7)  # peaks at hour 109, then recedes
    cfs[110:130] = np.linspace(400, 110, 20)

    [event] = events.extract_events(cfs, mm, area_sqmi=10.0)
    assert (event.start, event.end) == (100, 102)
    assert event.rain_mm == 20.0
    assert event.max_1h_mm == 10.0
    assert event.max_3h_mm == 20.0
    assert event.centroid_h == 1.0
    assert event.q0 == 100.0
    assert event.q0_24h_ago == 100.0
    assert event.rain_24h_mm == 0.0
    assert event.rain_7d_mm == 2.0
    assert event.api_mm == pytest.approx(2.0 * 0.9 ** ((99 - 50) / 24))
    assert (event.peak, event.qpeak) == (109, 500.0)
    assert event.peak_complete
    assert event.next_storm is None
    assert event.window_end == 102 + events.RESPONSE_H
    assert event.flow_coverage == 1.0

    excess_cfs_hours = float((cfs[100 : event.window_end + 1] - 100).sum())
    expected = excess_cfs_hours * 0.0283168 * 3600 / (10 * 2_589_988.0) * 1000
    assert event.runoff_mm == pytest.approx(expected)
    assert event.runoff_ratio == pytest.approx(expected / 20.0)


def test_next_storm_ends_the_response_window():
    mm = np.zeros(300)
    mm[100:103] = 5.0
    mm[150:153] = 5.0
    cfs = hydrograph()
    cfs[100:150] = np.linspace(100, 400, 50)  # still rising when the next storm starts

    first, second = events.extract_events(cfs, mm, area_sqmi=None)
    assert first.window_end == 149
    assert first.next_storm == 150
    assert first.peak == 149
    assert not first.peak_complete
    assert first.runoff_mm is None
    assert second.next_storm is None


def test_events_need_a_starting_flow_and_known_rain():
    mm = np.zeros(300)
    mm[100:103] = 5.0
    mm[200:203] = 5.0
    cfs = hydrograph()
    cfs[96:100] = np.nan  # no flow in the 4 hours before the first storm
    mm[190] = np.nan  # rain unknown in the week before the second
    assert events.extract_events(cfs, mm, area_sqmi=1.0) == []

    cfs[96] = 100.0  # a reading 3 hours before is close enough
    [event] = events.extract_events(cfs, mm, area_sqmi=1.0)
    assert event.start == 100


def test_events_need_most_of_the_response_window():
    mm = np.zeros(300)
    mm[100:103] = 5.0
    cfs = hydrograph()
    cfs[120:200] = np.nan
    assert events.extract_events(cfs, mm, area_sqmi=1.0) == []


# --- jobs ----------------------------------------------------------------------------


def add_gauge(session, site: str) -> int:
    gauge = Gauge(source="USGS", source_id=site, name=site)
    session.add(gauge)
    session.flush()
    session.add(
        Basin(
            gauge_id=gauge.id,
            geom=func.ST_GeomFromText("MULTIPOLYGON(((-81 38, -80 38, -80 39, -81 38)))", 4326),
            area_sqmi=10.0,
            source="test",
            fetched_at=T0,
        )
    )
    session.commit()
    return gauge.id


def test_hourly_tables_join_flow_with_history_and_live_rain(session, tmp_path):
    live_start = datetime(2025, 1, 1, tzinfo=UTC)
    now = datetime(2025, 1, 1, 3, 30, tzinfo=UTC)
    gauge_id = add_gauge(session, "01234567")
    add_gauge(session, "07654321")  # no flow history yet

    usgs_jobs._write_history(
        usgs_jobs.history_path(tmp_path, "cfs", "01234567", 2024),
        [(datetime(2024, 12, 31, 23, 45, tzinfo=UTC), 50.0, True, None)],
    )
    usgs_jobs._write_history(
        usgs_jobs.history_path(tmp_path, "cfs", "01234567", 2025),
        [(datetime(2025, 1, 1, 2, tzinfo=UTC), 60.0, False, None)],
    )
    aorc = pa.table(
        {
            "site": ["01234567", "01234567", "99999999"],
            "ts": [datetime(2024, 12, 31, 23, tzinfo=UTC), live_start, live_start],
            "mm": [1.5, 9.0, 9.0],  # AORC values from the live period are ignored
        },
        schema=rain_jobs.RAIN_SCHEMA,
    )
    write_parquet(rain_jobs.rain_path(tmp_path, 2024), aorc)
    session.add_all(
        [
            BasinRain(gauge_id=gauge_id, source="stage4", ts=live_start, mm=2.5),
            BasinRain(gauge_id=gauge_id, source="stage4", ts=now - timedelta(hours=1.5), mm=None),
        ]
    )
    session.commit()

    stats = jobs.build_hourly(
        session, data_dir=tmp_path, start_year=2024, live_rain_start=live_start, now=now
    )
    assert (stats.gauges, stats.no_flow) == (1, 1)
    table = pq.read_table(jobs.hourly_path(tmp_path, "01234567")).cast(hourly.HOURLY_SCHEMA)
    assert table.num_rows == 366 * 24 + 3  # 2024-01-01 01:00 through 2025-01-01 03:00
    last = table.slice(table.num_rows - 5).to_pydict()
    assert last["ts"][0] == datetime(2024, 12, 31, 23, tzinfo=UTC)
    np.testing.assert_array_equal(last["mm"], [1.5, 2.5, np.nan, np.nan, np.nan])
    np.testing.assert_array_equal(last["cfs"], [np.nan, 50.0, np.nan, 60.0, np.nan])


def test_extract_writes_every_gauges_events(session, tmp_path):
    add_gauge(session, "01234567")
    hours = hourly.hour_ends(T0, at(299))
    mm = np.zeros(300, np.float32)
    mm[100:103] = 5.0
    cfs = hydrograph()
    cfs[103:110] = 300.0
    write_parquet(jobs.hourly_path(tmp_path, "01234567"), hourly.to_table(hours, cfs, mm))
    write_parquet(jobs.hourly_path(tmp_path, "07654321"), hourly.to_table(hours, cfs, mm * 0))

    stats = jobs.extract(session, data_dir=tmp_path)
    assert (stats.gauges, stats.events) == (2, 1)
    [row] = pq.read_table(jobs.events_path(tmp_path)).to_pylist()
    assert row["site"] == "01234567"
    assert row["start"] == at(100)
    assert row["end"] == at(102)
    assert row["peak"] == at(103)
    assert row["duration_h"] == 3
    assert row["t_peak_h"] == 3
    assert row["lag_h"] == 2.0
    assert row["next_storm_h"] is None
    assert row["runoff_ratio"] == pytest.approx(row["runoff_mm"] / 15.0)
