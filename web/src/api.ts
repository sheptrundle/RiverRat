// Types for the River Rat API (backend/src/riverrat/api/reaches.py) and a fetch hook.
import { useEffect, useState } from "react";

export type StormType = "burst" | "typical" | "soaker";
export const BAND_NAMES = ["too_low", "low", "medium", "high", "too_high"] as const;
export type BandName = (typeof BAND_NAMES)[number];

export type ReachSummary = {
  id: number;
  aw_id: number | null;
  river: string;
  section: string;
  altname: string;
  difficulty: string | null;
  next_release: string | null; // AW's next scheduled dam release, YYYY-MM-DD
  states: string[];
  gauge_site: string | null;
  metric: string | null;
  range_min: number | null;
  range_max: number | null;
};

export type ReachInfo = Pick<ReachSummary, "id" | "aw_id" | "river" | "section" | "altname" | "difficulty" | "next_release">;
export type GaugeInfo = { id: number; site: string; name: string; primary: boolean };
export type Bands = {
  metric: "cfs" | "ft";
  minimum: number | null;
  maximum: number | null;
  edges: (number | null)[]; // lower edges of low, medium, high, too high
};
export type Now = {
  at: string;
  cfs: number;
  value: number;
  band: BandName;
  ft_observed: number | null;
  api_mm: number;
  rain_hours: number;
};
export type Fan = { start: string; p10: number[]; p50: number[]; p90: number[] };
export type Series = { start: string; values: (number | null)[] };
export type Quantiles = { p10: number; p50: number; p90: number };
export type Day = {
  day: string;
  sunrise: string;
  sunset: string;
  p_runnable: number;
  peak_bands: Record<BandName, number>;
  p_too_high_all_day: number;
  peak: Quantiles;
  hours_in_range: number;
};
export type Analog = {
  start: string;
  rain_in: number;
  duration_h: number;
  q0_cfs: number;
  qpeak_cfs: number;
  t_peak_h: number;
  weight: number;
};
/** A reach without a gauge, estimated from a similar gauge nearby scaled by watershed area. */
export type Estimate = { area_sqmi: number; donor_area_sqmi: number; distance_km: number };

/** A reach below dams: their release plan, plus the undammed part's gauge (null: the dams
 * set all of the flow). basis: the Army Corps' hourly plan, TVA's daily plan for a peaking
 * powerhouse, TVA's recreation release calendar, or (no published plan) the dam holding its
 * recent pattern. */
export type Release = { dams: string[]; natural: string | null; basis: "plan" | "tva" | "schedule" | "held" };

/** What a reach's dam forecast is built from, in a few words. */
export const RELEASE_TAG = { plan: "Dam release plan", tva: "TVA release plan", schedule: "Release schedule", held: "Dam pattern" };

export function releaseSource(release: Release): string {
  const dams = release.dams.join(" and ");
  if (release.basis === "tva") return `TVA's daily release plan for ${dams}`;
  if (release.basis === "schedule") return `TVA's recreation release calendar for ${dams}`;
  if (release.basis === "held") return `${dams} publishes no release plan: its recent pattern, with the chance it changes`;
  return `Army Corps release plan for ${dams}`;
}

export type WhatIf = {
  reach: ReachInfo;
  gauge: GaugeInfo;
  estimate: Estimate | null;
  release: Release | null;
  bands: Bands;
  now: Now;
  history: { share_in_range: number; days: number; note: string | null } | null;
  observed: Series;
  storm: { rain_in: number; type: StormType; start_h: number; duration_h: number };
  forecast: Fan;
  days: Day[];
  analogs: Analog[];
  extrapolated: boolean;
};
export type DayCurve = {
  day: string;
  p_runnable: number[];
  p_reach: number[];
  p_too_high_all_day: number[];
  rain_needed_in: number | null;
  forecast_rain_in: Quantiles | null;
};
export type RainResponse = {
  rain_in: number[];
  peak_p10: number[];
  peak_p50: number[];
  peak_p90: number[];
  days: DayCurve[];
  forecast_issued_at: string | null;
};

export type Fetched<T> = { data: T | null; error: string | null; loading: boolean };

/** GET `url` (null = nothing to fetch). Keeps the last data while a new request runs, so
 * charts don't flash as sliders move, and drops responses to superseded requests. */
export function useApi<T>(url: string | null, delayMs = 0): Fetched<T> {
  const [state, setState] = useState<Fetched<T>>({ data: null, error: null, loading: !!url });
  useEffect(() => {
    if (!url) return;
    const controller = new AbortController();
    setState((s) => ({ ...s, loading: true }));
    const timer = setTimeout(() => {
      fetch(url, { signal: controller.signal })
        .then(async (res) => {
          if (!res.ok) {
            const body = await res.json().catch(() => null);
            throw new Error(body?.detail ?? `HTTP ${res.status}`);
          }
          return res.json() as Promise<T>;
        })
        .then((data) => setState({ data, error: null, loading: false }))
        .catch((err: Error) => {
          if (err.name !== "AbortError") setState({ data: null, error: err.message, loading: false });
        });
    }, delayMs);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [url, delayMs]);
  return state;
}

export type OutlookDay = {
  day: string;
  p_runnable: number;
  peak_bands: Record<BandName, number>;
  p_too_high_all_day: number;
  peak: Quantiles;
  hours_in_range: number;
  rain_in: Quantiles | null; // NBM basin rain from now through this day's sunset
};
export type OutlookReach = {
  id: number;
  aw_id: number | null;
  river: string;
  section: string;
  altname: string;
  difficulty: string | null;
  next_release: string | null;
  states: string[];
  gauge_site: string;
  primary: boolean;
  metric: "cfs" | "ft";
  range_min: number | null;
  range_max: number | null;
  now_value: number;
  now_band: BandName;
  as_of: string;
  extrapolated: boolean;
  days: OutlookDay[];
  whatif: { day: string; p_runnable: number[] }[] | null; // per amount in Outlook.whatif_rain_in
  in_range_share: number | null; // share of days in range over the last 10 years
  range_note: string | null; // "above_record" / "below_record": the range looks wrong
  estimate: Estimate | null; // no gauge: estimated through gauge_site
  release: Release | null; // below Army Corps dams, from their release plans
};
export type Outlook = {
  computed_at: string | null;
  rain_issued_at: string | null;
  days: string[];
  whatif_storm_start: string | null;
  whatif_rain_in: number[];
  reaches: OutlookReach[];
};

// GET /api/track-record
export type TrackLead = { lead_days: number; n: number; brier: number; brier_persistence: number; skill: number | null };
export type TrackBin = { low: number; high: number; n: number; said: number; ran: number };
export type TrackCall = {
  day: string;
  issued_at: string;
  reach_id: number;
  river: string;
  section: string;
  altname: string;
  p_runnable: number;
  observed: boolean;
};
export type TrackRecord = {
  first_day: string | null;
  last_day: string | null;
  scored: number;
  replayed: number;
  base_rate: number | null;
  leads: TrackLead[];
  reliability: TrackBin[];
  recent: TrackCall[];
};

// GET /api/reaches/{id}/releases
export type DamRelease = {
  name: string;
  agency: "USACE" | "TVA";
  now_cfs: number | null;
  now_at: string | null;
  recent: Series; // hourly release, cfs, the last 48 hours
  forecast: { day: string; cfs: number }[]; // the operator's forecast, averaged per day
  notice: { since: string; message: string } | null;
};
export type ReachReleases = { reach: ReachInfo; dams: DamRelease[] };
