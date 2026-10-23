// Pieces shared by the reach tables: state codes, class ordering, sortable headers, paging.

export const PAGE_SIZES = [50, 100, 250];

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

export function stateCodes(states: string[]): string {
  return states.map((s) => STATES[s] ?? s.replace(/^USA-/, "")).join(", ");
}

export function allStates(rows: { states: string[] }[]): string[] {
  return [...new Set(rows.flatMap((r) => r.states.map((s) => stateCodes([s]))))].sort();
}

const ROMAN: Record<string, number> = { I: 1, II: 2, III: 3, IV: 4, V: 5, VI: 6 };

/** Sortable difficulty: [lowest class, highest class], "+" and "−" as ±0.3. Standout
 * rapids don't count. Unrated reaches get null. */
export function classKey(raw: string | null): number[] | null {
  if (!raw) return null;
  const grades = [...raw.split("standout")[0].matchAll(/(VI|IV|V|I{1,3})(plus|minus)?/g)].map(
    ([, numeral, sign]) => ROMAN[numeral] + (sign === "plus" ? 0.3 : sign === "minus" ? -0.3 : 0),
  );
  return grades.length ? [grades[0], grades[grades.length - 1]] : null;
}

/** Compare by class; unrated reaches sort last whichever the direction. */
export function compareClass(a: string | null, b: string | null, sign: number): number {
  const [x, y] = [classKey(a), classKey(b)];
  if (!x || !y) return (x ? 0 : 1) - (y ? 0 : 1);
  return sign * (x[0] - y[0] || x[1] - y[1]);
}

/** Matches every word of the query against river and section. */
export function matchesQuery(row: { river: string; section: string }, query: string): boolean {
  const text = `${row.river} ${row.section}`.toLowerCase();
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((w) => text.includes(w));
}

export type Sort<C extends string> = { column: C; descending: boolean };

export function SortHeader<C extends string>({
  column,
  label,
  sort,
  onSort,
  className,
}: {
  column: C;
  label: React.ReactNode;
  sort: Sort<C>;
  onSort: (column: C) => void;
  className?: string;
}) {
  const active = sort.column === column;
  return (
    <th className={className} aria-sort={active ? (sort.descending ? "descending" : "ascending") : "none"}>
      <button className="sort" onClick={() => onSort(column)}>
        {label}
        <span className="arrow">{active ? (sort.descending ? "▼" : "▲") : "↕"}</span>
      </button>
    </th>
  );
}

export function Pager({
  page,
  pages,
  pageSize,
  onPage,
  onPageSize,
}: {
  page: number;
  pages: number;
  pageSize: number;
  onPage: (page: number) => void;
  onPageSize: (size: number) => void;
}) {
  return (
    <div className="pager">
      <button disabled={page === 0} onClick={() => onPage(page - 1)}>
        ← Previous
      </button>
      <span>
        Page{" "}
        <select value={page} onChange={(e) => onPage(Number(e.target.value))} aria-label="Page">
          {Array.from({ length: pages }, (_, i) => (
            <option key={i} value={i}>
              {i + 1}
            </option>
          ))}
        </select>{" "}
        of {pages}
      </span>
      <button disabled={page >= pages - 1} onClick={() => onPage(page + 1)}>
        Next →
      </button>
      <label className="muted">
        Show{" "}
        <select value={pageSize} onChange={(e) => onPageSize(Number(e.target.value))}>
          {PAGE_SIZES.map((n) => (
            <option key={n}>{n}</option>
          ))}
          <option value={100_000}>all</option>
        </select>{" "}
        per page
      </label>
    </div>
  );
}

/** The rows on page `page` (clamped to the last page). */
export function paged<T>(rows: T[], page: number, pageSize: number) {
  const pages = Math.max(1, Math.ceil(rows.length / pageSize));
  const current = Math.min(page, pages - 1);
  return { pages, current, shown: rows.slice(current * pageSize, (current + 1) * pageSize) };
}
