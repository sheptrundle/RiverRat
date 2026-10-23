"""Run River Rat's recurring jobs (DESIGN.md §7). This is the `worker` container's command.

    uv run python -m riverrat.jobs.scheduler
    RIVERRAT_JOBS=usgs-live,outlook uv run python -m riverrat.jobs.scheduler   # a subset

Each job runs at startup and then every `every`. Steps run as subprocesses so one
crashing job can't take the scheduler down, and each step's log goes to stdout.
"""

import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Job:
    name: str
    steps: tuple[tuple[str, ...], ...]  # each step: module under riverrat.jobs + arguments
    every: timedelta


JOBS = (
    # Live gauge readings: the forecasts' starting point. A 3-hour look-back overlaps runs.
    Job("usgs-live", (("usgs", "live"),), timedelta(minutes=15)),
    # NWPS keeps only the latest forecasts; archive them every few hours (DESIGN.md §6.7).
    Job("noaa-archive", (("noaa", "link"), ("noaa", "archive")), timedelta(hours=3)),
    # Stage IV basin rain: new hours plus revisions; a long gap catches up 48 hours a run.
    Job("rain-recent", (("rain", "recent", "--max-hours", "48"),), timedelta(hours=1)),
    # NBM rain forecasts: each 00/06/12/18Z run once it's fully published (~4 h later).
    Job("rain-forecast", (("rain_forecast", "archive"),), timedelta(hours=1)),
    # Every reach's 6-day chances from the latest flows and rain forecast (~1 min).
    Job("outlook", (("outlook",),), timedelta(hours=1)),
    # Stage ratings for reaches in feet, refit as live readings shift the offset.
    Job("ratings", (("ratings",),), timedelta(days=1)),
)


def run_step(step: tuple[str, ...]) -> int:
    module, *args = step
    return subprocess.run([sys.executable, "-m", f"riverrat.jobs.{module}", *args]).returncode


def run_forever(
    jobs: tuple[Job, ...] = JOBS,
    *,
    run: Callable[[tuple[str, ...]], int] = run_step,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    iterations: int | None = None,
) -> None:
    next_run = {job.name: now() for job in jobs}
    loops = 0
    while iterations is None or loops < iterations:
        loops += 1
        for job in jobs:
            if next_run[job.name] > now():
                continue
            log.info("Running %s", job.name)
            for step in job.steps:
                code = run(step)
                if code != 0:
                    log.warning("%s: step %s exited with %d", job.name, " ".join(step), code)
            next_run[job.name] = now() + job.every
            log.info("%s done; next run at %s", job.name, next_run[job.name].isoformat())
        sleep(max(1.0, (min(next_run.values()) - now()).total_seconds()))


def select_jobs(names: str | None, jobs: tuple[Job, ...] = JOBS) -> tuple[Job, ...]:
    """The jobs named in a comma-separated list (all of them when empty). The server runs
    only the live jobs; ratings need the history that stays on the laptop."""
    if not names:
        return jobs
    wanted = {name.strip() for name in names.split(",") if name.strip()}
    unknown = wanted - {job.name for job in jobs}
    if unknown:
        raise SystemExit(f"unknown jobs: {', '.join(sorted(unknown))}")
    return tuple(job for job in jobs if job.name in wanted)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    jobs = select_jobs(os.environ.get("RIVERRAT_JOBS"))
    log.info("Jobs: %s", ", ".join(job.name for job in jobs))
    run_forever(jobs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
