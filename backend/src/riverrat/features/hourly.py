"""The hourly table behind every model (DESIGN.md §6.6): one row per hour per gauge with
the gauge's flow and its basin's rain, on one shared clock.

Hours are labeled by their end (UTC), like the rain products: the row for 14:00 holds
the rain that fell from 13:00 to 14:00 and the mean of the flow readings in that hour.
"""

from datetime import UTC, datetime, timedelta
from functools import reduce

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

HOURLY_SCHEMA = pa.schema(
    [
        ("ts", pa.timestamp("s", tz="UTC")),
        ("cfs", pa.float32()),
        ("mm", pa.float32()),
    ]
)
# Readings USGS flags as affected by ice, equipment trouble, or backwater: the reported
# flow doesn't reflect the river, so they're dropped.
BAD_QUALIFIERS = ("ICE", "EQUIP", "BACKWATER")

HOUR = timedelta(hours=1)


def hour_ends(start: datetime, end: datetime) -> np.ndarray:
    """Hour-end times from `start` through `end` (both on the hour), as datetime64[s]."""
    n_hours = int((end - start) / HOUR) + 1
    return np.datetime64(start.replace(tzinfo=None), "s") + np.arange(n_hours) * np.timedelta64(
        3600, "s"
    )


def latest_hour(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def hourly_flow(readings: pa.Table, hours: np.ndarray) -> np.ndarray:
    """Mean of the good readings in each hour (NaN for hours without any). `readings` has
    the USGS history columns (ts, value, qualifier); `hours` are consecutive hour ends.

    Averaging copes with the mix of 15-, 30-, and 60-minute recording in older years."""
    flags = (pc.match_substring(readings["qualifier"], q) for q in BAD_QUALIFIERS)
    flagged = pc.fill_null(reduce(pc.or_, flags), False)
    good = readings.filter(pc.and_(pc.invert(flagged), pc.is_valid(readings["value"])))
    # Some timestamps carry stray microseconds; whole seconds are plenty.
    micros = good["ts"].cast(pa.timestamp("us", tz="UTC")).cast(pa.int64()).to_numpy()
    seconds = micros // 1_000_000
    values = good["value"].to_numpy(zero_copy_only=False)

    # A reading at 13:15 or at 14:00 belongs to the hour ending 14:00.
    first = hours[0].astype("datetime64[s]").astype(np.int64)
    slot = -((first - seconds) // 3600)  # ceil((seconds - first) / 3600)
    keep = (slot >= 0) & (slot < len(hours))
    counts = np.bincount(slot[keep], minlength=len(hours))
    sums = np.bincount(slot[keep], weights=values[keep], minlength=len(hours))
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(counts > 0, sums / counts, np.nan).astype(np.float32)


def to_table(hours: np.ndarray, cfs: np.ndarray, mm: np.ndarray) -> pa.Table:
    return pa.table(
        {
            "ts": pa.array(hours.astype("datetime64[s]"), pa.timestamp("s")).cast(
                HOURLY_SCHEMA.field("ts").type
            ),
            "cfs": pa.array(cfs, pa.float32()),
            "mm": pa.array(mm, pa.float32()),
        },
        schema=HOURLY_SCHEMA,
    )
