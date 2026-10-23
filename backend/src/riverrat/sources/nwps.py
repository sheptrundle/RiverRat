"""NOAA National Water Prediction Service API (api.water.noaa.gov/nwps/v1), DESIGN.md §4.2.

Serves official River Forecast Center (RFC) forecasts for NWS forecast points and
National Water Model (NWM) forecasts for any NHDPlus reach. It keeps no history, so we
archive what it serves. As of 2026-10-18 its NWM medium/long-range series come back
empty (the model output exists on AWS); short range (hourly, 18 h) works.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

BASE_URL = "https://api.water.noaa.gov/nwps/v1"
MISSING = -999

_FLOW_FACTORS = {"kcfs": 1000.0, "cfs": 1.0}


def gauge_url(identifier: str) -> str:
    """Accepts an NWS location ID (e.g. "BVAW2") or a USGS site number."""
    return f"{BASE_URL}/gauges/{identifier}"


def rfc_forecast_url(lid: str) -> str:
    return f"{BASE_URL}/gauges/{lid}/stageflow/forecast"


def nwm_url(reach_id: int, series: str = "short_range") -> str:
    return f"{BASE_URL}/reaches/{reach_id}/streamflow?series={series}"


@dataclass(frozen=True)
class NwpsGauge:
    lid: str
    reach_id: int | None
    has_rfc_forecast: bool
    usgs_id: str | None = None  # the USGS gauge at the same place, if any


@dataclass(frozen=True)
class Forecast:
    issued_at: datetime
    # (valid time, flow in cfs or None, stage in ft or None)
    points: list[tuple[datetime, float | None, float | None]]


def parse_gauge(body: dict[str, Any]) -> NwpsGauge:
    reach = body.get("reachId")
    return NwpsGauge(
        lid=body["lid"],
        reach_id=int(reach) if reach else None,
        has_rfc_forecast=bool((body.get("pedts") or {}).get("forecast")),
        usgs_id=body.get("usgsId") or None,
    )


def parse_rfc_forecast(body: dict[str, Any]) -> Forecast | None:
    """Official forecast: primary is stage (ft), secondary is flow (usually kcfs)."""
    if not body.get("issuedTime") or not body.get("data"):
        return None
    flow_factor = _FLOW_FACTORS.get(body.get("secondaryUnits", ""))
    stage_is_ft = body.get("primaryUnits") == "ft"
    points = []
    for point in body["data"]:
        flow = _value(point.get("secondary"))
        stage = _value(point.get("primary"))
        points.append(
            (
                _time(point["validTime"]),
                flow * flow_factor if flow is not None and flow_factor else None,
                stage if stage_is_ft else None,
            )
        )
    return Forecast(issued_at=_time(body["issuedTime"]), points=points)


def parse_nwm(body: dict[str, Any], key: str = "shortRange") -> Forecast | None:
    series = (body.get(key) or {}).get("series") or {}
    if not series.get("referenceTime") or not series.get("data"):
        return None
    return Forecast(
        issued_at=_time(series["referenceTime"]),
        points=[(_time(p["validTime"]), _value(p.get("flow")), None) for p in series["data"]],
    )


def _value(value: Any) -> float | None:
    if value is None or value == MISSING:
        return None
    return float(value)


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
