"""US Army Corps of Engineers CWMS Data API (cwms-data.usace.army.mil), DESIGN.md §4.2.

Corps reservoirs publish their release (outflow) every 15 minutes, and Huntington
District (LRH) also publishes a daily forecast of each dam's hourly release a week or
two ahead (the "CELRH-FCST-DAILY" series). Values are in cfs; times are epoch ms (UTC).
"""

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

BASE_URL = "https://cwms-data.usace.army.mil/cwms-data"
ACCEPT = {"Accept": "application/json;version=2"}
PAGE_SIZE = 5000


def timeseries_url(
    office: str, name: str, begin: datetime, end: datetime, page: str | None = None
) -> str:
    query = {
        "name": name,
        "office": office,
        "begin": _iso(begin),
        "end": _iso(end),
        "page-size": PAGE_SIZE,
    }
    if page:
        query["page"] = page
    return f"{BASE_URL}/timeseries?{urlencode(query)}"


def parse_values(body: dict[str, Any]) -> list[tuple[datetime, float]]:
    """(time, value) pairs, skipping missing values."""
    return [
        (datetime.fromtimestamp(ms / 1000, UTC), float(value))
        for ms, value, *_ in body.get("values") or []
        if value is not None
    ]


def next_page(body: dict[str, Any]) -> str | None:
    return body.get("next-page")


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
