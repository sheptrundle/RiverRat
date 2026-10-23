import { useEffect, useMemo, useState } from "react";
import type { Outlook, OutlookReach } from "./api";
import { useApi } from "./api";
import { BAND_COLORS, BandBar } from "./charts";
import { BAND_LABELS, RANGE_NOTES, classLabel, flow, inches, percent, rainRange, timeLabel, weekday } from "./format";
import type { Sort } from "./tables";
import { PAGE_SIZES, Pager, SortHeader, allStates, compareClass, matchesQuery, paged, stateCodes } from "./tables";

// "best" = the highest chance over the days shown; "day:N" = day N's chance.
type Column = "best" | "river" | "class" | "state" | "runs" | `day:${number}`;
const CHANCES = [
  { label: "Any chance", min: 0 },
  { label: "≥ 10% some day", min: 0.1 },
  { label: "≥ 50% some day", min: 0.5 },
];

/** A day cell's background: green, stronger with the chance of running. */
function shade(p: number): string {
  return `color-mix(in srgb, var(--band-medium) ${Math.round(p * 70)}%, transparent)`;
}

export default function OutlookPage() {
  const { data, error } = useApi<Outlook>("/api/outlook");
  const [query, setQuery] = useState("");
  const [state, setState] = useState("");
  const [minChance, setMinChance] = useState(0);
  const [sort, setSort] = useState<Sort<Column>>({ column: "best", descending: true });
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(PAGE_SIZES[0]);
  // 0: NOAA's forecast; else an index into whatif_rain_in (a link can set it: #/?rain=1.5)
  const [rainParam] = useState(() => Number(new URLSearchParams(location.hash.split("?")[1]).get("rain") ?? 0));
  const [rainIndex, setRainIndex] = useState(0);
  useEffect(() => {
    if (data && rainParam > 0) setRainIndex(Math.max(0, data.whatif_rain_in.indexOf(rainParam)));
  }, [data, rainParam]);

  const reaches = data?.reaches ?? [];
  const days = data?.days ?? [];
  const states = useMemo(() => allStates(reaches), [reaches]);
  /** The chance of running on `day`: under NOAA's forecast, or the what-if storm. */
  const chance = (r: OutlookReach, day: string): number | null => {
    if (rainIndex > 0) return r.whatif?.find((w) => w.day === day)?.p_runnable[rainIndex] ?? null;
    return r.days.find((d) => d.day === day)?.p_runnable ?? null;
  };
  const best = (r: OutlookReach) => Math.max(0, ...days.map((day) => chance(r, day) ?? 0));
  const matches = useMemo(() => {
    const found = reaches.filter(
      (r) => matchesQuery(r, query) && (!state || stateCodes(r.states).includes(state)) && best(r) >= minChance,
    );
    const sign = sort.descending ? -1 : 1;
    const byColumn = (a: OutlookReach, b: OutlookReach): number => {
      if (sort.column === "best") return sign * (best(a) - best(b));
      if (sort.column === "river") return sign * a.river.localeCompare(b.river);
      if (sort.column === "state") return sign * stateCodes(a.states).localeCompare(stateCodes(b.states));
      if (sort.column === "class") return compareClass(a.difficulty, b.difficulty, sign);
      if (sort.column === "runs") return sign * ((a.in_range_share ?? -1) - (b.in_range_share ?? -1));
      const day = days[Number(sort.column.slice(4))];
      return sign * ((chance(a, day) ?? 0) - (chance(b, day) ?? 0));
    };
    return found.sort(
      (a, b) =>
        byColumn(a, b) || a.river.localeCompare(b.river) || a.section.localeCompare(b.section, undefined, { numeric: true }),
    );
  }, [reaches, days, query, state, minChance, sort, rainIndex]);

  if (error) return <p className="error">Couldn't load the Outlook: {error}</p>;
  if (!data) return <p className="muted">Loading the Outlook…</p>;
  if (!data.computed_at)
    return (
      <p className="muted">
        The Outlook hasn't been computed yet. It runs hourly in the worker (<code>python -m riverrat.jobs.outlook</code>).
      </p>
    );

  const { pages, current, shown } = paged(matches, page, pageSize);
  const onSort = (column: Column) => {
    // chances sort highest first; names A→Z
    const firstDescending = column === "best" || column === "runs" || column.startsWith("day:");
    setSort((s) => ({ column, descending: s.column === column ? !s.descending : firstDescending }));
    setPage(0);
  };
  const resetting =
    <T,>(set: (v: T) => void) =>
    (v: T) => {
      set(v);
      setPage(0);
    };
  const pager = (
    <Pager page={current} pages={pages} pageSize={pageSize} onPage={setPage} onPageSize={resetting(setPageSize)} />
  );
  const dry = reaches.every((r) => r.days.every((d) => !d.rain_in || d.rain_in.p90 < 0.05));
  const whatif = rainIndex > 0;
  const stormStart = data.whatif_storm_start ? Date.parse(data.whatif_storm_start) : null;

  return (
    <section>
      <h2>Outlook</h2>
      <p className="muted">
        Chance each reach is in its runnable range at some point during daylight. Updated {timeLabel(Date.parse(data.computed_at))}
        {data.rain_issued_at && ` from NOAA's ${timeLabel(Date.parse(data.rain_issued_at))} rain forecast`}.
        {dry && " NOAA's forecast is dry, so these follow each river's recession: try the rain slider."}
      </p>
      {stormStart && data.whatif_rain_in.length > 1 && (
        <div className={whatif ? "panel rain-bar on" : "panel rain-bar"}>
          <label htmlFor="region-rain">
            <strong>
              {whatif
                ? `What if every watershed got ${inches(data.whatif_rain_in[rainIndex])} starting ${timeLabel(stormStart)}?`
                : "Rain: NOAA's forecast"}
            </strong>
          </label>
          <input
            id="region-rain"
            type="range"
            min={0}
            max={data.whatif_rain_in.length - 1}
            step={1}
            value={rainIndex}
            onChange={(e) => resetting(setRainIndex)(Number(e.target.value))}
          />
          <span className="muted small">
            {whatif
              ? "A typical 18-hour storm of this size on every watershed, in place of the forecast. Open a reach to try other amounts, timings, and storm types."
              : `Drag to add a storm of up to ${data.whatif_rain_in[data.whatif_rain_in.length - 1]} in on every watershed, starting ${timeLabel(stormStart)}.`}
          </span>
        </div>
      )}
      <div className="filters">
        <input
          autoFocus
          placeholder="Search rivers and sections…"
          value={query}
          onChange={(e) => resetting(setQuery)(e.target.value)}
        />
        <select value={state} onChange={(e) => resetting(setState)(e.target.value)}>
          <option value="">All states</option>
          {states.map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
        <select value={minChance} onChange={(e) => resetting(setMinChance)(Number(e.target.value))}>
          {CHANCES.map((c) => (
            <option key={c.min} value={c.min}>
              {c.label}
            </option>
          ))}
        </select>
      </div>
      <p className="muted">
        {matches.length.toLocaleString()} of {reaches.length.toLocaleString()} forecast reaches
        {matches.length > 0 &&
          ` · showing ${(current * pageSize + 1).toLocaleString()}–${(current * pageSize + shown.length).toLocaleString()}`}
      </p>
      {pages > 1 && pager}
      <div className="table-scroll">
        <table className="outlook">
          <thead>
            <tr>
              <SortHeader column="river" label="River" sort={sort} onSort={onSort} />
              <SortHeader column="class" label="Class" sort={sort} onSort={onSort} />
              <SortHeader column="state" label="State" sort={sort} onSort={onSort} />
              <SortHeader
                column="runs"
                label={<span title="Share of days in range over the last 10 years">Runs</span>}
                sort={sort}
                onSort={onSort}
              />
              <th>Now</th>
              {data.days.map((day, n) => (
                <SortHeader key={day} className="day-col" column={`day:${n}`} label={weekday(day)} sort={sort} onSort={onSort} />
              ))}
              <SortHeader column="best" label="Best" className="day-col" sort={sort} onSort={onSort} />
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => (
              <tr key={r.id} onClick={() => (location.hash = `#/reach/${r.id}`)}>
                <td>
                  <a href={`#/reach/${r.id}`}>{r.river}</a>
                  {r.range_note && (
                    <span className="range-warning" title={RANGE_NOTES[r.range_note]}>
                      {" "}⚠
                    </span>
                  )}
                  <div className="muted small">{r.section}</div>
                </td>
                <td className="nowrap">{r.difficulty ? classLabel(r.difficulty) : ""}</td>
                <td>{stateCodes(r.states)}</td>
                <td className="nowrap muted" title="Share of days in range over the last 10 years">
                  {r.in_range_share !== null ? percent(r.in_range_share) : ""}
                </td>
                <td className="nowrap">
                  {flow(r.now_value, r.metric)}
                  <div>
                    <span className="chip small" style={{ background: BAND_COLORS[r.now_band] }}>
                      {BAND_LABELS[r.now_band]}
                    </span>
                  </div>
                </td>
                {data.days.map((day) => {
                  const d = r.days.find((x) => x.day === day);
                  const p = chance(r, day);
                  return (
                    <td
                      key={day}
                      className="day-cell"
                      style={p !== null ? { background: shade(p) } : undefined}
                      title={
                        d && !whatif
                          ? `Peak ~${flow(d.peak.p50, r.metric)}` +
                            (d.p_too_high_all_day >= 0.05 ? ` · ${percent(d.p_too_high_all_day)} too high all day` : "") +
                            (d.rain_in ? ` · rain through sunset: ${rainRange(d.rain_in)}` : "")
                          : ""
                      }
                    >
                      {p !== null ? (
                        <>
                          <strong>{percent(p)}</strong>
                          {d && !whatif && <BandBar bands={d.peak_bands} />}
                        </>
                      ) : (
                        <span className="muted">–</span>
                      )}
                    </td>
                  );
                })}
                <td className="day-cell">
                  <strong>{percent(best(r))}</strong>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {pages > 1 && pager}
      <p className="muted small">
        Bars show where each day's peak lands, too low → too high. Hover a cell for the expected peak and forecast rain. Later
        days are less certain: rain forecasts lose most of their skill after about 3 days.
      </p>
    </section>
  );
}
