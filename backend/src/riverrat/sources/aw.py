"""American Whitewater reach catalog, parsed from AW's public pages (DESIGN.md §10).

AW has no public API. Its pages are Next.js server-rendered and embed their data in
`self.__next_f.push([1, "..."])` script chunks. We decode that payload and pull out
**facts only**: names, class, length, gradient, geometry, gauge links, flow ranges, and
the next release date. Descriptions, photos, reports, and alerts are never extracted.
"""

import json
import re
from collections.abc import Callable, Iterator
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ValidationError, field_validator, model_validator

BASE_URL = "https://www.americanwhitewater.org"
INDEX_URL = f"{BASE_URL}/content/River/view/river-index"


def reach_url(aw_id: int) -> str:
    return f"{BASE_URL}/content/River/view/river-detail/{aw_id}/main"


# AW's state codes for the pilot region (DESIGN.md §15). Extend when adding regions.
AW_STATE_CODES = {
    "GA": "USA-GEO",
    "KY": "USA-KEN",
    "MD": "USA-MRY",
    "NC": "USA-NCR",
    "SC": "USA-SCR",
    "TN": "USA-TNN",
    "VA": "USA-VRG",
    "WV": "USA-WVR",
}

# AW uses these values to mean "no lower bound" / "no upper bound".
_NO_MIN = 0
_NO_MAX = 100_000

_METRICS = {"cfs": "cfs", "levelFT": "ft"}


class AWParseError(ValueError):
    """The page didn't contain the data we expected (or AW changed its page structure)."""


class ReachNotFound(AWParseError):
    pass


# --- Class ratings -------------------------------------------------------------------

_CLASS = r"(?:VI|V|IV|III|II|I)(?:plus|minus)?"
_DIFFICULTY = re.compile(
    rf"^(?P<low>{_CLASS})(?:to(?P<high>{_CLASS}))?(?:standout(?P<standout>{_CLASS}))?$"
)


class Difficulty(BaseModel):
    low: str
    high: str | None = None
    standout: str | None = None


def parse_difficulty(raw: str | None) -> Difficulty | None:
    """Parse AW's encoding, e.g. "IItoIIIstandoutIV" -> II to III, standout IV;
    "IIIplus" -> III+."""
    if not raw:
        return None
    match = _DIFFICULTY.match(raw)
    if match is None:
        raise AWParseError(f"unrecognized class rating {raw!r}")

    def fmt(value: str | None) -> str | None:
        return value and value.replace("plus", "+").replace("minus", "-")

    return Difficulty(
        low=fmt(match["low"]), high=fmt(match["high"]), standout=fmt(match["standout"])
    )


# --- Models --------------------------------------------------------------------------


# A few AW entries put the section number on the river ("1. Cumberland" / "Harlan to
# Wallins") where their neighbors have "Cumberland" / "2. Wallins to Varilla".
_NUMBERED_RIVER = re.compile(r"^\s*(\d+(?:\.\d+)?[.)])\s+(\S.*)$")


class ReachStub(BaseModel):
    """One entry in AW's river index."""

    id: int
    river: str
    section: str
    altname: str = ""
    difficulty: str | None = None
    states: list[str]

    @model_validator(mode="after")
    def _number_belongs_to_the_section(self) -> "ReachStub":
        match = _NUMBERED_RIVER.match(self.river)
        if match:
            number, river = match.groups()
            self.river = river.strip()
            if not self.section[:1].isdigit():
                self.section = f"{number} {self.section}".strip()
        return self


class FlowRange(BaseModel):
    metric: str  # "cfs" or "ft"; anything else is AW's raw metric name, kept so we notice
    min: float | None
    max: float | None


class GaugeLink(BaseModel):
    source: str  # "USGS", "NOAA" (NWS location ID), "CALCULATED" (AW virtual gauge), ...
    source_id: str
    name: str
    is_primary: bool
    range: FlowRange | None
    aw_status: str | None


class ReachDetail(BaseModel):
    stub: ReachStub
    length_mi: float | None
    gradient_fpm: float | None
    geometry: dict[str, Any] | None  # GeoJSON LineString / MultiLineString, lon/lat
    gauges: list[GaugeLink]
    next_release: date | None
    aw_updated_at: datetime | None

    @field_validator("geometry")
    @classmethod
    def _line_geometry(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None and value.get("type") not in {"LineString", "MultiLineString"}:
            raise ValueError(f"unexpected geometry type {value.get('type')!r}")
        return value


# --- Parsing -------------------------------------------------------------------------

_PUSH = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', re.S)
_DECODER = json.JSONDecoder()


def flight_payload(html: str) -> str:
    """Concatenate and unescape the Next.js data chunks embedded in a page."""
    chunks = _PUSH.findall(html)
    if not chunks:
        raise AWParseError("page has no Next.js data chunks")
    return "".join(json.loads(f'"{chunk}"') for chunk in chunks)


def _values(payload: str, key: str) -> Iterator[Any]:
    """Every JSON value that follows `"key":` in the payload. Next.js sometimes replaces
    repeated values with "$..." reference strings; those are skipped."""
    for match in re.finditer(re.escape(f'"{key}":'), payload):
        try:
            value, _ = _DECODER.raw_decode(payload, match.end())
        except ValueError:
            continue
        if isinstance(value, str) and value.startswith("$"):
            continue
        yield value


def _enclosing_objects(payload: str, key: str) -> Iterator[dict[str, Any]]:
    """For each occurrence of `"key":`, the innermost JSON object that contains it."""
    for match in re.finditer(re.escape(f'"{key}":'), payload):
        start = match.start()
        while (start := payload.rfind("{", 0, start)) != -1:
            try:
                value, _ = _DECODER.raw_decode(payload, start)
            except ValueError:
                continue
            if isinstance(value, dict) and key in value:
                yield value
                break


def _first(items: Iterator[Any], predicate: Callable[[Any], bool]) -> Any | None:
    return next((item for item in items if predicate(item)), None)


def parse_index(html: str) -> list[ReachStub]:
    stubs = _first(_values(flight_payload(html), "reachStubs"), lambda v: isinstance(v, list))
    if not stubs:
        raise AWParseError("river index has no reachStubs list")
    try:
        return [ReachStub.model_validate(stub) for stub in stubs]
    except ValidationError as exc:
        raise AWParseError(f"river index: {exc}") from exc


def parse_reach_page(html: str, aw_id: int) -> ReachDetail:
    payload = flight_payload(html)
    reach_key = str(aw_id)

    stub = _first(
        _values(payload, "stub"), lambda v: isinstance(v, dict) and v.get("id") == reach_key
    )
    if stub is None:
        raise ReachNotFound(f"no reach {aw_id} on page")

    sidebar = _first(
        _enclosing_objects(payload, "correlations"),
        lambda v: v.get("reachId") == reach_key,
    )
    if sidebar is None:
        raise AWParseError(f"reach {aw_id}: no gauge/summary block")

    map_props = _first(
        _enclosing_objects(payload, "geometry"),
        lambda v: (
            v.get("reachId") == reach_key
            and isinstance(v["geometry"], dict)
            and isinstance(v["geometry"].get("coordinates"), list)
        ),
    )

    release = sidebar.get("nextRelease") or {}
    try:
        return ReachDetail(
            stub=ReachStub.model_validate(stub),
            length_mi=sidebar.get("length"),
            gradient_fpm=sidebar.get("averageGradient"),
            geometry=map_props["geometry"] if map_props else None,
            gauges=[_gauge_link(c) for c in sidebar.get("correlations") or []],
            next_release=release.get("date"),
            aw_updated_at=sidebar.get("reachInfoUpdated"),
        )
    except (ValidationError, KeyError, TypeError) as exc:
        raise AWParseError(f"reach {aw_id}: {exc}") from exc


def _gauge_link(correlation: dict[str, Any]) -> GaugeLink:
    info = correlation["gaugeInfo"]
    data = (correlation.get("correlationDetails") or {}).get("data") or {}
    flow_range = None
    if data.get("metric"):
        low, high = data.get("beginLowRunnable"), data.get("endHighRunnable")
        flow_range = FlowRange(
            metric=_METRICS.get(data["metric"], data["metric"]),
            min=None if low is None or low <= _NO_MIN else low,
            max=None if high is None or high >= _NO_MAX else high,
        )
    return GaugeLink(
        source=info["source"],
        source_id=str(info["id"]),
        name=info.get("name") or "",
        is_primary=bool(correlation.get("isPrimary")),
        range=flow_range,
        aw_status=(correlation.get("status") or {}).get("status"),
    )
