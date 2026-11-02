"""Tennessee Valley Authority dam data (www.tva.com/RestApi), as its Lake Levels pages
use it. Locations are NWS-style IDs (OCCT1 = Ocoee No. 3 Dam; the full list is
locations.json).

- observed-data/{id}.json: the last 8 hours' average hourly discharge (cfs), labeled with
  the hour's end in Eastern time ("3 PM EST", "Noon EDT").
- predicted-data/{id}.json: TVA's forecast of each day's average outflow, 3 days.
- lake-messages/{id}.json: TVA's notice for the dam (spills, debris removal, outages).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

BASE_URL = "https://www.tva.com/RestApi"
EASTERN = ZoneInfo("America/New_York")
_OFFSETS = {"EST": -5, "EDT": -4}


def observed_url(location: str) -> str:
    return f"{BASE_URL}/observed-data/{location}.json"


def predicted_url(location: str) -> str:
    return f"{BASE_URL}/predicted-data/{location}.json"


def messages_url(location: str) -> str:
    return f"{BASE_URL}/lake-messages/{location}.json"


@dataclass(frozen=True)
class Notice:
    since: datetime
    message: str


def parse_observed(rows: list[dict[str, Any]]) -> list[tuple[datetime, float]]:
    """(end of the hour, average discharge in cfs) for each hour reported."""
    out = []
    for row in rows:
        value = _number(row.get("AverageHourlyDischarge"))
        at = _time(row.get("Day"), row.get("Time"))
        if value is not None and at is not None:
            out.append((at, value))
    return out


def parse_predicted(rows: list[dict[str, Any]]) -> list[tuple[date, float]]:
    """(day, forecast average outflow in cfs)."""
    out = []
    for row in rows:
        value = _number(row.get("AverageOutflow"))
        if value is not None and row.get("Day"):
            out.append((datetime.strptime(row["Day"], "%m/%d/%Y").date(), value))
    return out


def parse_notice(rows: list[dict[str, Any]]) -> Notice | None:
    """The dam's current notice, or None when it's blank."""
    for row in rows:
        message = (row.get("Message") or "").strip()
        since = _stamp(row.get("EffectiveSince"))
        if message and since is not None:
            return Notice(since, message)
    return None


def day_span(day: date) -> tuple[datetime, datetime]:
    """Eastern midnight to the next midnight."""
    start = datetime(day.year, day.month, day.day, tzinfo=EASTERN)
    return start, start + timedelta(days=1)


def _number(text: Any) -> float | None:
    try:
        return float(str(text).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _time(day: str | None, label: str | None) -> datetime | None:
    """ "11/02/2026" + "3 PM EST" (or "Noon EST", "Midnight EST") → a UTC-aware time."""
    if not day or not label:
        return None
    try:
        clock, zone = label.rsplit(" ", 1)
        base = datetime.strptime(day, "%m/%d/%Y")
        if clock == "Noon":
            hour = 12
        elif clock == "Midnight":
            hour = 24  # the end of that day
        else:
            hour = datetime.strptime(clock, "%I %p").hour
        offset = timedelta(hours=_OFFSETS[zone])
    except (KeyError, ValueError):
        return None
    return (base + timedelta(hours=hour) - offset).replace(tzinfo=UTC)


def _stamp(text: str | None) -> datetime | None:
    """ "11/02/2026 03:15 PM EST" → a UTC-aware time."""
    if not text:
        return None
    try:
        clock, zone = text.rsplit(" ", 1)
        local = datetime.strptime(clock, "%m/%d/%Y %I:%M %p")
        return (local - timedelta(hours=_OFFSETS[zone])).replace(tzinfo=UTC)
    except (KeyError, ValueError):
        return None
