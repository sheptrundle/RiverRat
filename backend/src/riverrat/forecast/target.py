"""Which gauge forecasts a reach, and everything needed to do it.

A reach is forecast through its gauge: the primary USGS gauge with a usable runnable
range. When the primary can't be used (often an AW "calculated" gauge), another of the
reach's USGS gauges stands in, preferring one on the same river. Flows are modeled in
cfs and reported in the range's units, through the gauge's stage rating for reaches
rated in feet.

A reach below dams is forecast from their release plans (forecast/releases.py): the
Corps' hourly plans, plus the rain forecast for any undammed part; TVA's daily plans for
peaking powerhouses; or TVA's recreation calendar on the Ocoee.

A reach with no gauge at all but a mapped watershed (reach_basin) is estimated instead:
through a similar gauge nearby, scaled by watershed area, against a range estimated from
the watershed's size and gradient (forecast/estimate.py).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, aliased

from riverrat.db.models import Basin, Gauge, Reach, ReachBasin, ReachGauge
from riverrat.forecast import estimate, live, releases
from riverrat.forecast.bands import Bands
from riverrat.models import analog, store

# A gauge whose flow record ended this long ago has been discontinued.
DISCONTINUED_AFTER = timedelta(days=60)
# Below dams on a live gauge of its own: hours for the gap between the gauge's reading and
# the sum of the parts to fade (to 1/e)
ANCHOR_FADE_H = 24
# AW sometimes fills in a range's top with a placeholder for "no upper limit" (20000 ft,
# 99999 cfs). A top this many times the gauge's highest reading in its last 10 years is
# taken as one, and the range as open-ended.
PLACEHOLDER_TOP = 5.0


class NoForecast(Exception):
    """The reach can't be forecast; the message says why."""


@dataclass(frozen=True)
class Estimate:
    """How a reach without a gauge is estimated."""

    area_sqmi: float  # the reach's watershed
    donor_area_sqmi: float
    distance_km: float  # between the watersheds' centers


@dataclass(frozen=True)
class Release:
    """A reach below dams: their planned release, added to any undammed part."""

    # The dams' release, hour 0 at the target's current.state.at: one path, or several
    # (paths × hours) with `path_weights`
    cfs: np.ndarray
    dams: tuple[str, ...]  # their names
    natural: str | None  # the undammed part's gauge, forecast from rain (None: dams only)
    gauge: Gauge  # the gauge that records the reach's own flow
    observed: live.Current  # its readings
    path_weights: np.ndarray | None = None
    natural_scale: float = 1.0  # on the undammed part's forecast
    natural_lag_h: int = 0  # hours from the undammed part's gauge to the reach's
    # "plan": the Corps' hourly plan; "tva": TVA's daily; "schedule": TVA's calendar;
    # "held": no plan, the dam holding its pattern or switching state
    basis: str = "plan"

    def add_to(self, members: np.ndarray, weights: np.ndarray):
        """Each undammed-flow member plus each release path, with combined weights."""
        if self.natural_scale != 1.0:
            members = members * self.natural_scale
        if self.natural_lag_h:
            lag = min(self.natural_lag_h, members.shape[-1])
            lead = np.repeat(members[:, :1], lag, axis=1)
            members = np.concatenate([lead, members[:, : members.shape[-1] - lag]], axis=1)
        paths = np.atleast_2d(self.cfs)[:, : members.shape[-1]]
        if self.path_weights is None:
            path_weights = np.full(len(paths), 1 / len(paths))
        else:
            path_weights = self.path_weights
        combined = (members[:, None, :] + paths[None]).reshape(-1, members.shape[-1])
        return combined, (weights[:, None] * path_weights[None]).ravel()


@dataclass(frozen=True)
class Target:
    reach: Reach
    gauge: Gauge
    bands: Bands
    lat: float
    lon: float
    library: analog.AnalogLibrary | None  # None: a reach below dams with no undammed part
    current: live.Current
    to_units: Callable  # cfs → the range's units
    primary: bool  # False: standing in for AW's primary gauge
    estimate: Estimate | None = None  # a reach without a gauge, through a donor gauge
    # For estimates: factors on flow, one per plausible range, each judged against `bands`
    spread: np.ndarray | None = None
    release: Release | None = None  # a reach below Army Corps dams

    def values(self, members: np.ndarray, weights: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Forecast members (cfs) in the range's units, with their weights. An estimate
        repeats each member once per plausible range; below dams, their planned release
        is added."""
        if self.release is not None:
            members, weights = self.release.add_to(members, weights)
        values = self.to_units(members)
        if self.spread is None:
            return values, weights
        n = len(self.spread)
        values = (values[None] * self.spread[:, None, None]).reshape(-1, values.shape[-1])
        return values, np.tile(weights, n) / n

    @property
    def observed(self) -> live.Current:
        """The reach's own recent flow: below dams, AW's calculated gauge."""
        return self.release.observed if self.release else self.current

    @property
    def record_gauge(self) -> Gauge:
        """The gauge whose readings say what the reach did (for the Track record)."""
        return self.release.gauge if self.release else self.gauge


def target(session: Session, data_dir: Path, reach: Reach, now: datetime) -> Target:
    if reach.aw_id in releases.DAM_RUN:
        raise NoForecast(
            "dam releases, more than rain, bring this section into range, often on dry days, "
            "and River Rat doesn't have its release schedule yet"
        )
    if reach.aw_id in releases.TVA_REACHES:
        return _tva(session, reach, releases.TVA_REACHES[reach.aw_id], now)
    # Each linked gauge, or for an NWS location its USGS twin at the same place.
    linked = aliased(Gauge)
    gauge = aliased(Gauge)
    rows = session.execute(
        select(ReachGauge, gauge, func.ST_Y(gauge.geom), func.ST_X(gauge.geom), Basin.area_sqmi)
        .join(linked, linked.id == ReachGauge.gauge_id)
        .join(
            gauge,
            gauge.id == case((linked.source == "USGS", linked.id), else_=linked.same_as_id),
        )
        .outerjoin(Basin, Basin.gauge_id == gauge.id)
        .where(
            ReachGauge.reach_id == reach.id,
            gauge.source == "USGS",
            gauge.geom.is_not(None),
            ReachGauge.metric.in_(("cfs", "ft")),
        )
        .order_by(ReachGauge.id)
    ).all()
    # Below Army Corps dams, the reach follows their release plans (forecast/releases.py)
    for link, linked_gauge in session.execute(
        select(ReachGauge, Gauge)
        .join(Gauge, Gauge.id == ReachGauge.gauge_id)
        .where(ReachGauge.reach_id == reach.id)
        .order_by(ReachGauge.is_primary.desc(), ReachGauge.id)
    ):
        key = (linked_gauge.source, linked_gauge.source_id)
        recipe = releases.RECIPES.get(key)
        if recipe and (recipe.reaches is None or reach.aw_id in recipe.reaches):
            return _release(session, data_dir, reach, link, linked_gauge, now)
        if key in releases.HELD:
            return _held(session, reach, link, linked_gauge, releases.HELD[key], now)
    river = reach.river.lower()
    rows = sorted(rows, key=lambda r: (not r[0].is_primary, river not in r[1].name.lower()))
    problem = "the reach has no USGS gauge with a runnable range"
    if not rows and not session.scalar(
        select(ReachGauge.id).where(ReachGauge.reach_id == reach.id).limit(1)
    ):
        return _estimate(session, data_dir, reach, now)
    if not rows:
        found = session.execute(
            select(ReachGauge, Gauge)
            .join(Gauge, Gauge.id == ReachGauge.gauge_id)
            .where(ReachGauge.reach_id == reach.id, Gauge.source == "CALCULATED")
        ).first()
        if found:
            problem = (
                f'its gauge is American Whitewater\'s calculated "{found[1].name}", usually a '
                "dam's release; River Rat doesn't forecast this dam's flows yet"
            )
    for link, gauge, lat, lon, area in rows:
        try:
            bands = Bands(link.metric, link.range_min, link.range_max)
        except ValueError:
            continue
        if gauge.cfs_end is not None and gauge.cfs_end < now - DISCONTINUED_AFTER:
            problem = (
                f"gauge {gauge.source_id} stopped reporting flow in {gauge.cfs_end.year} "
                "(USGS discontinued it)"
            )
            continue
        library = store.library(data_dir, gauge.source_id, area)
        if library is None:
            if gauge.cfs_begin is None:
                problem = f"gauge {gauge.source_id} has never published continuous flow data"
            else:
                problem = f"gauge {gauge.source_id} has too little history to forecast"
            continue
        to_units = _identity
        if link.metric == "ft":
            rating = store.rating(data_dir, gauge.source_id)
            if rating is None:
                problem = f"gauge {gauge.source_id} has no stage rating for a range in feet"
                continue
            to_units = rating.to_ft
        current = live.current(session, gauge.id, now)
        if current is None:
            if live.reports_stage(session, gauge.id, now):
                problem = (
                    f"gauge {gauge.source_id} is reporting stage only right now "
                    "(USGS isn't publishing its flow)"
                )
            else:
                problem = (
                    f"gauge {gauge.source_id} has no reading in the last 48 hours (it may be down)"
                )
            continue
        bands = _without_placeholder_top(bands, data_dir, gauge.source_id, to_units)
        return Target(reach, gauge, bands, lat, lon, library, current, to_units, link.is_primary)
    raise NoForecast(problem)


def _release(
    session: Session, data_dir: Path, reach: Reach, link: ReachGauge, calculated: Gauge, now
) -> Target:
    """Through the Corps' release plans, plus the rain forecast for any undammed part."""
    recipe = releases.RECIPES[(calculated.source, calculated.source_id)]
    dams = [releases.DAMS[key] for key in recipe.dams]
    try:
        bands = Bands(link.metric, link.range_min, link.range_max)
    except ValueError:
        raise NoForecast("the reach has no runnable range") from None
    to_units = _identity
    if link.metric == "ft":
        rating = store.rating(data_dir, calculated.source_id)
        if rating is None:
            raise NoForecast(
                f"gauge {calculated.source_id} has no stage rating for a range in feet"
            )
        to_units = rating.to_ft
    observed = live.current(session, calculated.id, now)
    if observed is None:
        raise NoForecast("the dam release readings haven't come in")
    if recipe.natural:
        found = session.execute(
            select(Gauge, Basin.area_sqmi, func.ST_Y(Gauge.geom), func.ST_X(Gauge.geom))
            .outerjoin(Basin, Basin.gauge_id == Gauge.id)
            .where(Gauge.source == "USGS", Gauge.source_id == recipe.natural)
        ).first()
        if found is None:
            raise NoForecast(f"gauge {recipe.natural} isn't set up yet")
        gauge, area, lat, lon = found
        library = store.library(data_dir, gauge.source_id, area)
        current = live.current(session, gauge.id, now)
        if library is None or current is None:
            raise NoForecast(f"gauge {gauge.source_id} can't be forecast right now")
    else:
        gauge, library, current = calculated, None, observed
        lat, lon = dams[0].lat, dams[0].lon
    cfs = np.zeros(analog.HORIZON_H + 1)
    for dam in dams:
        dam_gauge = session.scalar(
            select(Gauge.id).where(Gauge.source == "USACE", Gauge.source_id == dam.source_id)
        )
        plan = None
        if dam_gauge is not None:
            plan = releases.planned(
                session, dam_gauge, current.state.at, len(cfs), lag_h=recipe.dam_lag_h
            )
        if plan is None:
            raise NoForecast(f"the Army Corps hasn't published a release forecast for {dam.name}")
        cfs += plan
    if not recipe.composite:
        # The gauge measures the reach itself: start from its reading, letting the sum's
        # error at the start (a few percent) fade over a day or so
        natural_now = recipe.natural_scale * current.state.q if recipe.natural else 0.0
        start_error = observed.state.q - (cfs[0] + natural_now)
        cfs += start_error * np.exp(-np.arange(len(cfs)) / ANCHOR_FADE_H)
    release = Release(
        cfs,
        tuple(dam.name for dam in dams),
        gauge.name if recipe.natural else None,
        calculated,
        observed,
        natural_scale=recipe.natural_scale,
        natural_lag_h=recipe.natural_lag_h,
    )
    return Target(reach, gauge, bands, lat, lon, library, current, to_units, True, release=release)


def _held(
    session: Session, reach: Reach, link: ReachGauge, gauge: Gauge, dam: releases.Held, now
) -> Target:
    """Below a dam with no published plan: it holds its pattern, or switches state."""
    try:
        bands = Bands(link.metric, link.range_min, link.range_max)
    except ValueError:
        raise NoForecast("the reach has no runnable range") from None
    if link.metric != "cfs":
        raise NoForecast("the reach's range isn't in cfs")
    observed = live.current(session, gauge.id, now)
    if observed is None:
        raise NoForecast(f"gauge {gauge.source_id} has no reading in the last 48 hours")
    q = observed.state.q
    low = bands.minimum if bands.minimum is not None else -np.inf
    inside = low <= q <= (bands.maximum if bands.maximum is not None else np.inf)
    profile = observed.recent_cfs[-24:].copy()
    profile[~np.isfinite(profile)] = q
    cfs, weights = releases.held(
        profile,
        dam.out_cfs if inside else dam.in_cfs,
        dam.leave if inside else dam.enter,
        observed.state.at,
        analog.HORIZON_H + 1,
    )
    lat, lon = session.execute(
        select(func.ST_Y(Gauge.geom), func.ST_X(Gauge.geom)).where(Gauge.id == gauge.id)
    ).one()
    release = Release(cfs, (dam.dam,), None, gauge, observed, path_weights=weights, basis="held")
    return Target(reach, gauge, bands, lat, lon, None, observed, _identity, True, release=release)


def _tva(session: Session, reach: Reach, tva: releases.TvaReach, now: datetime) -> Target:
    """Below a TVA dam: its recreation calendar, or its daily plan for the powerhouse."""
    dam = releases.DAMS[tva.dam]
    if tva.range_cfs is None:
        raise NoForecast(tva.why_not)
    gauge = session.scalar(
        select(Gauge).where(Gauge.source == "TVA", Gauge.source_id == dam.source_id)
    )
    observed = live.current(session, gauge.id, now) if gauge is not None else None
    if gauge is None or observed is None:
        raise NoForecast(f"TVA's readings for {dam.name} haven't come in")
    bands = Bands("cfs", *tva.range_cfs)
    hours = analog.HORIZON_H + 1
    if tva.schedule is not None:
        # The riverbed's flow is the calendar's: the dam's discharge is mostly the flume's.
        at = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        recent_start = at - timedelta(hours=len(observed.recent_cfs) - 1)
        recent = releases.scheduled(tva, recent_start, len(observed.recent_cfs))
        state = analog.State(q=float(recent[-1]), api_mm=0.0, at=at)
        observed = live.Current(state, None, 0, recent_start, recent)
        cfs = np.stack([releases.scheduled(tva, at, hours), np.zeros(hours)])
        weights = np.array([releases.SCHEDULE_KEPT, 1 - releases.SCHEDULE_KEPT])
        basis = "schedule"
    else:
        plan = releases.planned(session, gauge.id, observed.state.at, hours, dam.forecast_source)
        if plan is None:
            raise NoForecast(f"TVA hasn't published a release forecast for {dam.name}")
        recent = observed.recent_cfs[np.isfinite(observed.recent_cfs)]
        running = recent[recent >= 0.5 * tva.unit_cfs]
        unit = float(np.median(running)) if len(running) else tva.unit_cfs
        base = float(min(recent.min(), 0.5 * unit)) if len(recent) else 0.0
        cfs = releases.generating(plan, observed.state.at, unit, base)
        weights = None
        basis = "tva"
    release = Release(cfs, (dam.name,), None, gauge, observed, path_weights=weights, basis=basis)
    return Target(
        reach,
        gauge,
        bands,
        dam.lat,
        dam.lon,
        None,
        observed,
        _identity,
        True,
        release=release,
    )


def _estimate(session: Session, data_dir: Path, reach: Reach, now: datetime) -> Target:
    """Through the nearest usable gauge whose basin is within DONOR_RATIO of the size."""
    basin = session.get(ReachBasin, reach.id)
    model = store.range_model(data_dir)
    if basin is None or model is None:
        raise NoForecast("the reach has no gauge")
    if estimate.snapped_downstream(basin.area_sqmi, basin.putin_area_sqmi):
        raise NoForecast("the reach has no gauge, and its watershed couldn't be mapped reliably")
    centre = (
        select(func.ST_Centroid(func.Geography(ReachBasin.geom)))
        .where(ReachBasin.reach_id == reach.id)
        .scalar_subquery()
    )
    distance = func.ST_Distance(func.Geography(func.ST_Centroid(Basin.geom)), centre)
    donors = session.execute(
        select(Gauge, Basin.area_sqmi, distance, func.ST_Y(Gauge.geom), func.ST_X(Gauge.geom))
        .join(Basin, Basin.gauge_id == Gauge.id)
        .where(
            Gauge.source == "USGS",
            Gauge.geom.is_not(None),
            Basin.area_sqmi.between(
                basin.area_sqmi / estimate.DONOR_RATIO, basin.area_sqmi * estimate.DONOR_RATIO
            ),
            distance <= estimate.DONOR_MAX_KM * 1000,
        )
        .order_by(distance)
    ).all()
    for gauge, area, meters, lat, lon in donors:
        if gauge.cfs_end is not None and gauge.cfs_end < now - DISCONTINUED_AFTER:
            continue
        library = store.library(data_dir, gauge.source_id, area)
        if library is None:
            continue
        current = live.current(session, gauge.id, now)
        if current is None:
            continue
        minimum, strays = model.minimum(basin.area_sqmi, reach.gradient_fpm)
        scale = basin.area_sqmi / area
        return Target(
            reach,
            gauge,
            Bands("cfs", minimum, minimum * model.ratio),
            lat,
            lon,
            library,
            current,
            lambda cfs, scale=scale: np.asarray(cfs, float) * scale,
            False,
            Estimate(basin.area_sqmi, area, meters / 1000),
            1 / strays,  # a member q is in a range strayed by f when q / f is in the fit
        )
    raise NoForecast(
        f"the reach has no gauge, and no similar gauge within {estimate.DONOR_MAX_KM:.0f} km"
    )


def _without_placeholder_top(bands: Bands, data_dir: Path, site: str, to_units: Callable) -> Bands:
    """The range with AW's placeholder top (PLACEHOLDER_TOP) dropped, if it has one."""
    if bands.maximum is None or bands.minimum is None:
        return bands
    daily = store.daily_range(data_dir, site)
    if daily is None or not len(daily[1]):
        return bands
    highest = float(np.nanmax(np.asarray(to_units(daily[1]), float)))
    if bands.maximum > PLACEHOLDER_TOP * highest:
        return Bands(bands.metric, bands.minimum, None)
    return bands


def _identity(cfs):
    return np.asarray(cfs, float)
