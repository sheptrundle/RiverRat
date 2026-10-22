"""Each gauge's trained models, loaded from the local data lake on first use (DESIGN.md §7).

The analog library is the gauge's whole storm history (data/events) with the hourly flow
after each storm (data/hourly) and its recession curve; ratings for reaches in feet come
from data/ratings (jobs/ratings.py). Each is cached until its files change, so rebuilt
tables take effect without restarting the API.
"""

import json
from functools import lru_cache
from pathlib import Path

import pyarrow.parquet as pq

from riverrat.features import hourly
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


def rating(data_dir: Path, site: str) -> Rating | None:
    path = ratings_path(data_dir)
    if not path.exists():
        return None
    return _ratings(path, _mtime(path)).get(site)


@lru_cache(maxsize=2)
def _ratings(path: Path, _mtime: float) -> dict[str, Rating]:
    return {site: Rating.from_dict(data) for site, data in json.loads(path.read_text()).items()}


def _mtime(path: Path) -> float:
    return path.stat().st_mtime
