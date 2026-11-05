"""Each gauge's trained models, loaded from the local data lake on first use (DESIGN.md §7).

The analog library is the gauge's whole storm history (data/events) with the hourly flow
after each storm (data/hourly) and its recession curve; ratings for reaches in feet come
from data/ratings (jobs/ratings.py). Each is cached until its files change, so rebuilt
tables take effect without restarting the API.
"""

import json
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from riverrat.features import hourly
from riverrat.forecast import estimate
from riverrat.forecast.rating import Rating
from riverrat.jobs import events as event_jobs
from riverrat.jobs.ratings import ratings_path
from riverrat.models import analog
from riverrat.models.recession import Recession


def library(data_dir: Path, site: str, area_sqmi: float | None) -> analog.AnalogLibrary | None:
    """None when the gauge has no tables or too few past storms to match against."""
    hourly_file = event_jobs.hourly_path(data_dir, site)
    events_file = event_jobs.events_path(data_dir)
    if not (hourly_file.exists() and events_file.exists()):
        return None
    mtimes = (_mtime(hourly_file), _mtime(events_file))
    return _library(hourly_file, events_file, site, area_sqmi, *mtimes)


@lru_cache(maxsize=64)
def _library(hourly_file, events_file, site, area_sqmi, *_mtimes) -> analog.AnalogLibrary | None:
    table = pq.read_table(hourly_file).cast(hourly.HOURLY_SCHEMA)
    hours = table["ts"].to_numpy().astype("datetime64[s]")
    cfs = table["cfs"].to_numpy().astype(float)
    mm = table["mm"].to_numpy().astype(float)
    rows = pq.read_table(events_file, filters=[("site", "==", site)]).to_pylist()
    found = analog.AnalogLibrary(rows, hours, cfs, Recession.fit(cfs, mm), area_sqmi)
    return found if len(found) >= analog.K else None


def library_before(
    data_dir: Path,
    site: str,
    area_sqmi: float | None,
    before: datetime,
    recession: Recession | None = None,
) -> analog.AnalogLibrary | None:
    """As `library`, as it could have been at `before`: only storms whose whole window
    had passed, and a recession fitted on flows up to then (or `recession`, fitted
    earlier still, which saves refitting for each day of a replay)."""
    hourly_file = event_jobs.hourly_path(data_dir, site)
    events_file = event_jobs.events_path(data_dir)
    if not (hourly_file.exists() and events_file.exists()):
        return None
    hours, cfs, mm, rows = _tables(hourly_file, events_file, site, _mtime(hourly_file))
    cutoff = np.datetime64(before.astimezone(UTC).replace(tzinfo=None), "s")
    seen = hours <= cutoff
    rows = [r for r in rows if r["start"] + timedelta(hours=r["window_h"]) <= before]
    if recession is None:
        recession = Recession.fit(cfs[seen], mm[seen])
    found = analog.AnalogLibrary(rows, hours, cfs, recession, area_sqmi)
    return found if len(found) >= analog.K else None


@lru_cache(maxsize=4)
def _tables(hourly_file: Path, events_file: Path, site: str, _mtime: float):
    table = pq.read_table(hourly_file).cast(hourly.HOURLY_SCHEMA)
    rows = pq.read_table(events_file, filters=[("site", "==", site)]).to_pylist()
    return (
        table["ts"].to_numpy().astype("datetime64[s]"),
        table["cfs"].to_numpy().astype(float),
        table["mm"].to_numpy().astype(float),
        rows,
    )


HISTORY_YEARS = 10
MIN_HOURS_PER_DAY = 18


def daily_range(data_dir: Path, site: str) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """The gauge's daily lowest and highest hourly flow (cfs) over its last
    HISTORY_YEARS years, for days with at least MIN_HOURS_PER_DAY hours of readings, and
    each day's month (1–12)."""
    hourly_file = event_jobs.hourly_path(data_dir, site)
    if not hourly_file.exists():
        return None
    return _daily_range(hourly_file, _mtime(hourly_file))


@lru_cache(maxsize=512)
def _daily_range(hourly_file: Path, _mtime: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    table = pq.read_table(hourly_file, columns=["ts", "cfs"])
    cfs = table["cfs"].to_numpy().astype(float)
    whole = (len(cfs) // 24) * 24
    days = cfs[-whole:].reshape(-1, 24)[-365 * HISTORY_YEARS :]
    # each day's middle hour gives its month
    middles = table["ts"].to_numpy()[-whole:].reshape(-1, 24)[-365 * HISTORY_YEARS :, 12]
    months = middles.astype("datetime64[M]").astype(int) % 12 + 1
    seen = np.isfinite(days).sum(axis=1) >= MIN_HOURS_PER_DAY
    with np.errstate(all="ignore"):
        return np.nanmin(days[seen], axis=1), np.nanmax(days[seen], axis=1), months[seen]


def rating(data_dir: Path, site: str) -> Rating | None:
    path = ratings_path(data_dir)
    if not path.exists():
        return None
    return _ratings(path, _mtime(path)).get(site)


@lru_cache(maxsize=2)
def _ratings(path: Path, _mtime: float) -> dict[str, Rating]:
    return {site: Rating.from_dict(data) for site, data in json.loads(path.read_text()).items()}


def range_model(data_dir: Path) -> estimate.RangeModel | None:
    """The fitted range model for reaches without a gauge (jobs/estimates.py)."""
    path = estimate.range_model_path(data_dir)
    if not path.exists():
        return None
    return _range_model(path, _mtime(path))


@lru_cache(maxsize=2)
def _range_model(path: Path, _mtime: float) -> estimate.RangeModel:
    return estimate.load(path)


def _mtime(path: Path) -> float:
    return path.stat().st_mtime
