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


@lru_cache
def get_settings() -> Settings:
    return Settings()
