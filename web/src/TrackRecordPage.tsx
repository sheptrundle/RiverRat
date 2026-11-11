import { useState } from "react";
import type { TrackBin, TrackLead, TrackRecord } from "./api";
import { useApi } from "./api";
import { dayLabel, percent } from "./format";
import { sectionLabel } from "./tables";

// The day-before calls list (hits and misses) is hidden for now; flip to bring it back.
const SHOW_RECENT_CALLS = false;
// The perfect-rain hindcast (DESIGN.md §6.4, jobs/hindcast.py): the analog model's hourly CRPS
// against replaying all of a gauge's past storms alike, library ≤ 2019. Update with each rerun.
const BACKTEST = { lowerError: "39%", storms: "177,000", years: "2020–25" };
const LEAD_LABELS = ["That day", "1 day ahead", "2 days", "3 days", "4 days", "5 days"];

/** How the Outlook's chances have held up against what the rivers did. */
export default function TrackRecordPage() {
  const [calls, setCalls] = useState<"all" | "hits" | "misses">("all");
  const { data, error } = useApi<TrackRecord>("/api/track-record");

  if (error) return <p className="error">Couldn't load the track record: {error}</p>;
  if (!data) return <p className="muted">Loading the track record…</p>;

  const confident = pool(data.reliability, 0.7, 0.9);
  const unlikely = pool(data.reliability, 0, 0.1);
  const shownCalls = data.recent.filter((c) =>
    calls === "all" ? true : calls === "hits" ? hit(c.p_runnable, c.observed) : !hit(c.p_runnable, c.observed),
  );

  return (
    <section className="track">
      <h2>Track record</h2>
      <p className="muted">
        Every morning at 6 am Eastern, River Rat keeps each gauged reach's chance of running that day and the next five. Once
        a day is over, it checks the gauge: was the reach in range at some point in daylight?{" "}
        {data.scored > 0 && (
          <>
            Scored: <strong>{data.scored.toLocaleString()}</strong> reach-days, {dayLabel(data.first_day!)} –{" "}
            {dayLabel(data.last_day!)}.
          </>
        )}
      </p>
      {data.replayed > 0 && (
        <p className="muted small">
          {data.replayed === data.scored ? "All" : `${data.replayed.toLocaleString()} of these`} come from mornings before
          River Rat started keeping its forecasts, replayed afterwards from only what was known then: NOAA's rain forecast
          issued by that morning, the gauge readings up to it, and past storms that had already ended.
        </p>
      )}

      {data.scored === 0 ? (
        <p className="panel">Nothing scored yet. Days are scored once they're over.</p>
      ) : (
        <>
          <div className="headline">
            {confident && (
              <div className="panel stat">
                <span className="big">{percent(confident.ran)}</span>
                <span>
                  ran when we said 70–90% <span className="muted">({confident.n.toLocaleString()} reach-days)</span>
                </span>
              </div>
            )}
            {unlikely && (
              <div className="panel stat">
                <span className="big">{percent(unlikely.ran)}</span>
                <span>
                  ran when we said under 10% <span className="muted">({unlikely.n.toLocaleString()})</span>
                </span>
              </div>
            )}
            <div className="panel stat">
              <span className="big">{BACKTEST.lowerError}</span>
              <span>
                lower error than replaying a river's typical storm response{" "}
                <span className="muted">
                  (back-tested on {BACKTEST.storms} storms, {BACKTEST.years}, given the rain that fell)
                </span>
              </span>
            </div>
          </div>

          <div className="track-charts">
            <div className="panel">
              <h3>Did the chances come true?</h3>
              <Reliability bins={data.reliability} />
              <p className="muted small">
                Each dot is the forecasts in one 10% band: how often those reaches ran (up) against the chance we gave (across).
                On the line means honest chances. Bigger dots, more reach-days.
              </p>
            </div>
            <div className="panel">
              <h3>Days ahead</h3>
              <Leads leads={data.leads} />
              <p className="muted small">
                Brier skill against assuming each reach stays in the band it was in that morning: 0% is no better, 100% perfect.
                The same day, where a river is now already says a lot, so there's less to add; further out, rivers move, and the
                forecast's edge over standing still grows.
              </p>
            </div>
          </div>

          {SHOW_RECENT_CALLS && (
            <div className="panel">
              <div className="calls-header">
                <h3>Recent calls, made the day before</h3>
                <div className="segmented" role="radiogroup" aria-label="Calls">
                  {(["all", "hits", "misses"] as const).map((c) => (
                    <button key={c} className={c === calls ? "on" : ""} onClick={() => setCalls(c)}>
                      {c[0].toUpperCase() + c.slice(1)}
                    </button>
                  ))}
                </div>
              </div>
              <p className="muted small">Reaches we gave at least 50%, and every reach that ran, over the last scored days.</p>
              <table className="calls">
                <thead>
                  <tr>
                    <th>Day</th>
                    <th>Reach</th>
                    <th>We said</th>
                    <th>It</th>
                  </tr>
                </thead>
                <tbody>
                  {shownCalls.map((c) => (
                    <tr key={`${c.reach_id}-${c.day}`} onClick={() => (location.hash = `#/reach/${c.reach_id}`)}>
                      <td className="nowrap">{dayLabel(c.day)}</td>
                      <td>
                        <a href={`#/reach/${c.reach_id}`}>{c.river}</a>
                        <div className="muted small">{sectionLabel(c)}</div>
                      </td>
                      <td className="nowrap">{percent(c.p_runnable)}</td>
                      <td className={hit(c.p_runnable, c.observed) ? "hit nowrap" : "miss nowrap"}>
                        {c.observed ? "ran" : "didn't"} {hit(c.p_runnable, c.observed) ? "✓" : "✗"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function hit(p: number, observed: boolean): boolean {
  return p >= 0.5 === observed;
}

function signed(x: number): string {
  return `${x >= 0 ? "+" : "−"}${Math.round(Math.abs(x) * 100)}%`;
}

/** The bins from `low` up to `high`, pooled. */
function pool(bins: TrackBin[], low: number, high: number) {
  const inside = bins.filter((b) => b.low >= low - 1e-9 && b.high <= high + 1e-9);
  const n = inside.reduce((s, b) => s + b.n, 0);
  if (n < 20) return null;
  return { n, ran: inside.reduce((s, b) => s + b.ran * b.n, 0) / n };
}

const SIZE = 300;
const PAD = 44;

function Reliability({ bins }: { bins: TrackBin[] }) {
  const span = SIZE - 2 * PAD;
  const x = (p: number) => PAD + p * span;
  const y = (p: number) => SIZE - PAD - p * span;
  const most = Math.max(...bins.map((b) => b.n), 1);
  return (
    <svg viewBox={`0 0 ${SIZE} ${SIZE}`} className="reliability" role="img" aria-label="Forecast chance against how often reaches ran">
      {[0, 0.25, 0.5, 0.75, 1].map((t) => (
        <g key={t}>
          <line x1={x(t)} x2={x(t)} y1={y(0)} y2={y(1)} className="grid" />
          <line x1={x(0)} x2={x(1)} y1={y(t)} y2={y(t)} className="grid" />
          <text x={x(t)} y={SIZE - PAD + 16} textAnchor="middle" className="tick">
            {Math.round(t * 100)}%
          </text>
          <text x={PAD - 6} y={y(t) + 4} textAnchor="end" className="tick">
            {Math.round(t * 100)}%
          </text>
        </g>
      ))}
      <line x1={x(0)} y1={y(0)} x2={x(1)} y2={y(1)} className="diagonal" />
      <polyline points={bins.map((b) => `${x(b.said)},${y(b.ran)}`).join(" ")} className="trace" />
      {bins.map((b) => (
        <circle key={b.low} cx={x(b.said)} cy={y(b.ran)} r={3 + 7 * Math.sqrt(b.n / most)} className="dot">
          <title>
            Said {percent(b.low)}–{percent(b.high)} ({b.n.toLocaleString()} reach-days): ran {percent(b.ran)}
          </title>
        </circle>
      ))}
      <text x={SIZE / 2} y={SIZE - 4} textAnchor="middle" className="axis-label">
        We said
      </text>
      <text x={8} y={SIZE / 2} textAnchor="middle" transform={`rotate(-90 8 ${SIZE / 2})`} className="axis-label">
        It ran
      </text>
    </svg>
  );
}

function Leads({ leads }: { leads: TrackLead[] }) {
  return (
    <div className="leads">
      {leads.map((l) => (
        <div key={l.lead_days} className="lead-row" title={`${l.n.toLocaleString()} reach-days`}>
          <span className="lead-label">{LEAD_LABELS[l.lead_days] ?? `${l.lead_days} days`}</span>
          <span className="lead-bar">
            <span style={{ width: `${Math.max(0, (l.skill ?? 0) * 100)}%` }} />
          </span>
          <span className="lead-value">{l.skill == null ? "–" : signed(l.skill)}</span>
        </div>
      ))}
    </div>
  );
}
