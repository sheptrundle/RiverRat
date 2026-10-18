"""Runtime configuration, read from environment variables or the repo-root .env file."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Pilot region (DESIGN.md §15): Southern Appalachians plus VA/WV/MD.
PILOT_STATES = ("NC", "TN", "GA", "SC", "KY", "VA", "WV", "MD")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_REPO_ROOT / ".env", extra="ignore")

    database_url: str = "postgresql+psycopg://riverrat:riverrat@localhost:5432/riverrat"
    # Outlook horizon: today plus up to 5 days (DESIGN.md §6.3).
    outlook_max_days: int = 6
    pilot_states: tuple[str, ...] = PILOT_STATES

    # Scraping etiquette (DESIGN.md §10): identify ourselves and go slowly.
    scraper_contact: str = "https://github.com/sheptrundle/RiverRat"
    aw_request_interval_s: float = 5.0
    aw_refresh_days: int = 7

    # USGS Water Data API (DESIGN.md §4.2). The key allows 1,000 requests/hour; a 4 s
    # interval leaves headroom for live polling while a backfill runs.
    usgs_api_key: str | None = None
    usgs_request_interval_s: float = 4.0
    # Stage IV rain records start in 2002, so earlier flow history isn't useful for training.
    history_start_year: int = 2002
    observation_retention_days: int = 60
    data_dir: Path = _REPO_ROOT / "data"


@lru_cache
def get_settings() -> Settings:
    return Settings()
