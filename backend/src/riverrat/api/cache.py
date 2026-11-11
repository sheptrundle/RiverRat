"""Whole responses kept in memory between data updates.

The Outlook, the reach list, the map, and the Track record change at most hourly but took
2–15 s to build on the server (thousands of rows read and serialized per request). Each
is built once, kept as JSON and gzip bytes, and rebuilt when its `version`, a cheap query
that changes when the data does (e.g. the Outlook's latest computed_at), changes.

`warm_forever` (started with the API, one thread per worker process) checks the versions
every WARM_INTERVAL_S and rebuilds in the background, so visitors are rarely the ones who
wait; a request whose version moved before the warmer noticed rebuilds it itself.
"""

import gzip
import logging
import threading
from collections.abc import Callable, Hashable
from dataclasses import dataclass

from fastapi import Request, Response
from pydantic import BaseModel, TypeAdapter
from sqlalchemy.orm import Session

from riverrat.db.session import get_engine

log = logging.getLogger(__name__)

WARM_INTERVAL_S = 30.0
GZIP_LEVEL = 6


@dataclass(frozen=True)
class Cached:
    key: str
    version: Callable[[Session], Hashable]
    build: Callable[[Session], object]  # a response model (or a list of them)


@dataclass(frozen=True)
class _Entry:
    version: Hashable
    body: bytes
    gzipped: bytes


_registry: dict[str, Cached] = {}
_entries: dict[str, _Entry] = {}
_locks: dict[str, threading.Lock] = {}


def register(key: str, version, build) -> Cached:
    cached = Cached(key, version, build)
    _registry[key] = cached
    _locks[key] = threading.Lock()
    return cached


def clear() -> None:
    _entries.clear()


def _json(value: object) -> bytes:
    if isinstance(value, BaseModel):
        return value.model_dump_json().encode()
    return TypeAdapter(type(value)).dump_json(value)


def _refresh(cached: Cached, session: Session) -> _Entry:
    version = cached.version(session)
    entry = _entries.get(cached.key)
    if entry is not None and entry.version == version:
        return entry
    with _locks[cached.key]:  # one build at a time; later callers take its result
        entry = _entries.get(cached.key)
        if entry is not None and entry.version == version:
            return entry
        body = _json(cached.build(session))
        entry = _Entry(version, body, gzip.compress(body, GZIP_LEVEL))
        _entries[cached.key] = entry
        return entry


def respond(cached: Cached, request: Request, session: Session) -> Response:
    entry = _refresh(cached, session)
    if "gzip" in request.headers.get("accept-encoding", ""):
        # GZipMiddleware leaves a response that already has a Content-Encoding alone.
        return Response(
            entry.gzipped,
            media_type="application/json",
            headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"},
        )
    return Response(entry.body, media_type="application/json")


def warm_once() -> None:
    for cached in list(_registry.values()):
        try:
            with Session(get_engine()) as session:
                _refresh(cached, session)
        except Exception:
            log.exception("Rebuilding the cached %s failed", cached.key)


def warm_forever(stop: threading.Event) -> None:
    while not stop.is_set():
        warm_once()
        stop.wait(WARM_INTERVAL_S)


def start_warming() -> Callable[[], None]:
    stop = threading.Event()
    threading.Thread(target=warm_forever, args=(stop,), name="cache-warmer", daemon=True).start()
    return stop.set
