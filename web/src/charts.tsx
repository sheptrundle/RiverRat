// Hand-drawn SVG charts: the What-if hydrograph and the rain-response curve.
import type { BandName, Bands, DayCurve, WhatIf } from "./api";
import { BAND_NAMES } from "./api";
import { HOUR_MS, inches, percent, rainRange } from "./format";

const W = 900;
const PAD = { left: 64, right: 16, top: 14, bottom: 30 };

export const BAND_COLORS: Record<BandName, string> = {
  too_low: "var(--band-too-low)",
  low: "var(--band-low)",
  medium: "var(--band-medium)",
  high: "var(--band-high)",
  too_high: "var(--band-too-high)",
};

type Axis = { map: (v: number) => number; ticks: number[] };

function logAxis(lo: number, hi: number, top: number, bottom: number): Axis {
  const [a, b] = [Math.log(lo), Math.log(hi)];
  const ticks: number[] = [];
  for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++) {
    for (const m of [1, 2, 5]) {
      const t = m * 10 ** e;
      if (t >= lo && t <= hi) ticks.push(t);
    }
  }
  return { map: (v) => bottom - ((Math.log(Math.max(v, lo)) - a) / (b - a)) * (bottom - top), ticks };
}

function linearAxis(lo: number, hi: number, top: number, bottom: number, count = 5): Axis {
  const raw = (hi - lo) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const ticks: number[] = [];
  for (let t = Math.ceil(lo / step) * step; t <= hi + 1e-9; t += step) ticks.push(Number(t.toFixed(6)));
  return { map: (v) => bottom - ((v - lo) / (hi - lo)) * (bottom - top), ticks };
}

/** The value ranges of the five bands, unbounded ends as ±Infinity. */
function bandSpans(bands: Bands): [BandName, number, number][] {
  const [e0, e1, e2, e3] = bands.edges;
  const bounds = [-Infinity, e0 ?? -Infinity, e1 ?? Infinity, e2 ?? Infinity, e3 ?? Infinity, Infinity];
  return BAND_NAMES.map((name, i) => [name, bounds[i], bounds[i + 1]]);
}

function path(points: [number, number][]): string {
  return points.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join("");
}

export function Hydrograph({ whatif, height = 320 }: { whatif: WhatIf; height?: number }) {
  const { bands, observed, forecast, storm, days } = whatif;
  const top = PAD.top;
  const bottom = height - PAD.bottom;
  const obsStart = Date.parse(observed.start);
  const fcStart = Date.parse(forecast.start);
  const t0 = obsStart;
  const t1 = fcStart + (forecast.p50.length - 1) * HOUR_MS;
  const x = (t: number) => PAD.left + ((t - t0) / (t1 - t0)) * (W - PAD.left - PAD.right);

  const seen = observed.values.filter((v): v is number => v !== null);
  const range = [bands.minimum, bands.maximum].filter((v): v is number => v !== null);
  const all = [...seen, ...forecast.p10, ...forecast.p90, ...range];
  const [min, max] = [Math.min(...all), Math.max(...all)];
  const margin = 0.05 * (max - min || 1);
  const [lo, hi] = bands.metric === "cfs" ? [Math.max(min / 1.2, 1), max * 1.2] : [min - margin, max + margin];
  const y = bands.metric === "cfs" ? logAxis(lo, hi, top, bottom) : linearAxis(lo, hi, top, bottom);
  const clampY = (v: number) => y.map(Math.min(Math.max(v, lo), hi));

  const fcX = (h: number) => x(fcStart + h * HOUR_MS);
  const fan = [
    ...forecast.p90.map((v, h) => [fcX(h), clampY(v)] as [number, number]),
    ...forecast.p10.map((v, h) => [fcX(h), clampY(v)] as [number, number]).reverse(),
  ];
  const obsSegments: [number, number][][] = [[]];
  observed.values.forEach((v, i) => {
    if (v === null) obsSegments.push([]);
    else obsSegments[obsSegments.length - 1].push([x(obsStart + i * HOUR_MS), clampY(v)]);
  });

  // local midnights for the day ticks
  const midnights: number[] = [];
  const m = new Date(t0);
  m.setHours(24, 0, 0, 0);
  for (; m.getTime() < t1; m.setDate(m.getDate() + 1)) midnights.push(m.getTime());

  const stormStart = fcStart + storm.start_h * HOUR_MS;
  const stormEnd = stormStart + storm.duration_h * HOUR_MS;

  return (
    <svg viewBox={`0 0 ${W} ${height}`} className="chart" role="img" aria-label="Flow forecast">
      {bandSpans(bands).map(([name, a, b]) => {
        const yTop = clampY(Math.min(b, hi));
        const yBottom = clampY(Math.max(a, lo));
        if (a >= hi || b <= lo || yBottom - yTop < 0.5) return null;
        return (
          <rect key={name} x={PAD.left} width={W - PAD.left - PAD.right} y={yTop} height={yBottom - yTop} fill={BAND_COLORS[name]} opacity={0.35} />
        );
      })}
      {days.map((d) => (
        <rect key={d.day} x={x(Date.parse(d.sunrise))} width={x(Date.parse(d.sunset)) - x(Date.parse(d.sunrise))} y={bottom - 5} height={5} fill="var(--daylight)">
          <title>Daylight</title>
        </rect>
      ))}
      {y.ticks.map((t) => (
        <g key={t}>
          <line x1={PAD.left} x2={W - PAD.right} y1={y.map(t)} y2={y.map(t)} className="grid" />
          <text x={PAD.left - 6} y={y.map(t) + 4} textAnchor="end" className="tick">
            {bands.metric === "cfs" ? t.toLocaleString() : t}
          </text>
        </g>
      ))}
      {midnights.map((t) => (
        <g key={t}>
          <line x1={x(t)} x2={x(t)} y1={top} y2={bottom} className="grid" />
          <text x={x(t) + 4} y={bottom + 18} className="tick">
            {new Date(t).toLocaleDateString(undefined, { weekday: "short", day: "numeric" })}
          </text>
        </g>
      ))}
      {storm.rain_in > 0 && (
        <rect x={x(stormStart)} width={Math.max(x(stormEnd) - x(stormStart), 2)} y={top} height={8} fill="var(--rain)">
          <title>{`${inches(storm.rain_in)} storm`}</title>
        </rect>
      )}
      <path d={path(fan) + "Z"} fill="var(--fan)" />
      <path d={path(forecast.p50.map((v, h) => [fcX(h), clampY(v)]))} className="median" />
      {obsSegments.map((seg, i) => (seg.length ? <path key={i} d={path(seg)} className="observed" /> : null))}
      <line x1={x(fcStart)} x2={x(fcStart)} y1={top} y2={bottom} className="now" />
      <text x={x(fcStart) + 4} y={top + 22} className="tick">
        now
      </text>
      <text x={14} y={(top + bottom) / 2} transform={`rotate(-90 14 ${(top + bottom) / 2})`} textAnchor="middle" className="tick">
        {bands.metric}
      </text>
    </svg>
  );
}

export function ResponseCurve({
  rainIn,
  curve,
  rain,
  height = 240,
}: {
  rainIn: number[];
  curve: DayCurve;
  rain: number;
  height?: number;
}) {
  const top = PAD.top;
  const bottom = height - PAD.bottom;
  const maxRain = rainIn[rainIn.length - 1];
  const x = (r: number) => PAD.left + (Math.min(r, maxRain) / maxRain) * (W - PAD.left - PAD.right);
  const y = linearAxis(0, 1, top, bottom, 4);
  const line = (values: number[]) => path(values.map((p, i) => [x(rainIn[i]), y.map(p)]));
  const fc = curve.forecast_rain_in;

  return (
    <svg viewBox={`0 0 ${W} ${height}`} className="chart" role="img" aria-label="Chance of running by rain amount">
      {fc && (
        <g>
          <rect x={x(fc.p10)} width={Math.max(x(fc.p90) - x(fc.p10), 2)} y={top} height={bottom - top} fill="var(--rain)" opacity={0.18} />
          <line x1={x(fc.p50)} x2={x(fc.p50)} y1={top} y2={bottom} stroke="var(--rain)" strokeWidth={2} />
          <text x={x(fc.p50) + 4} y={top + 12} className="tick">
            forecast {rainRange(fc)}
          </text>
        </g>
      )}
      {y.ticks.map((t) => (
        <g key={t}>
          <line x1={PAD.left} x2={W - PAD.right} y1={y.map(t)} y2={y.map(t)} className="grid" />
          <text x={PAD.left - 6} y={y.map(t) + 4} textAnchor="end" className="tick">
            {percent(t)}
          </text>
        </g>
      ))}
      {rainIn.filter((r) => r % 0.5 === 0).map((r) => (
        <text key={r} x={x(r)} y={bottom + 18} textAnchor="middle" className="tick">
          {r} in
        </text>
      ))}
      <path d={line(curve.p_too_high_all_day)} className="too-high-line" />
      <path d={line(curve.p_runnable)} className="runnable-line" />
      {curve.rain_needed_in !== null && curve.rain_needed_in > 0 && (
        <g>
          <line x1={x(curve.rain_needed_in)} x2={x(curve.rain_needed_in)} y1={y.map(0.5) - 8} y2={bottom} className="needed" />
          <text x={x(curve.rain_needed_in) + 4} y={bottom - 6} className="tick">
            needs {inches(curve.rain_needed_in)}
          </text>
        </g>
      )}
      <line x1={x(rain)} x2={x(rain)} y1={top} y2={bottom} className="now" />
    </svg>
  );
}

/** A horizontal stacked bar of the day's peak-band chances. */
export function BandBar({ bands }: { bands: Record<BandName, number> }) {
  return (
    <div className="band-bar" title={BAND_NAMES.map((b) => `${b.replace("_", " ")} ${percent(bands[b])}`).join(" · ")}>
      {BAND_NAMES.map((b) => (
        <span key={b} style={{ width: `${bands[b] * 100}%`, background: BAND_COLORS[b] }} />
      ))}
    </div>
  );
}
