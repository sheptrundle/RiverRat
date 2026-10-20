from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from riverrat.config import Settings

TEST_DATABASE = "riverrat_test"
BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def db_engine():
    """A migrated `riverrat_test` database on the configured server. Tests using it are
    skipped when Postgres isn't running (start it with `docker compose up -d db`)."""
    url = make_url(Settings().database_url)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": TEST_DATABASE}
            )
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{TEST_DATABASE}"'))
    except OperationalError:
        pytest.skip("Postgres is not running")
    finally:
        admin.dispose()

    test_url = url.set(database=TEST_DATABASE).render_as_string(hide_password=False)
    config = Config(BACKEND_DIR / "alembic.ini")
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", test_url.replace("%", "%%"))
    command.upgrade(config, "head")

    engine = create_engine(test_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(db_engine):
    tables = (
        "reach, gauge, reach_gauge, observation, noaa_forecast, basin, basin_rain, rain_hour, "
        "rain_forecast, rain_forecast_run"
    )
    with db_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    with Session(db_engine) as session:
        yield session
