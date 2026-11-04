"""Dam releases: the Army Corps' and TVA's (forecast/releases.py).

    uv run python -m riverrat.jobs.releases sync     # the worker runs this hourly

For each Corps dam: the last few days of its observed release (into `observation`), and
the Corps' current release forecast (into `noaa_forecast`, source "usace", one issue a
day: CWMS serves only the latest, which a later sync the same day overwrites). For each
TVA dam: its last 8 hours of discharge, its 3-day forecast (source "tva"), and its notice
(dam_notice). Then each AW gauge with a recipe gets its observed flow, the sum of its
parts hour by hour, so its reaches have a "now" and the Track record can score them.
"""

import argparse
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

import httpx2
import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from riverrat.config import get_settings
from riverrat.db.models import DamNotice, Gauge, NoaaForecast, Observation
from riverrat.db.session import get_engine
from riverrat.features import hourly
from riverrat.forecast.releases import DAMS, FORECAST_SOURCE, RECIPES, Dam
from riverrat.http import PoliteClient
from riverrat.sources import cwms, tva

log = logging.getLogger(__name__)

OBSERVED_DAYS = 3
FORECAST_DAYS = 15


@dataclass
class ReleaseStats:
    dams: int = 0
    readings: int = 0
    forecast_hours: int = 0
    composite_hours: int = 0
    failed: int = 0


def sync(client: PoliteClient, session: Session, now: datetime) -> ReleaseStats:
    stats = ReleaseStats()
    for dam in DAMS.values():
        gauge = dam_gauge(session, dam)
        if dam.agency == "TVA":
            try:
                _sync_tva(client, session, dam, gauge.id, now)
            except (httpx2.HTTPError, ValueError) as exc:
                log.warning("TVA for %s failed: %s", dam.name, exc)
                stats.failed += 1
                continue
            session.commit()
            stats.dams += 1
            continue
        try:
            observed = _values(
                client, dam.office, dam.observed, now - timedelta(days=OBSERVED_DAYS), now
            )
            planned = _values(
                client,
                dam.office,
                dam.forecast,
                now - timedelta(days=1),
                now + timedelta(days=FORECAST_DAYS),
            )
        except httpx2.HTTPError as exc:
            log.warning("CWMS for %s failed: %s", dam.name, exc)
            stats.failed += 1
            continue
        _store_observed(session, gauge.id, observed)
        issued = datetime.combine(now.date(), time(), UTC)
        rows = [
            {
                "gauge_id": gauge.id,
                "source": FORECAST_SOURCE,
                "issued_at": issued,
                "valid_time": t,
                "flow_cfs": v,
                "stage_ft": None,
            }
            for t, v in planned
        ]
        if rows:
            stmt = insert(NoaaForecast).values(rows)
            session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[
                        NoaaForecast.gauge_id,
                        NoaaForecast.source,
                        NoaaForecast.issued_at,
                        NoaaForecast.valid_time,
                    ],
                    set_={"flow_cfs": stmt.excluded.flow_cfs},
                )
            )
        session.commit()
        stats.dams += 1
        stats.readings += len(observed)
        stats.forecast_hours += len(rows)

    for (source, source_id), recipe in RECIPES.items():
        if not recipe.composite:  # a live gauge of its own
            continue
        calculated = session.scalar(
            select(Gauge).where(Gauge.source == source, Gauge.source_id == source_id)
        )
        if calculated is None:
            continue
        parts = [dam_gauge(session, DAMS[key]).id for key in recipe.dams]
        if recipe.natural:
            natural = session.scalar(
                select(Gauge.id).where(Gauge.source == "USGS", Gauge.source_id == recipe.natural)
            )
            if natural is None:
                continue
            if recipe.natural_live:
                office, series = recipe.natural_live
                try:
                    since = now - timedelta(days=OBSERVED_DAYS)
                    _store_observed(session, natural, _values(client, office, series, since, now))
                except httpx2.HTTPError as exc:
                    log.warning("CWMS for %s failed: %s", series, exc)
                    stats.failed += 1
            parts.append(natural)
        stats.composite_hours += _store_composite(session, calculated.id, parts, now)
        session.commit()
    return stats


def _sync_tva(client: PoliteClient, session: Session, dam: Dam, gauge_id: int, now) -> None:
    """TVA's last 8 hours of discharge, its 3-day forecast (each day's average, held
    across the day), and its notice."""
    _store_observed(
        session, gauge_id, tva.parse_observed(client.get(tva.observed_url(dam.location)).json())
    )
    issued = datetime.combine(now.date(), time(), UTC)
    rows = []
    for day, cfs in tva.parse_predicted(client.get(tva.predicted_url(dam.location)).json()):
        start, end = tva.day_span(day)
        for at in (start, end - timedelta(hours=1)):
            rows.append(
                {
                    "gauge_id": gauge_id,
                    "source": dam.forecast_source,
                    "issued_at": issued,
                    "valid_time": at,
                    "flow_cfs": cfs,
                    "stage_ft": None,
                }
            )
    if rows:
        stmt = insert(NoaaForecast).values(rows)
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=[
                    NoaaForecast.gauge_id,
                    NoaaForecast.source,
                    NoaaForecast.issued_at,
                    NoaaForecast.valid_time,
                ],
                set_={"flow_cfs": stmt.excluded.flow_cfs},
            )
        )
    notice = tva.parse_notice(client.get(tva.messages_url(dam.location)).json())
    session.execute(delete(DamNotice).where(DamNotice.gauge_id == gauge_id))
    if notice is not None:
        session.add(
            DamNotice(gauge_id=gauge_id, since=notice.since, message=notice.message, fetched_at=now)
        )


def dam_gauge(session: Session, dam: Dam) -> Gauge:
    gauge = session.scalar(
        select(Gauge).where(Gauge.source == dam.agency, Gauge.source_id == dam.source_id)
    )
    if gauge is None:
        gauge = Gauge(
            source=dam.agency,
            source_id=dam.source_id,
            name=f"{dam.name} release",
            geom=f"SRID=4326;POINT({dam.lon} {dam.lat})",
        )
        session.add(gauge)
        session.flush()
    return gauge


def _values(client: PoliteClient, office: str, name: str, begin: datetime, end: datetime):
    values, page = [], None
    while True:
        body = client.get(
            cwms.timeseries_url(office, name, begin, end, page), headers=cwms.ACCEPT
        ).json()
        values += cwms.parse_values(body)
        page = cwms.next_page(body)
        if not page:
            return values


def _store_observed(session: Session, gauge_id: int, values) -> None:
    rows = {
        t: {
            "gauge_id": gauge_id,
            "param": "cfs",
            "ts": t,
            "value": v,
            "approved": False,
            "qualifier": None,
        }
        for t, v in values
    }
    if not rows:
        return
    stmt = insert(Observation).values(list(rows.values()))
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Observation.gauge_id, Observation.param, Observation.ts],
            set_={"value": stmt.excluded.value},
        )
    )


def _store_composite(session: Session, gauge_id: int, parts: list[int], now: datetime) -> int:
    """The hourly sum of `parts`' readings over the last OBSERVED_DAYS, for hours that
    every part has a reading in. Returns how many hours were stored."""
    end = hourly.latest_hour(now)
    hours = hourly.hour_ends(end - timedelta(days=OBSERVED_DAYS) + hourly.HOUR, end)
    since = end - timedelta(days=OBSERVED_DAYS)
    total = np.zeros(len(hours))
    for part in parts:
        rows = session.execute(
            select(Observation.ts, Observation.value, Observation.qualifier).where(
                Observation.gauge_id == part, Observation.param == "cfs", Observation.ts > since
            )
        ).all()
        total += hourly.hourly_flow(hourly.readings_table(rows), hours, nonnegative=True)
    seen = np.isfinite(total)
    rows = [
        {
            "gauge_id": gauge_id,
            "param": "cfs",
            "ts": hour.astype("datetime64[s]").item().replace(tzinfo=UTC),
            "value": float(value),
            "approved": False,
            "qualifier": None,
        }
        for hour, value in zip(hours[seen], total[seen], strict=True)
    ]
    if rows:
        stmt = insert(Observation).values(rows)
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=[Observation.gauge_id, Observation.param, Observation.ts],
                set_={"value": stmt.excluded.value},
            )
        )
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Army Corps dam releases")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync", help="Observed releases, release forecasts, calculated gauges")
    parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    settings = get_settings()
    user_agent = f"RiverRat/0.1 (+{settings.scraper_contact})"
    with PoliteClient(user_agent, 1.0, timeout=120) as client, Session(get_engine()) as session:
        log.info("Releases: %s", sync(client, session, datetime.now(UTC)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
