import type { BandName } from "./api";

export const BAND_LABELS: Record<BandName, string> = {
  too_low: "Too low",
  low: "Low",
  medium: "Medium",
  high: "High",
  too_high: "Too high",
};

export const HOUR_MS = 3_600_000;

export function flow(value: number, metric: string): string {
  if (metric === "ft") return `${value.toFixed(2)} ft`;
  const rounded = value >= 1000 ? Math.round(value / 10) * 10 : Math.round(value);
  return `${rounded.toLocaleString()} cfs`;
}

export function percent(p: number): string {
  if (p > 0.99) return ">99%";
  if (p > 0 && p < 0.01) return "<1%";
  return `${Math.round(p * 100)}%`;
}

export function inches(value: number): string {
  return `${value.toFixed(2)} in`;
}

/** Days are labeled by the date at solar noon, which is the UTC date in the US. */
export function dayLabel(day: string): string {
  const date = new Date(`${day}T12:00:00Z`);
  return date.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });
}

export function weekday(day: string): string {
  return new Date(`${day}T12:00:00Z`).toLocaleDateString(undefined, { weekday: "short", timeZone: "UTC" });
}

export function timeLabel(ms: number): string {
  return new Date(ms).toLocaleString(undefined, { weekday: "short", hour: "numeric" });
}

export function dateLabel(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

export function awUrl(awId: number): string {
  return `https://www.americanwhitewater.org/content/River/view/river-detail/${awId}/main`;
}

/** AW's difficulty, e.g. "IVtoV" → "IV–V", "IIIplusstandoutV" → "III+ (V)". */
export function classLabel(raw: string): string {
  return raw
    .replace(/standout(\S+)/, " ($1)")
    .replace(/plus/g, "+")
    .replace(/minus/g, "−")
    .replace(/to/g, "–");
}

/** A runnable range, e.g. "500–2,500 cfs"; "…" for a missing bound. */
export function rangeLabel(low: number | null, high: number | null, metric: string): string {
  const number = (v: number | null) => (v === null ? "…" : flow(v, metric).replace(/ (cfs|ft)$/, ""));
  return `${number(low)}–${number(high)} ${metric}`;
}

/** A forecast rain range, or "dry" when it's (nearly) zero. */
export function rainRange(q: { p10: number; p50: number; p90: number }): string {
  if (q.p90 < 0.005) return "dry";
  return `${inches(q.p50)} (${inches(q.p10)}–${inches(q.p90)})`;
}

/** Why a range looks wrong for its gauge (forecast/history.py notes). */
export const RANGE_NOTES: Record<string, string> = {
  above_record:
    "This gauge almost never gets as high as this range. It may be a flood-only run, or AW's range may be meant for a different gauge.",
  below_record:
    "This gauge almost never gets as low as this range. AW's range may be meant for a different gauge or datum.",
};
