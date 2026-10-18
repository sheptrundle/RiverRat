"""USGS Water Data OGC API (api.waterdata.usgs.gov), DESIGN.md §4.2.

The legacy waterservices.usgs.gov API shuts down in Q1 2027; this is its replacement.
What the API supports, as tested on 2026-10-18:
- `monitoring-locations`, `time-series-metadata`, and `latest-continuous` accept
  comma-separated site lists.
- `continuous` (15-minute values) takes one site per request, or a bbox + time window
  for many sites at once. Pages hold up to 50,000 rows; CSV is ~6x smaller than JSON.
"""

import csv
import io
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from riverrat.http import PoliteClient

BASE_URL = "https://api.waterdata.usgs.gov/ogcapi/v0/collections"
MAX_PAGE = 50_000

# USGS parameter code -> our unit name.
PARAMS = {"00060": "cfs", "00065": "ft"}
PARAM_CODES = {unit: code for code, unit in PARAMS.items()}
INSTANTANEOUS = "00011"  # statistic code for 15-minute ("Points") series


def site_id(number: str) -> str:
    return f"USGS-{number}"


def items_url(collection: str, **params: Any) -> str:
    return f"{BASE_URL}/{collection}/items?{urlencode(params, safe=',/:')}"


def iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Site:
    number: str
    name: str
    lon: float
    lat: float
    drainage_area_sqmi: float | None
    state_code: str | None
    huc: str | None


@dataclass(frozen=True)
class Series:
    number: str
    unit: str  # "cfs" or "ft"
    begin: datetime | None
    end: datetime | None


@dataclass(frozen=True)
class Observation:
    number: str
    unit: str
    ts: datetime
    value: float
    approved: bool
    qualifier: str | None


def features(client: PoliteClient, url: str) -> Iterator[dict[str, Any]]:
    """All features of a JSON query, following `next` links across pages."""
    next_url: str | None = url
    while next_url:
        page = client.get(next_url).json()
        yield from page.get("features") or []
        next_url = next(
            (link["href"] for link in page.get("links", []) if link.get("rel") == "next"), None
        )


def parse_site(feature: dict[str, Any]) -> Site:
    props = feature["properties"]
    lon, lat = feature["geometry"]["coordinates"][:2]
    return Site(
        number=props["monitoring_location_number"],
        name=props["monitoring_location_name"],
        lon=lon,
        lat=lat,
        drainage_area_sqmi=props.get("drainage_area"),
        state_code=props.get("state_code"),
        huc=props.get("hydrologic_unit_code"),
    )


def parse_series(feature: dict[str, Any]) -> Series | None:
    """15-minute discharge/stage series; None for anything else (daily stats, other
    parameters)."""
    props = feature["properties"]
    unit = PARAMS.get(props.get("parameter_code"))
    if unit is None or props.get("statistic_id") != INSTANTANEOUS:
        return None
    return Series(
        number=props["monitoring_location_id"].removeprefix("USGS-"),
        unit=unit,
        begin=_timestamp(props.get("begin")),
        end=_timestamp(props.get("end")),
    )


def parse_observation(props: dict[str, Any]) -> Observation | None:
    unit = PARAMS.get(props.get("parameter_code"))
    value = _number(props.get("value"))
    if unit is None or value is None:
        return None
    return Observation(
        number=props["monitoring_location_id"].removeprefix("USGS-"),
        unit=unit,
        ts=datetime.fromisoformat(props["time"]),
        value=value,
        approved=props.get("approval_status") == "Approved",
        qualifier=props.get("qualifier") or None,
    )


def history(
    client: PoliteClient,
    number: str,
    unit: str,
    start: datetime,
    end: datetime,
    page_size: int = MAX_PAGE,
) -> list[tuple[datetime, float, bool, str | None]]:
    """15-minute values in [start, end) as (ts, value, approved, qualifier) rows. If a
    window fills a whole page (e.g. a 5-minute gauge), it is split in half and refetched."""
    url = items_url(
        "continuous",
        monitoring_location_id=site_id(number),
        parameter_code=PARAM_CODES[unit],
        time=f"{iso(start)}/{iso(end)}",
        f="csv",
        limit=page_size,
        skipGeometry="true",
        properties="time,value,approval_status,qualifier",
    )
    rows = parse_csv(client.get(url).text)
    if len(rows) >= page_size:
        mid = start + (end - start) / 2
        return history(client, number, unit, start, mid, page_size) + history(
            client, number, unit, mid, end, page_size
        )
    return sorted(row for row in rows if start <= row[0] < end)


def parse_csv(text: str) -> list[tuple[datetime, float, bool, str | None]]:
    rows = []
    for record in csv.DictReader(io.StringIO(text)):
        value = _number(record.get("value"))
        if value is None:
            continue
        rows.append(
            (
                datetime.fromisoformat(record["time"]),
                value,
                record.get("approval_status") == "Approved",
                record.get("qualifier") or None,
            )
        )
    return rows


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    ts = datetime.fromisoformat(value)
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)  # metadata times are UTC, unlabeled
