import { useState } from "react";
import type { RainResponse, ReachReleases, StormType, WhatIf } from "./api";
import { useApi } from "./api";
import { BAND_COLORS, BandBar, Hydrograph, ResponseCurve } from "./charts";
import DamReleases from "./DamReleases";
import { Star, useFavorites } from "./favorites";
import { sectionLabel } from "./tables";
import { BAND_LABELS, HOUR_MS, awUrl, classLabel, RANGE_NOTES, rangeLabel, dateLabel, dayLabel, flow, inches, percent, rainRange, timeLabel, weekday } from "./format";

// Kept in step with releases.SCHEDULE_KEPT in the backend
const SCHEDULE_KEPT = 0.95;
const DAMS_ONLY_LINE = { plan: "Corps release plan", tva: "TVA plan (typical placement)", schedule: "release schedule" };

const STORMS: { type: StormType; label: string; hint: string }[] = [
  { type: "burst", label: "Burst", hint: "3 h thunderstorm" },
  { type: "typical", label: "Typical", hint: "18 h, the median storm" },
  { type: "soaker", label: "Soaker", hint: "36 h steady rain" },
];

export default function ReachPage({ reachId }: { reachId: number }) {
  const [rain, setRain] = useState(1);
  const [storm, setStorm] = useState<StormType>("typical");
  const [startH, setStartH] = useState(12);
  const [dayIndex, setDayIndex] = useState<number | null>(null);
  const [favorites, toggleFavorite] = useFavorites();

  const base = `/api/reaches/${reachId}`;
  const whatif = useApi<WhatIf>(`${base}/whatif?rain_in=${rain}&storm=${storm}&start_h=${startH}`, 120);
  const curve = useApi<RainResponse>(`${base}/rain-response?storm=${storm}&start_h=${startH}`, 120);
  const dams = useApi<ReachReleases>(`${base}/releases`);
  const w = whatif.data;

  if (whatif.error && !w) {
    const reach = dams.data?.reach;
    return (
      <section>
        <p>
          <a href="#/outlook">← Outlook</a>
        </p>
        {reach && (
          <h2>
            <Star id={reachId} favorites={favorites} toggle={toggleFavorite} /> {reach.river}{" "}
            <span className="muted">· {sectionLabel(reach)}</span>
          </h2>
        )}
        <p className={dams.data?.dams.length ? "muted" : "error"}>No forecast for this reach: {whatif.error}</p>
        <DamReleases dams={dams.data?.dams ?? []} />
      </section>
    );
  }
  if (!w) return <p className="muted">Loading forecast…</p>;

  const metric = w.bands.metric;
  const damsOnly = w.release !== null && w.release.natural === null; // rain changes nothing
  const nowMs = Date.parse(w.now.at);
  const stormStart = nowMs + startH * HOUR_MS;
  const rr = curve.data;
  // The response curve's day: the one clicked, else the first with a real chance of running.
  const shownDay = dayIndex ?? Math.max(0, w.days.findIndex((d) => d.p_runnable >= 0.5));
  const dayCurve = rr?.days[Math.min(shownDay, rr.days.length - 1)];

  return (
    <section className={whatif.loading ? "reach loading" : "reach"}>
      <p>
        <a href="#/outlook">← Outlook</a>
      </p>
      <header className="reach-header">
        <div>
          <h2>
            <Star id={reachId} favorites={favorites} toggle={toggleFavorite} /> {w.reach.river} <span className="muted">· {sectionLabel(w.reach)}</span>
          </h2>
          <p className="muted">
            {w.reach.difficulty && <>Class {classLabel(w.reach.difficulty)} · </>}
            {w.release ? (
              <>
                Flow: {w.release.dams.join(" + ")} release{w.release.natural && <> + {w.release.natural}</>}
              </>
            ) : w.estimate ? (
              <>No gauge: estimated from {w.gauge.name} ({w.gauge.site})</>
            ) : (
              <>
                Gauge: {w.gauge.name} ({w.gauge.site})
                {!w.gauge.primary && <> (standing in for AW's primary gauge, with AW's range for it)</>}
              </>
            )}
            {w.reach.aw_id && (
              <>
                {" · "}
                <a href={awUrl(w.reach.aw_id)} target="_blank" rel="noreferrer">
                  American Whitewater
                </a>
              </>
            )}
          </p>
        </div>
        <div className="now-box">
          <span className="chip" style={{ background: BAND_COLORS[w.now.band] }}>
            {BAND_LABELS[w.now.band]}
          </span>
          <strong>{flow(w.now.value, metric)}</strong>
          <span className="muted">
            {metric === "ft" && `${flow(w.now.cfs, "cfs")} · `}
            as of {timeLabel(nowMs)}
          </span>
          <span className="muted">Range {rangeLabel(w.bands.minimum, w.bands.maximum, metric)}</span>
          {w.history && (
            <span className="muted">In range on {percent(w.history.share_in_range)} of days over the last 10 years</span>
          )}
        </div>
      </header>

      {w.history?.note && <p className="warning">{RANGE_NOTES[w.history.note]}</p>}
      {w.estimate && (
        <p className="panel estimate-note">
          <strong>Estimate.</strong> This reach has no gauge. Its flow is {w.gauge.name}'s, {Math.round(w.estimate.distance_km)}{" "}
          km away, scaled from that gauge's {Math.round(w.estimate.donor_area_sqmi)} mi² watershed to this reach's{" "}
          {Math.round(w.estimate.area_sqmi)} mi². The runnable range of about{" "}
          {rangeLabel(w.bands.minimum, w.bands.maximum, w.bands.metric)} is estimated from watershed size and gradient, and
          real ranges often differ by 2× either way, so the chances below are spread to match. Treat it as a hint and check
          before you go.
        </p>
      )}
      {w.release?.basis === "plan" && (
        <p className="panel release-note">
          <strong>Below the dam.</strong> This forecast uses the Army Corps' own daily plan for{" "}
          {w.release.dams.join(" and ")}'s release
          {w.release.natural
            ? `, plus River Rat's rain forecast for the undammed water measured at ${w.release.natural}.`
            : ". The dam sets all of the flow here, so rain doesn't change the forecast."}{" "}
          The Corps can change its plan at short notice: check the day before you go.
        </p>
      )}
      {w.release?.basis === "tva" && (
        <p className="panel release-note">
          <strong>Below the dam.</strong> The river runs when {w.release.dams.join(" and ")}'s powerhouse generates. This
          forecast uses TVA's plan for each day's average release over the next 3 days, which says roughly how many hours
          it will generate but not when, so the chance spreads those hours across the day (after day 3, the last day's
          plan is held). American Whitewater gives no range here, so River Rat counts {w.bands.minimum?.toLocaleString()}{" "}
          cfs and up (one generator) as running. TVA can change its plan at short notice: check its schedule the day
          before you go.
        </p>
      )}
      {w.release?.basis === "schedule" && (
        <p className="panel release-note">
          <strong>Released on a schedule.</strong> The dam bypasses this riverbed except for TVA's recreation releases,
          so this forecast follows TVA's published calendar for {w.release.dams.join(" and ")}: running on scheduled days
          and hours, dry otherwise. A scheduled release is occasionally called off, so release days show{" "}
          {Math.round(SCHEDULE_KEPT * 100)}%.
        </p>
      )}
      {w.reach.next_release && (
        <p className="panel release-note">
          Next scheduled dam release: <strong>{releaseLabel(w.reach.next_release)}</strong> (American Whitewater's calendar).
          {!w.release && " The forecast below is the river's natural flow from rain; on release days the dam sets the flow."}
        </p>
      )}
      <DamReleases dams={dams.data?.dams ?? []} />
      <div className="panel">
        <h3>{damsOnly ? "Release forecast" : "What if it rains?"}</h3>
        {!damsOnly && (
          <div className="controls">
            <label>
              Basin rain <strong>{inches(rain)}</strong>
              <input type="range" min={0} max={4} step={0.25} value={rain} onChange={(e) => setRain(Number(e.target.value))} />
            </label>
            <label>
              Starting <strong>{timeLabel(stormStart)}</strong>
              <input type="range" min={0} max={120} step={3} value={startH} onChange={(e) => setStartH(Number(e.target.value))} />
            </label>
            <div className="segmented" role="radiogroup" aria-label="Storm type">
              {STORMS.map((s) => (
                <button key={s.type} className={s.type === storm ? "on" : ""} title={s.hint} onClick={() => setStorm(s.type)}>
                  {s.label}
                </button>
              ))}
            </div>
          </div>
        )}
        {w.extrapolated && (
          <p className="warning">More rain than any similar storm this gauge has seen: treat these numbers as rough.</p>
        )}

        <Hydrograph whatif={w} showStorm={!damsOnly} />
        <p className="legend">
          <span className="key observed" /> observed <span className="key median" /> {damsOnly ? DAMS_ONLY_LINE[w.release?.basis ?? "plan"] : "forecast median"}{" "}
          {!damsOnly && (
            <>
              <span className="key fan" /> 80% range <span className="key rain" /> storm{" "}
            </>
          )}
          <span className="key daylight" /> daylight
        </p>

        <div className="days">
          {w.days.map((d, i) => (
            <button key={d.day} className={i === shownDay ? "day on" : "day"} onClick={() => setDayIndex(i)}>
              <span className="muted">{weekday(d.day)}</span>
              <strong>{percent(d.p_runnable)}</strong>
              <BandBar bands={d.peak_bands} />
              <span className="muted small">peak ~{flow(d.peak.p50, metric)}</span>
              {d.p_too_high_all_day >= 0.2 && <span className="small too-high">{percent(d.p_too_high_all_day)} too high all day</span>}
            </button>
          ))}
        </div>
        <p className="muted small">Chance of being in range at some point during daylight. Bars: where the day's peak lands, too low → too high.</p>
      </div>

      {rr && dayCurve && (
        <div className="panel">
          <h3>
            How much rain does {dayLabel(dayCurve.day)} need?{" "}
            <span className="muted small">({STORMS.find((s) => s.type === storm)?.label.toLowerCase()} storm starting {timeLabel(stormStart)})</span>
          </h3>
          <ResponseCurve rainIn={rr.rain_in} curve={dayCurve} rain={rain} />
          <p className="legend">
            <span className="key runnable" /> chance runnable <span className="key too-high" /> too high all day{" "}
            <span className="key rain" /> NBM forecast basin rain through sunset (10–90%)
            {rr.forecast_issued_at && <>, issued {timeLabel(Date.parse(rr.forecast_issued_at))}</>}
          </p>
          <p>
            {dayCurve.rain_needed_in === null
              ? "Even 4 inches likely isn't enough that day."
              : dayCurve.rain_needed_in === 0
                ? "Likely to reach the range without more rain."
                : `Needs about ${inches(dayCurve.rain_needed_in)} for an even chance of reaching the range.`}
            {dayCurve.forecast_rain_in &&
              ` The forecast through that day is ${rainRange(dayCurve.forecast_rain_in)}.`}
          </p>
        </div>
      )}

      {w.analogs.length > 0 && (
        <div className="panel">
          <h3>Similar past storms</h3>
          <table className="analogs">
            <thead>
              <tr>
                <th>Date</th>
                <th>Rain</th>
                <th>Duration</th>
                <th>Flow at start → peak</th>
                <th>Peak after</th>
                <th>Similarity</th>
              </tr>
            </thead>
            <tbody>
              {w.analogs.map((a) => (
                <tr key={a.start}>
                  <td>{dateLabel(a.start)}</td>
                  <td>{inches(a.rain_in)}</td>
                  <td>{a.duration_h} h</td>
                  <td>
                    {flow(a.q0_cfs, "cfs")} → {flow(a.qpeak_cfs, "cfs")}
                  </td>
                  <td>{a.t_peak_h} h</td>
                  <td>
                    <span className="weight" style={{ width: `${(a.weight / w.analogs[0].weight) * 60}px` }} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted small">The forecast replays the 60 most similar storms at this gauge, scaled to this storm's rain and today's flow.</p>
        </div>
      )}
    </section>
  );
}

/** "Thu, Sep 16, 2027": a release can be months away. */
function releaseLabel(day: string): string {
  return new Date(`${day}T12:00:00Z`).toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  });
}
