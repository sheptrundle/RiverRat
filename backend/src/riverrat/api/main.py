"""Read-only HTTP API for outlooks and forecasts (DESIGN.md §9)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from riverrat.api import cache, outlook, reach_map, reaches, track_record
from riverrat.db.session import get_engine


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Build the cached responses now and whenever their data changes (api/cache.py).
    stop = cache.start_warming()
    yield
    stop()


app = FastAPI(title="River Rat API", version="0.1.0", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.include_router(reaches.router)
app.include_router(outlook.router)
app.include_router(track_record.router)
app.include_router(reach_map.router)


def database_status() -> dict[str, Any]:
    try:
        with get_engine().connect() as conn:
            postgis = conn.execute(text("SELECT postgis_lib_version()")).scalar_one()
    except SQLAlchemyError as exc:
        return {"ok": False, "error": type(exc).__name__}
    return {"ok": True, "postgis": postgis}


@app.get("/api/health")
def health(database: Annotated[dict[str, Any], Depends(database_status)]) -> dict[str, Any]:
    return {"status": "ok", "database": database}
