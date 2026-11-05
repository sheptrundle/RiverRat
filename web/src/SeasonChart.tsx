import type { MonthShare } from "./api";
import { BAND_COLORS } from "./charts";
import { percent } from "./format";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Best months to paddle: each month's share of days in range, too low, and too high all
 * day, from the gauge's last 10 years. */
export function SeasonChart({ months }: { months: (MonthShare | null)[] }) {
  const known = months.flatMap((m, i) => (m ? [{ ...m, month: i }] : []));
  if (known.length < 6) return null;
  const ranked = [...known].sort((a, b) => b.in_range - a.in_range);
  const best = ranked.filter((m) => m.in_range >= 0.05 && m.in_range >= ranked[0].in_range * 0.8);
  const bestSet = new Set(best.map((m) => m.month));
  const thisMonth = new Date().getMonth();

  return (
    <div className="panel">
      <h3>Best months to paddle</h3>
      <div className="season" role="img" aria-label={`Days in range by month: ${known.map((m) => `${MONTHS[m.month]} ${percent(m.in_range)}`).join(", ")}`}>
        {months.map((m, i) => (
          <div key={MONTHS[i]} className={`season-month${bestSet.has(i) ? " best" : ""}${i === thisMonth ? " now" : ""}`}>
            <span className="season-value small">{m ? percent(m.in_range) : "–"}</span>
            <div
              className="season-bar"
              title={m ? `${MONTHS[i]}: in range ${percent(m.in_range)}, too low ${percent(m.too_low)}, too high ${percent(m.too_high)} of ${m.days} days` : `${MONTHS[i]}: too few days on record`}
            >
              {m && (
                <>
                  <span style={{ height: `${m.too_high * 100}%`, background: BAND_COLORS.too_high }} />
                  <span style={{ height: `${m.in_range * 100}%`, background: BAND_COLORS.medium }} />
                  <span style={{ height: `${m.too_low * 100}%`, background: BAND_COLORS.too_low }} />
                </>
              )}
            </div>
            <span className="season-label small">{MONTHS[i]}</span>
          </div>
        ))}
      </div>
      <p className="small">
        {best.length === 0
          ? "Rarely in range in any month: it takes a big storm."
          : best.length >= 6
            ? `In range most of the year; least often in ${listMonths(ranked.slice(-2).map((m) => m.month))}.`
            : `Most often in range in ${listMonths(best.map((m) => m.month))}.`}
      </p>
      <p className="legend">
        <span className="key" style={{ background: BAND_COLORS.medium }} /> in range at some point in the day{" "}
        <span className="key" style={{ background: BAND_COLORS.too_low }} /> too low all day{" "}
        <span className="key" style={{ background: BAND_COLORS.too_high }} /> too high all day · the gauge's last 10 years
      </p>
    </div>
  );
}

function listMonths(months: number[]): string {
  const names = [...months].sort((a, b) => a - b).map((m) => MONTHS[m]);
  if (names.length <= 2) return names.join(" and ");
  return `${names.slice(0, -1).join(", ")}, and ${names[names.length - 1]}`;
}
