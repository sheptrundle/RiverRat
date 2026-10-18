from datetime import UTC, datetime, timedelta

from riverrat.jobs.scheduler import Job, run_forever


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 10, 18, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def test_jobs_run_at_startup_then_on_their_interval():
    clock = FakeClock()
    ran: list[tuple[datetime, tuple[str, ...]]] = []
    jobs = (
        Job("often", (("a", "x"),), timedelta(hours=1)),
        Job("rare", (("b", "y"), ("b", "z")), timedelta(hours=3)),
    )

    def run(step):
        ran.append((clock.now, step))
        return 0

    run_forever(jobs, run=run, now=clock, sleep=clock.sleep, iterations=4)

    hours = [
        (int((t - datetime(2026, 10, 18, tzinfo=UTC)).total_seconds() // 3600), s) for t, s in ran
    ]
    assert hours == [
        (0, ("a", "x")),
        (0, ("b", "y")),
        (0, ("b", "z")),
        (1, ("a", "x")),
        (2, ("a", "x")),
        (3, ("a", "x")),
        (3, ("b", "y")),
        (3, ("b", "z")),
    ]


def test_a_failing_step_does_not_stop_the_job_or_scheduler():
    clock = FakeClock()
    ran = []

    def run(step):
        ran.append(step)
        return 1 if step == ("noaa", "link") else 0

    job = Job("noaa-archive", (("noaa", "link"), ("noaa", "archive")), timedelta(hours=3))
    run_forever((job,), run=run, now=clock, sleep=clock.sleep, iterations=2)
    assert ran == [("noaa", "link"), ("noaa", "archive")] * 2
