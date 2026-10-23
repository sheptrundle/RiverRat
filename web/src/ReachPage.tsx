import { useState } from "react";
import type { RainResponse, StormType, WhatIf } from "./api";
import { useApi } from "./api";
import { BAND_COLORS, BandBar, Hydrograph, ResponseCurve } from "./charts";
import { BAND_LABELS, HOUR_MS, awUrl, classLabel, rangeLabel, dateLabel, dayLabel, flow, inches, percent, rainRange, timeLabel, weekday } from "./format";

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

  const base = `/api/reaches/${reachId}`;
  const whatif = useApi<WhatIf>(`${base}/whatif?rain_in=${rain}&storm=${storm}&start_h=${startH}`, 120);
  const curve = useApi<RainResponse>(`${base}/rain-response?storm=${storm}&start_h=${startH}`, 120);
  const w = whatif.data;

  if (whatif.error && !w) {
    return (
      <section>
        <p>
          <a href="#/">← Outlook</a>
        </p>
        <p className="error">No forecast for this reach: {whatif.error}</p>
      </section>
    );
  }
  if (!w) return <p className="muted">Loading forecast…</p>;

  const metric = w.bands.metric;
  const nowMs = Date.parse(w.now.at);
  const stormStart = nowMs + startH * HOUR_MS;
  const rr = curve.data;
  // The response curve's day: the one clicked, else the first with a real chance of running.
  const shownDay = dayIndex ?? Math.max(0, w.days.findIndex((d) => d.p_runnable >= 0.5));
  const dayCurve = rr?.days[Math.min(shownDay, rr.days.length - 1)];

  return (
    <section className={whatif.loading ? "reach loading" : "reach"}>
      <p>
        <a href="#/">← Outlook</a>
      </p>
      <header className="reach-header">
        <div>
          <h2>
            {w.reach.river} <span className="muted">· {w.reach.section}</span>
          </h2>
          <p className="muted">
            {w.reach.difficulty && <>Class {classLabel(w.reach.difficulty)} · </>}
            Gauge: {w.gauge.name} ({w.gauge.site})
            {!w.gauge.primary && <> (standing in for AW's primary gauge, with AW's range for it)</>}
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
        </div>
      </header>

      <div className="panel">
        <h3>What if it rains?</h3>
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
        {w.extrapolated && (
          <p className="warning">More rain than any similar storm this gauge has seen: treat these numbers as rough.</p>
        )}

        <Hydrograph whatif={w} />
        <p className="legend">
          <span className="key observed" /> observed <span className="key median" /> forecast median{" "}
          <span className="key fan" /> 80% range <span className="key rain" /> storm{" "}
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
