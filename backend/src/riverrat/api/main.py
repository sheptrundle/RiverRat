"""Read-only HTTP API for outlooks and forecasts (DESIGN.md §9)."""

from typing import Annotated, Any

from fastapi import Depends, FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from riverrat.api import outlook, reaches
from riverrat.db.session import get_engine

app = FastAPI(title="River Rat API", version="0.1.0")
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.include_router(reaches.router)
app.include_router(outlook.router)


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
