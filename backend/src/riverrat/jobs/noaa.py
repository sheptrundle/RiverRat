"""NOAA forecast archiving (DESIGN.md §4.2, §6.7).

    uv run python -m riverrat.jobs.noaa link      # map gauges to NWS IDs and NWM reaches
    uv run python -m riverrat.jobs.noaa archive   # store any forecast issuances we lack

NWPS serves only the latest forecasts, so `archive` should run every few hours; every
issuance it misses is gone for good. Official RFC forecasts exist only for NWS forecast
points; NWM short-range forecasts exist for every reach.
"""

import argparse
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx2
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from riverrat.config import get_settings
from riverrat.db.models import Gauge, NoaaForecast
from riverrat.db.session import get_engine
from riverrat.http import PoliteClient
from riverrat.sources import nldi, nwps

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


# --- link ----------------------------------------------------------------------------


@dataclass
class LinkStats:
    gauges: int = 0
    forecast_points: int = 0
    with_reach: int = 0
    failed: int = 0


def link_gauges(
    client: PoliteClient, session: Session, *, max_age: timedelta = timedelta(days=30)
) -> LinkStats:
    """Look up each USGS gauge in NWPS (NWS location ID, NWM reach, whether the RFC issues
    forecasts). Gauges NWPS doesn't know get their NWM reach from USGS NLDI."""
    stats = LinkStats()
    gauges = session.execute(
        select(Gauge.id, Gauge.source_id).where(
            Gauge.source == "USGS",
            or_(Gauge.noaa_linked_at.is_(None), Gauge.noaa_linked_at < _now() - max_age),
        )
    ).all()
    session.rollback()  # no transaction open during the network calls below

    for gauge_id, number in gauges:
        try:
            found = _nwps_gauge(client, number)
            reach_id = found.reach_id if found else None
            if reach_id is None:
                reach_id = _nldi_comid(client, number)
        except httpx2.HTTPError as exc:
            stats.failed += 1
            log.warning("Linking gauge %s failed: %s", number, exc)
            continue
        session.execute(
            update(Gauge)
            .where(Gauge.id == gauge_id)
            .values(
                nws_lid=found.lid if found else None,
                nwm_reach_id=reach_id,
                has_rfc_forecast=bool(found and found.has_rfc_forecast),
                noaa_linked_at=_now(),
            )
        )
        session.commit()
        stats.gauges += 1
        stats.forecast_points += bool(found and found.has_rfc_forecast)
        stats.with_reach += reach_id is not None
    return stats


def _get_json_or_none(client: PoliteClient, url: str) -> dict | None:
    """The JSON body, or None if the service doesn't know this ID (404)."""
    try:
        return client.get(url).json()
    except httpx2.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise


def _nwps_gauge(client: PoliteClient, number: str) -> nwps.NwpsGauge | None:
    body = _get_json_or_none(client, nwps.gauge_url(number))
    return nwps.parse_gauge(body) if body else None


def _nldi_comid(client: PoliteClient, number: str) -> int | None:
    body = _get_json_or_none(client, nldi.usgs_site_url(number))
    return nldi.parse_comid(body) if body else None


# --- archive -------------------------------------------------------------------------


@dataclass
class ArchiveStats:
    rfc_new: int = 0
    nwm_new: int = 0
    unchanged: int = 0
    empty: int = 0
    failed: int = 0


def archive(client: PoliteClient, session: Session) -> ArchiveStats:
    stats = ArchiveStats()
    gauges = session.execute(
        select(Gauge.id, Gauge.source_id, Gauge.nws_lid, Gauge.nwm_reach_id, Gauge.has_rfc_forecast)
        .where(Gauge.source == "USGS", Gauge.noaa_linked_at.is_not(None))
        .order_by(Gauge.source_id)
    ).all()
    latest = {
        (gauge_id, source): issued
        for gauge_id, source, issued in session.execute(
            select(
                NoaaForecast.gauge_id, NoaaForecast.source, func.max(NoaaForecast.issued_at)
            ).group_by(NoaaForecast.gauge_id, NoaaForecast.source)
        )
    }
    session.rollback()  # no transaction open during the network calls below

    for gauge_id, number, lid, reach_id, has_rfc in gauges:
        jobs = []
        if has_rfc and lid:
            jobs.append(("rfc", nwps.rfc_forecast_url(lid), nwps.parse_rfc_forecast))
        if reach_id:
            jobs.append(("nwm_short", nwps.nwm_url(reach_id), nwps.parse_nwm))
        for source, url, parse in jobs:
            try:
                forecast = parse(client.get(url).json())
            except (httpx2.HTTPError, ValueError, KeyError) as exc:
                stats.failed += 1
                log.warning("%s forecast for gauge %s failed: %s", source, number, exc)
                continue
            if forecast is None:
                stats.empty += 1
            elif latest.get((gauge_id, source)) == forecast.issued_at:
                stats.unchanged += 1
            else:
                _store(session, gauge_id, source, forecast)
                session.commit()
                if source == "rfc":
                    stats.rfc_new += 1
                else:
                    stats.nwm_new += 1
    return stats


def _store(session: Session, gauge_id: int, source: str, forecast: nwps.Forecast) -> None:
    rows = [
        {
            "gauge_id": gauge_id,
            "source": source,
            "issued_at": forecast.issued_at,
            "valid_time": valid_time,
            "flow_cfs": flow,
            "stage_ft": stage,
        }
        for valid_time, flow, stage in forecast.points
    ]
    session.execute(insert(NoaaForecast).values(rows).on_conflict_do_nothing())


# --- CLI -----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="NOAA forecast archiving")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("link", help="Map gauges to NWS location IDs and NWM reaches")
    sub.add_parser("archive", help="Store new RFC and NWM forecast issuances")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    settings = get_settings()
    user_agent = f"RiverRat/0.1 (+{settings.scraper_contact})"
    with (
        PoliteClient(user_agent, settings.nwps_request_interval_s) as client,
        Session(get_engine()) as session,
    ):
        if args.command == "link":
            log.info("Link: %s", link_gauges(client, session))
        else:
            log.info("Archive: %s", archive(client, session))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
