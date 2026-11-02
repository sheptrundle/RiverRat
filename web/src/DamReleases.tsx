import type { DamRelease } from "./api";
import { HOUR_MS, dayLabel, flow, timeLabel } from "./format";

const AGENCY = { USACE: "Army Corps", TVA: "TVA" };

/** The dams behind a reach: what they're releasing, their forecasts, and any notice. */
export default function DamReleases({ dams }: { dams: DamRelease[] }) {
  if (!dams.length) return null;
  return (
    <div className="panel dam-releases">
      <h3>Dam releases</h3>
      {dams.map((d) => (
        <div key={d.name} className="dam">
          <div className="dam-head">
            <strong>
              {d.name} <span className="muted">({AGENCY[d.agency]})</span>
            </strong>
            {d.now_cfs !== null && d.now_at && (
              <span>
                Releasing <strong>{flow(d.now_cfs, "cfs")}</strong>{" "}
                <span className="muted">as of {timeLabel(Date.parse(d.now_at))}</span>
              </span>
            )}
          </div>
          {d.notice && (
            <p className="dam-notice">
              <strong>{AGENCY[d.agency]} notice:</strong> {d.notice.message}{" "}
              <span className="muted small">(posted {timeLabel(Date.parse(d.notice.since))})</span>
            </p>
          )}
          <Sparkline start={Date.parse(d.recent.start)} values={d.recent.values} />
          {d.forecast.length > 0 && (
            <div className="dam-forecast">
              <span className="muted small">{AGENCY[d.agency]} forecast, daily average:</span>
              {d.forecast.map((f) => (
                <span key={f.day} className="chip-plain">
                  {dayLabel(f.day)} <strong>{flow(f.cfs, "cfs")}</strong>
                </span>
              ))}
            </div>
          )}
        </div>
      ))}
      <p className="muted small">
        Straight from the operators. Release plans can change at short notice: check the operator before you go.
      </p>
    </div>
  );
}

/** The last 48 hours of release as a small line chart. */
function Sparkline({ start, values }: { start: number; values: (number | null)[] }) {
  const W = 600;
  const H = 70;
  const seen = values.filter((v): v is number => v !== null);
  if (seen.length < 2) return null;
  const hi = Math.max(...seen, 1) * 1.1;
  const x = (i: number) => (i / (values.length - 1)) * W;
  const y = (v: number) => H - 4 - (v / hi) * (H - 12);
  const segments: string[] = [];
  let open = false;
  values.forEach((v, i) => {
    if (v === null) {
      open = false;
      return;
    }
    segments.push(`${open ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`);
    open = true;
  });
  const last = new Date(start + (values.length - 1) * HOUR_MS);
  return (
    <div className="sparkline">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label="Release over the last 48 hours">
        <path d={segments.join("")} vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="sparkline-labels muted small">
        <span>Last 48 hours (peak {flow(hi / 1.1, "cfs")})</span>
        <span>to {last.toLocaleString(undefined, { weekday: "short", hour: "numeric" })}</span>
      </div>
    </div>
  );
}
