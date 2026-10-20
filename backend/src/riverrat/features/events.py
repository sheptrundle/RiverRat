"""Past storms and how each gauge responded: the analog model's library (DESIGN.md §6.4
M1, §8 analog_event). "X rain at Y level → what happened", with the context that makes
two storms comparable: how wet the basin already was, the season, rain intensity.

A storm is a run of wet hours of basin rain with no dry spell of DRY_GAP_H or longer,
totaling at least MIN_STORM_MM. Its response is the flow from the first wet hour until
RESPONSE_H after the last, cut short where the next storm begins so its water isn't
credited to this one. Indices below are positions in a gauge's hourly table.
"""

from dataclasses import dataclass

import numpy as np

WET_MM = 0.25  # an hour counts as wet at this much basin rain
DRY_GAP_H = 12
MIN_STORM_MM = 6.35  # 0.25 in
RESPONSE_H = 96
# Antecedent precipitation index: yesterday's rain counts 0.9, the day before 0.81, ...
API_DAILY_DECAY = 0.9
API_DAYS = 30
# Skip storms whose starting flow is unknown or whose response window is mostly missing.
MAX_Q0_GAP_H = 3
MIN_FLOW_COVERAGE = 0.8

CFS_HOURS_TO_M3 = 0.0283168 * 3600
SQMI_TO_M2 = 2_589_988.0


@dataclass(frozen=True)
class Event:
    start: int  # first wet hour
    end: int  # last wet hour
    rain_mm: float
    max_1h_mm: float
    max_3h_mm: float
    centroid_h: float  # center of the storm's rain, hours after `start`
    q0: float  # flow in the hour before the rain began
    q0_24h_ago: float | None
    rain_24h_mm: float  # before the storm
    rain_7d_mm: float
    rain_30d_mm: float
    api_mm: float
    window_end: int  # last hour searched for the peak
    peak: int
    qpeak: float
    peak_complete: bool  # the flow peaked before the window ended
    next_storm: int | None  # start of the next storm, if inside the response window
    flow_coverage: float  # share of window hours with flow
    runoff_mm: float | None  # flow above q0 over the window, as depth over the basin

    @property
    def runoff_ratio(self) -> float | None:
        return None if self.runoff_mm is None else self.runoff_mm / self.rain_mm


def find_storms(mm: np.ndarray) -> list[tuple[int, int]]:
    """(first, last) wet hour of each storm. Missing rain counts as dry here; events
    check for it separately."""
    wet = np.flatnonzero(np.nan_to_num(mm) >= WET_MM)
    if not len(wet):
        return []
    breaks = np.flatnonzero(np.diff(wet) > DRY_GAP_H)
    starts = wet[np.concatenate([[0], breaks + 1])]
    ends = wet[np.concatenate([breaks, [len(wet) - 1]])]
    rain = np.nan_to_num(mm)
    return [
        (int(s), int(e))
        for s, e in zip(starts, ends, strict=True)
        if rain[s : e + 1].sum() >= MIN_STORM_MM
    ]


def extract_events(cfs: np.ndarray, mm: np.ndarray, area_sqmi: float | None) -> list[Event]:
    storms = find_storms(mm)
    rain = np.nan_to_num(mm)
    cumulative = np.concatenate([[0.0], np.cumsum(rain, dtype=np.float64)])
    api = _api(rain)
    events = []
    for n, (start, end) in enumerate(storms):
        history = max(0, start - API_DAYS * 24)
        if np.isnan(mm[history : end + 1]).any():
            continue  # rain unknown during the storm or the month before
        q0 = _last_value(cfs, start - 1, MAX_Q0_GAP_H)
        if q0 is None:
            continue
        next_start = storms[n + 1][0] if n + 1 < len(storms) else None
        if next_start is not None and next_start > end + RESPONSE_H:
            next_start = None  # too late to matter
        window_end = min(end + RESPONSE_H, len(cfs) - 1)
        if next_start is not None:
            window_end = min(window_end, next_start - 1)
        flow = cfs[start : window_end + 1]
        coverage = float(np.isfinite(flow).mean())
        if coverage < MIN_FLOW_COVERAGE:
            continue

        storm = rain[start : end + 1]
        peak = start + int(np.nanargmax(flow))
        events.append(
            Event(
                start=start,
                end=end,
                rain_mm=float(storm.sum()),
                max_1h_mm=float(storm.max()),
                max_3h_mm=float(np.convolve(storm, np.ones(3), "full").max()),
                centroid_h=float((storm * np.arange(len(storm))).sum() / storm.sum()),
                q0=q0,
                q0_24h_ago=_last_value(cfs, start - 25, MAX_Q0_GAP_H),
                rain_24h_mm=_before(cumulative, start, 24),
                rain_7d_mm=_before(cumulative, start, 24 * 7),
                rain_30d_mm=_before(cumulative, start, 24 * 30),
                api_mm=float(api[start - 1]) if start else 0.0,
                window_end=window_end,
                peak=peak,
                qpeak=float(cfs[peak]),
                peak_complete=peak < window_end,
                next_storm=next_start,
                flow_coverage=coverage,
                runoff_mm=_runoff_mm(flow, q0, area_sqmi),
            )
        )
    return events


def _last_value(cfs: np.ndarray, i: int, max_gap: int) -> float | None:
    """The flow at hour `i`, or the latest one up to `max_gap` hours earlier."""
    for j in range(i, max(i - max_gap, -1) - 1, -1):
        if j >= 0 and np.isfinite(cfs[j]):
            return float(cfs[j])
    return None


def _before(cumulative: np.ndarray, start: int, hours: int) -> float:
    return float(cumulative[start] - cumulative[max(0, start - hours)])


def _api(rain: np.ndarray) -> np.ndarray:
    """Antecedent precipitation index at each hour, decaying hourly at the daily rate."""
    decay = API_DAILY_DECAY ** (1 / 24)
    out = np.empty(len(rain))
    level = 0.0
    for i, value in enumerate(rain):
        level = level * decay + value
        out[i] = level
    return out


def _runoff_mm(flow: np.ndarray, q0: float, area_sqmi: float | None) -> float | None:
    if not area_sqmi:
        return None
    excess = np.nansum(np.maximum(flow - q0, 0))  # cfs·hours
    return float(excess * CFS_HOURS_TO_M3 / (area_sqmi * SQMI_TO_M2) * 1000)
