"""A synthetic basin and river whose response to rain is known exactly."""

from datetime import UTC, datetime, timedelta

import numpy as np

from riverrat.features import events, hourly
from riverrat.jobs import events as event_jobs

START = datetime(2016, 1, 1, 1, tzinfo=UTC)
BASE = 50.0
DECAY = 0.05  # per hour, toward BASE


def synthetic_river(years: int = 6, seed: int = 1):
    """A basin that gets a storm every 4–8 days (5–60 mm over 3–12 h) and a river whose
    excess flow is a leaky bucket: each mm of rain adds 0.4·mm² cfs, decaying 5%/hour."""
    rng = np.random.default_rng(seed)
    n = years * 8760
    mm = np.zeros(n)
    t = 48
    while t < n - 24:
        duration = int(rng.integers(3, 13))
        mm[t : t + duration] = rng.uniform(5, 60) / duration
        t += duration + int(rng.integers(96, 192))
    cfs = np.empty(n)
    excess = 0.0
    storm_total = 0.0
    for i in range(n):
        storm_total = storm_total + mm[i] if mm[i] else 0.0
        excess = excess * (1 - DECAY) + 0.4 * (storm_total**2 - (storm_total - mm[i]) ** 2)
        cfs[i] = BASE + excess
    hours = hourly.hour_ends(START, START + timedelta(hours=n - 1))
    return hours, cfs.astype(np.float32), mm.astype(np.float32)


def event_rows(hours, cfs, mm, area=100.0) -> list[dict]:
    found = events.extract_events(cfs, mm, area)
    table = hourly.to_table(hours, cfs, mm)
    return event_jobs.event_table("01234567", table["ts"], found).to_pylist()
