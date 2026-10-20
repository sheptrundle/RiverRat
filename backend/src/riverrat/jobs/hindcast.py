"""Perfect-rain hindcasts (DESIGN.md §6.7 mode a): replay past storms with the rain that
actually fell and score each model's forecast of what the river did.

    uv run python -m riverrat.jobs.hindcast analog                    # 2020–22 storms
    uv run python -m riverrat.jobs.hindcast analog --test 2023 2025   # the held-out test years

Models only learn from storms up to `--train-end` (default 2019). Each scored storm is
one that peaked inside its window (features/events.py), forecast from the hour before
its rain and scored over that window, so later storms don't muddy the comparison. Scores
use log(1 + cfs), so big rivers don't drown out small creeks:

- crps: mean hourly CRPS over the window (0 is perfect)
- peak_crps / peak_error: the window's peak, as a distribution / median vs. observed
- peak_in_80: whether the observed peak fell inside the forecast's 10–90% range

Results go to data/skill/analog_{first}-{last}.parquet, one row per storm and model.
"""

import argparse
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy.orm import Session

from riverrat.config import get_settings
from riverrat.db.session import get_engine
from riverrat.features import hourly
from riverrat.jobs import events as event_jobs
from riverrat.models import analog
from riverrat.models.recession import Recession
from riverrat.storage import write_parquet

log = logging.getLogger(__name__)

MODELS = ("persistence", "no_matching", "analog")
SCORE_SCHEMA = pa.schema(
    [
        ("site", pa.string()),
        ("start", pa.timestamp("s", tz="UTC")),
        ("rain_mm", pa.float32()),
        ("model", pa.string()),
        ("crps", pa.float32()),
        ("peak_crps", pa.float32()),
        ("peak_error", pa.float32()),
        ("peak_in_80", pa.bool_()),
    ]
)


def crps(members: np.ndarray, weights: np.ndarray, observed: np.ndarray) -> np.ndarray:
    """CRPS of a weighted ensemble (members × times) against observations, per time."""
    spread_to_obs = (weights[:, None] * np.abs(members - observed[None])).sum(axis=0)
    order = np.argsort(members, axis=0)
    ranked = np.take_along_axis(members, order, axis=0)
    w = weights[order]
    below = np.cumsum(w, axis=0) - w
    # ½ Σᵢⱼ wᵢwⱼ|xᵢ − xⱼ| in O(n log n), from the sorted members
    spread = (w * ranked * (2 * below + w - 1)).sum(axis=0)
    return spread_to_obs - spread


def skill_path(data_dir: Path, first: int, last: int) -> Path:
    return data_dir / "skill" / f"analog_{first}-{last}.parquet"


@dataclass
class HindcastStats:
    gauges: int = 0
    storms: int = 0
    skipped_gauges: int = 0


def hindcast_gauge(
    site: str,
    event_rows: list[dict],
    hourly_table: pa.Table,
    area_sqmi: float | None,
    *,
    train_end: int,
    test: tuple[int, int],
) -> list[dict]:
    hours = hourly_table["ts"].to_numpy().astype("datetime64[s]")
    cfs = hourly_table["cfs"].to_numpy()
    mm = hourly_table["mm"].to_numpy()
    train_until = np.datetime64(f"{train_end + 1}-01-01T00:00:00")
    in_train = hours <= train_until
    recession = Recession.fit(cfs[in_train], mm[in_train])
    library = analog.AnalogLibrary(
        [r for r in event_rows if r["start"].year <= train_end],
        hours,
        cfs,
        recession,
        area_sqmi,
    )
    if len(library) < analog.K:
        return []

    first = int(hours[0].astype(np.int64))
    rows = []
    for event in event_rows:
        if not (test[0] <= event["start"].year <= test[1]):
            continue
        if not event["peak_complete"] or event["flow_coverage"] < analog.MIN_LIBRARY_COVERAGE:
            continue
        start = (int(event["start"].timestamp()) - first) // 3600
        n = min(event["window_h"], analog.HORIZON_H)
        observed = cfs[start : start + n].astype(float)
        seen = np.isfinite(observed)
        state = analog.State(
            q=event["q0"], api_mm=event["api_mm"], at=event["start"] - timedelta(hours=1)
        )
        storm = analog.Storm(
            rain_mm=event["rain_mm"], duration_h=event["duration_h"], max_3h_mm=event["max_3h_mm"]
        )
        forecasts = {
            "persistence": (np.full((1, n), event["q0"]), np.ones(1)),
            "no_matching": _window(
                library.forecast(state, storm, k=None, similar=False, stretch=False), n
            ),
            "analog": _window(library.forecast(state, storm), n),
        }
        log_obs = np.log1p(np.where(seen, observed, 0))
        peak = np.log1p(np.nanmax(observed))
        for model, (members, weights) in forecasts.items():
            log_members = np.log1p(members)
            peaks = log_members.max(axis=1)
            low, mid, high = analog.weighted_quantile(peaks, weights, [0.1, 0.5, 0.9])
            rows.append(
                {
                    "site": site,
                    "start": event["start"],
                    "rain_mm": event["rain_mm"],
                    "model": model,
                    "crps": float(crps(log_members, weights, log_obs)[seen].mean()),
                    "peak_crps": float(crps(peaks[:, None], weights, np.array([peak]))[0]),
                    "peak_error": float(mid - peak),
                    "peak_in_80": bool(low <= peak <= high),
                }
            )
    return rows


def _window(forecast: analog.Forecast, n: int) -> tuple[np.ndarray, np.ndarray]:
    """The forecast's hours 1…n: the storm's window, after the hour of the forecast."""
    return forecast.members[:, 1 : n + 1], forecast.weights


def run(
    *,
    data_dir: Path,
    areas: dict[str, float],
    train_end: int,
    test: tuple[int, int],
    sites: list[str] | None = None,
) -> HindcastStats:
    """`areas` are basin areas (mi²) by site number, for the runoff cap."""
    stats = HindcastStats()
    events_table = pq.read_table(event_jobs.events_path(data_dir))
    by_site: dict[str, list[dict]] = defaultdict(list)
    for row in events_table.to_pylist():
        by_site[row["site"]].append(row)
    rows: list[dict] = []
    for site in sorted(sites or by_site):
        path = event_jobs.hourly_path(data_dir, site)
        if site not in by_site or not path.exists():
            stats.skipped_gauges += 1
            continue
        table = pq.read_table(path).cast(hourly.HOURLY_SCHEMA)
        found = hindcast_gauge(
            site, by_site[site], table, areas.get(site), train_end=train_end, test=test
        )
        if not found:
            stats.skipped_gauges += 1
            continue
        rows.extend(found)
        stats.gauges += 1
        stats.storms += len(found) // len(MODELS)
    write_parquet(skill_path(data_dir, *test), pa.Table.from_pylist(rows, schema=SCORE_SCHEMA))
    _log_summary(rows)
    return stats


def _log_summary(rows: list[dict]) -> None:
    by_model: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_model[row["model"]].append(row)
    for model in MODELS:
        scored = by_model.get(model, [])
        if not scored:
            continue
        big = [r for r in scored if r["rain_mm"] >= 25.4]
        log.info(
            "%-12s crps %.3f  peak crps %.3f  peak error %+.3f (1 in+: %+.3f)  in 80%%: %.0f%%",
            model,
            np.mean([r["crps"] for r in scored]),
            np.mean([r["peak_crps"] for r in scored]),
            np.mean([r["peak_error"] for r in scored]),
            np.mean([r["peak_error"] for r in big]) if big else float("nan"),
            100 * np.mean([r["peak_in_80"] for r in scored]),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Perfect-rain hindcasts")
    sub = parser.add_subparsers(dest="command", required=True)
    m1 = sub.add_parser("analog", help="Score the analog model against baselines")
    m1.add_argument("--train-end", type=int, default=2019, help="Last year models learn from")
    m1.add_argument("--test", type=int, nargs=2, default=(2020, 2022), metavar=("FIRST", "LAST"))
    m1.add_argument("--gauge", action="append", help="Only this USGS site number")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.test[0] <= args.train_end:
        parser.error("test years must come after --train-end")
    with Session(get_engine()) as session:
        areas = event_jobs.basin_areas(session)
    stats = run(
        data_dir=get_settings().data_dir,
        areas=areas,
        train_end=args.train_end,
        test=tuple(args.test),
        sites=args.gauge,
    )
    log.info("Hindcast: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
