import { useMemo, useState } from "react";
import type { ReachSummary } from "./api";
import { useApi } from "./api";
import { classLabel, rangeLabel } from "./format";

const SHOWN = 200;

// American Whitewater's state codes, as postal abbreviations.
const STATES: Record<string, string> = {
  "USA-WVR": "WV",
  "USA-VRG": "VA",
  "USA-NCR": "NC",
  "USA-TNN": "TN",
  "USA-GEO": "GA",
  "USA-MRY": "MD",
  "USA-SCR": "SC",
  "USA-KEN": "KY",
  "USA-PEN": "PA",
  "USA-DOC": "DC",
  "USA-ALB": "AL",
  "USA-FLA": "FL",
};

function stateCodes(states: string[]): string {
  return states.map((s) => STATES[s] ?? s.replace(/^USA-/, "")).join(", ");
}

export default function ReachList() {
  const { data, error } = useApi<ReachSummary[]>("/api/reaches");
  const [query, setQuery] = useState("");
  const [state, setState] = useState("");

  const states = useMemo(
    () => [...new Set((data ?? []).flatMap((r) => r.states.map((s) => stateCodes([s]))))].sort(),
    [data],
  );
  const matches = useMemo(() => {
    const words = query.toLowerCase().split(/\s+/).filter(Boolean);
    return (data ?? []).filter((r) => {
      const text = `${r.river} ${r.section}`.toLowerCase();
      return words.every((w) => text.includes(w)) && (!state || stateCodes(r.states).includes(state));
    });
  }, [data, query, state]);

  return (
    <section>
      <div className="filters">
        <input autoFocus placeholder="Search rivers and sections…" value={query} onChange={(e) => setQuery(e.target.value)} />
        <select value={state} onChange={(e) => setState(e.target.value)}>
          <option value="">All states</option>
          {states.map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
      </div>
      {error && <p className="error">Couldn't load reaches: {error}</p>}
      {!data && !error && <p className="muted">Loading reaches…</p>}
      {data && (
        <>
          <p className="muted">
            {matches.length.toLocaleString()} of {data.length.toLocaleString()} reaches
            {matches.length > SHOWN && ` (showing the first ${SHOWN})`}
          </p>
          <table className="reaches">
            <thead>
              <tr>
                <th>River</th>
                <th>Section</th>
                <th>Class</th>
                <th>State</th>
                <th>Runnable range</th>
              </tr>
            </thead>
            <tbody>
              {matches.slice(0, SHOWN).map((r) => (
                <tr key={r.id} onClick={() => (location.hash = `#/reach/${r.id}`)}>
                  <td>
                    <a href={`#/reach/${r.id}`}>{r.river}</a>
                  </td>
                  <td>{r.section}</td>
                  <td>{r.difficulty ? classLabel(r.difficulty) : ""}</td>
                  <td>{stateCodes(r.states)}</td>
                  <td className="nowrap">
                    {r.metric && (r.range_min !== null || r.range_max !== null) ? rangeLabel(r.range_min, r.range_max, r.metric) : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}
