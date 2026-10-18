"""Sync the American Whitewater reach catalog for the pilot states (DESIGN.md §10).

One request fetches AW's river index (every reach's name, section, class, and states).
Then each reach whose details are missing or older than `aw_refresh_days` gets its own
page fetched, one request every `aw_request_interval_s` seconds. Each reach is committed
as soon as it's parsed, so an interrupted run picks up where it left off.

    uv run python -m riverrat.jobs.sync_reaches                  # index + stale details
    uv run python -m riverrat.jobs.sync_reaches --index-only
    uv run python -m riverrat.jobs.sync_reaches --states WV --limit 20
    uv run python -m riverrat.jobs.sync_reaches --reach 2378 --reach 2379
"""

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.robotparser import RobotFileParser

import httpx2
from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from riverrat.config import get_settings
from riverrat.db.models import Gauge, Reach, ReachGauge
from riverrat.db.session import get_engine
from riverrat.http import PoliteClient
from riverrat.sources import aw

log = logging.getLogger(__name__)


@dataclass
class SyncStats:
    listed: int = 0
    scraped: int = 0
    failed: int = 0


def sync(
    client: PoliteClient,
    session: Session,
    *,
    user_agent: str,
    state_codes: set[str],
    max_age: timedelta,
    limit: int | None = None,
    aw_ids: list[int] | None = None,
    index_only: bool = False,
) -> SyncStats:
    stats = SyncStats()
    _check_robots(client, user_agent)

    if not aw_ids:
        stubs = aw.parse_index(client.get(aw.INDEX_URL).text)
        pilot = [s for s in stubs if state_codes & set(s.states)]
        _upsert_stubs(session, pilot, _now())
        session.commit()
        stats.listed = len(pilot)
        log.info("Index: %d reaches total, %d in %s", len(stubs), len(pilot), sorted(state_codes))
        if index_only:
            return stats

    todo = aw_ids or _stale_reaches(session, state_codes, max_age, limit)
    log.info("Fetching details for %d reaches", len(todo))
    for n, aw_id in enumerate(todo, 1):
        try:
            detail = aw.parse_reach_page(client.get(aw.reach_url(aw_id)).text, aw_id)
            _save_detail(session, detail, _now())
            session.commit()
        except (aw.AWParseError, httpx2.HTTPError) as exc:
            session.rollback()
            stats.failed += 1
            log.warning("Reach %d failed: %s", aw_id, exc)
            continue
        stats.scraped += 1
        if n % 25 == 0:
            log.info("%d/%d reaches done", n, len(todo))
    return stats


def _now() -> datetime:
    return datetime.now(UTC)


def _check_robots(client: PoliteClient, user_agent: str) -> None:
    try:
        robots = client.get(f"{aw.BASE_URL}/robots.txt").text
    except httpx2.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return  # no robots.txt: nothing is disallowed
        raise
    parser = RobotFileParser()
    parser.parse(robots.splitlines())
    for url in (aw.INDEX_URL, aw.reach_url(1)):
        if not parser.can_fetch(user_agent, url):
            raise PermissionError(f"robots.txt now disallows {url}; not scraping")


def _class_columns(raw: str | None) -> dict[str, str | None]:
    try:
        difficulty = aw.parse_difficulty(raw)
    except aw.AWParseError as exc:
        log.warning("%s", exc)
        difficulty = None
    return {
        "class_low": difficulty.low if difficulty else None,
        "class_high": difficulty.high if difficulty else None,
        "class_standout": difficulty.standout if difficulty else None,
    }


def _stub_columns(stub: aw.ReachStub) -> dict:
    return {
        "river": stub.river,
        "section": stub.section,
        "altname": stub.altname,
        "difficulty_raw": stub.difficulty,
        "states": stub.states,
        **_class_columns(stub.difficulty),
    }


def _upsert_stubs(session: Session, stubs: list[aw.ReachStub], now: datetime) -> None:
    if not stubs:
        return
    rows = [{"aw_id": s.id, "source": "aw", "listed_at": now, **_stub_columns(s)} for s in stubs]
    stmt = insert(Reach).values(rows)
    updated = [*_stub_columns(stubs[0]), "listed_at"]
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Reach.aw_id],
            set_={col: stmt.excluded[col] for col in updated},
            where=Reach.source == "aw",
        )
    )


def _stale_reaches(
    session: Session, state_codes: set[str], max_age: timedelta, limit: int | None
) -> list[int]:
    query = (
        select(Reach.aw_id)
        .where(
            Reach.source == "aw",
            Reach.states.overlap(sorted(state_codes)),
            or_(Reach.scraped_at.is_(None), Reach.scraped_at < _now() - max_age),
        )
        .order_by(Reach.scraped_at.asc().nulls_first(), Reach.aw_id)
        .limit(limit)
    )
    return list(session.scalars(query))


def _save_detail(session: Session, detail: aw.ReachDetail, now: datetime) -> None:
    geom = (
        func.ST_SetSRID(func.ST_GeomFromGeoJSON(json.dumps(detail.geometry)), 4326)
        if detail.geometry
        else None
    )
    values = {
        **_stub_columns(detail.stub),
        "length_mi": detail.length_mi,
        "gradient_fpm": detail.gradient_fpm,
        "geom": geom,
        "next_release": detail.next_release,
        "aw_updated_at": detail.aw_updated_at,
        "scraped_at": now,
    }
    stmt = insert(Reach).values(aw_id=detail.stub.id, source="aw", **values)
    reach_id = session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Reach.aw_id], set_=values, where=Reach.source == "aw"
        ).returning(Reach.id)
    ).scalar_one_or_none()
    if reach_id is None:
        log.info("Reach %d is maintained manually; leaving it alone", detail.stub.id)
        return

    # AW is the source of truth for its gauge links, so replace them wholesale.
    session.execute(delete(ReachGauge).where(ReachGauge.reach_id == reach_id))
    linked: set[int] = set()
    for link in detail.gauges:
        gauge_stmt = insert(Gauge).values(
            source=link.source, source_id=link.source_id, name=link.name
        )
        gauge_id = session.execute(
            gauge_stmt.on_conflict_do_update(
                index_elements=[Gauge.source, Gauge.source_id],
                set_={"name": gauge_stmt.excluded.name},
            ).returning(Gauge.id)
        ).scalar_one()
        if gauge_id in linked:
            log.warning("Reach %d lists gauge %s twice; keeping the first", detail.stub.id, link)
            continue
        linked.add(gauge_id)
        session.add(
            ReachGauge(
                reach_id=reach_id,
                gauge_id=gauge_id,
                is_primary=link.is_primary,
                metric=link.range.metric if link.range else None,
                range_min=link.range.min if link.range else None,
                range_max=link.range.max if link.range else None,
                aw_status=link.aw_status,
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--states", help="Comma-separated, e.g. WV,VA (default: pilot states)")
    parser.add_argument("--reach", type=int, action="append", help="Only this AW reach ID")
    parser.add_argument("--limit", type=int, help="Fetch at most this many reach pages")
    parser.add_argument("--index-only", action="store_true", help="Skip reach pages")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    settings = get_settings()
    states = args.states.upper().split(",") if args.states else list(settings.pilot_states)
    unknown = [s for s in states if s not in aw.AW_STATE_CODES]
    if unknown:
        parser.error(f"no AW state code mapping for {unknown}; add it to aw.AW_STATE_CODES")

    user_agent = f"RiverRat/0.1 (+{settings.scraper_contact})"
    with (
        PoliteClient(user_agent, settings.aw_request_interval_s) as client,
        Session(get_engine()) as session,
    ):
        stats = sync(
            client,
            session,
            user_agent=user_agent,
            state_codes={aw.AW_STATE_CODES[s] for s in states},
            max_age=timedelta(days=settings.aw_refresh_days),
            limit=args.limit,
            aw_ids=args.reach,
            index_only=args.index_only,
        )
    log.info("Done: %d listed, %d scraped, %d failed", stats.listed, stats.scraped, stats.failed)
    return 1 if stats.failed and not stats.scraped else 0


if __name__ == "__main__":
    raise SystemExit(main())
