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
  difficulty: string | null;
  states: string[];
  gauge_site: string | null;
  metric: string | null;
  range_min: number | null;
  range_max: number | null;
};

export type ReachInfo = Pick<ReachSummary, "id" | "aw_id" | "river" | "section" | "difficulty">;
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
export type WhatIf = {
  reach: ReachInfo;
  gauge: GaugeInfo;
  bands: Bands;
  now: Now;
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
  difficulty: string | null;
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
};
export type Outlook = {
  computed_at: string | null;
  rain_issued_at: string | null;
  days: string[];
  whatif_storm_start: string | null;
  whatif_rain_in: number[];
  reaches: OutlookReach[];
};
