"""Run River Rat's recurring jobs (DESIGN.md §7). This is the `worker` container's command.

    uv run python -m riverrat.jobs.scheduler

Each job runs at startup and then every `every`. Steps run as subprocesses so one
crashing job can't take the scheduler down, and each step's log goes to stdout.
"""

import logging
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
    # NWPS keeps only the latest forecasts; archive them every few hours (DESIGN.md §6.7).
    Job("noaa-archive", (("noaa", "link"), ("noaa", "archive")), timedelta(hours=3)),
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


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
