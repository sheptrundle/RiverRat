import { useMemo, useState } from "react";
import type { ReachSummary } from "./api";
import { useApi } from "./api";
import { classLabel, rangeLabel } from "./format";
import type { Sort } from "./tables";
import { PAGE_SIZES, Pager, SortHeader, allStates, compareClass, matchesQuery, paged, stateCodes } from "./tables";

type Column = "river" | "section" | "class" | "state";

/** Every reach in the catalog, forecastable or not. */
export default function ReachList() {
  const { data, error } = useApi<ReachSummary[]>("/api/reaches");
  const [query, setQuery] = useState("");
  const [state, setState] = useState("");
  const [sort, setSort] = useState<Sort<Column>>({ column: "river", descending: false });
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(PAGE_SIZES[0]);

  const states = useMemo(() => allStates(data ?? []), [data]);
  const matches = useMemo(() => {
    const found = (data ?? []).filter((r) => matchesQuery(r, query) && (!state || stateCodes(r.states).includes(state)));
    const sign = sort.descending ? -1 : 1;
    const byColumn = (a: ReachSummary, b: ReachSummary) => {
      switch (sort.column) {
        case "river":
          return sign * a.river.localeCompare(b.river);
        case "section":
          return sign * a.section.localeCompare(b.section, undefined, { numeric: true });
        case "state":
          return sign * stateCodes(a.states).localeCompare(stateCodes(b.states));
        case "class":
          return compareClass(a.difficulty, b.difficulty, sign);
      }
    };
    return found.sort(
      (a, b) =>
        byColumn(a, b) || a.river.localeCompare(b.river) || a.section.localeCompare(b.section, undefined, { numeric: true }),
    );
  }, [data, query, state, sort]);

  const { pages, current, shown } = paged(matches, page, pageSize);
  const onSort = (column: Column) => {
    setSort((s) => ({ column, descending: s.column === column ? !s.descending : false }));
    setPage(0);
  };
  const pager = (
    <Pager
      page={current}
      pages={pages}
      pageSize={pageSize}
      onPage={setPage}
      onPageSize={(n) => {
        setPageSize(n);
        setPage(0);
      }}
    />
  );

  return (
    <section>
      <p>
        <a href="#/">← Outlook</a>
      </p>
      <h2>All reaches</h2>
      <p className="muted">Every reach in the catalog, including those without a forecast (no USGS gauge with a runnable range, or too little history).</p>
      <div className="filters">
        <input
          autoFocus
          placeholder="Search rivers and sections…"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setPage(0);
          }}
        />
        <select
          value={state}
          onChange={(e) => {
            setState(e.target.value);
            setPage(0);
          }}
        >
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
            {matches.length > 0 &&
              ` · showing ${(current * pageSize + 1).toLocaleString()}–${(current * pageSize + shown.length).toLocaleString()}`}
          </p>
          {pages > 1 && pager}
          <table className="reaches">
            <thead>
              <tr>
                <SortHeader column="river" label="River" sort={sort} onSort={onSort} />
                <SortHeader column="section" label="Section" sort={sort} onSort={onSort} />
                <SortHeader column="class" label="Class" sort={sort} onSort={onSort} />
                <SortHeader column="state" label="State" sort={sort} onSort={onSort} />
                <th>Runnable range</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((r) => (
                <tr key={r.id} onClick={() => (location.hash = `#/reach/${r.id}`)}>
                  <td>
                    <a href={`#/reach/${r.id}`}>{r.river}</a>
                  </td>
                  <td>{r.section}</td>
                  <td className="nowrap">{r.difficulty ? classLabel(r.difficulty) : ""}</td>
                  <td>{stateCodes(r.states)}</td>
                  <td className="nowrap">
                    {r.metric && (r.range_min !== null || r.range_max !== null) ? rangeLabel(r.range_min, r.range_max, r.metric) : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {pages > 1 && pager}
        </>
      )}
    </section>
  );
}
