import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect, useMemo, useRef, useState } from "react";
import type { BandName, Outlook, OutlookReach } from "./api";
import { useApi } from "./api";
import { BAND_LABELS, classLabel, flow, flowNumber, percent, weekday } from "./format";
import { sectionLabel } from "./tables";

// GET /api/map (backend/src/riverrat/api/reach_map.py)
type MapReach = { id: number; river: string; section: string; difficulty: string | null; line: [number, number][] };

// "now": where each reach is right now; else a day of the Outlook
type View = "now" | string;

const STYLE_LIGHT = "https://tiles.openfreemap.org/styles/positron";
const STYLE_DARK = "https://tiles.openfreemap.org/styles/dark";
const NO_FORECAST = "#8f9893";
// Chance of running, gray (none) to deep green (near certain).
const CHANCE_STOPS: [number, string][] = [
  [0, "#a3aaa6"],
  [0.1, "#c4dfa0"],
  [0.5, "#4caf6a"],
  [0.9, "#17703a"],
];
const BAND_COLORS: Record<BandName, string> = {
  too_low: "#a3aaa6",
  low: "#8fcf86",
  medium: "#2e9e53",
  high: "#3b8fc4",
  too_high: "#d9534f",
};

function dark(): boolean {
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
}

/** Every reach on a map, colored by its chance of running on the chosen day. */
export default function MapPage() {
  const lines = useApi<{ reaches: MapReach[] }>("/api/map");
  const outlook = useApi<Outlook>("/api/outlook");
  const [view, setView] = useState<View>("now");
  const [selected, setSelected] = useState<number | null>(null);
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const [ready, setReady] = useState(false);

  const byId = useMemo(() => new Map((outlook.data?.reaches ?? []).map((r) => [r.id, r])), [outlook.data]);
  const days = outlook.data?.days ?? [];

  const features = useMemo<GeoJSON.FeatureCollection>(() => {
    const reaches = lines.data?.reaches ?? [];
    return {
      type: "FeatureCollection",
      features: reaches.map((r) => {
        const found = byId.get(r.id);
        const day = found?.days.find((d) => d.day === view);
        return {
          type: "Feature",
          id: r.id,
          geometry: { type: "LineString", coordinates: r.line },
          properties: {
            id: r.id,
            forecast: !!found,
            band: found?.now_band ?? null,
            p: day?.p_runnable ?? null,
          },
        };
      }),
    };
  }, [lines.data, byId, view]);

  // The map itself, once.
  useEffect(() => {
    if (!container.current) return;
    const m = new maplibregl.Map({
      container: container.current,
      style: dark() ? STYLE_DARK : STYLE_LIGHT,
      center: [-81.5, 36.8],
      zoom: 5.4,
      attributionControl: { compact: true },
    });
    m.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    m.addControl(new maplibregl.GeolocateControl({ positionOptions: { enableHighAccuracy: false } }), "top-right");
    m.on("load", () => {
      m.addSource("reaches", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      m.addLayer({
        id: "reach-casing",
        type: "line",
        source: "reaches",
        filter: ["get", "forecast"],
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": dark() ? "#121614" : "#ffffff",
          "line-width": ["interpolate", ["linear"], ["zoom"], 5, 3, 10, 6, 14, 10],
          "line-opacity": 0.9,
        },
      });
      // Reaches without a forecast: thin and dashed, under the forecast ones.
      m.addLayer({
        id: "reach-none",
        type: "line",
        source: "reaches",
        filter: ["!", ["get", "forecast"]],
        paint: {
          "line-color": NO_FORECAST,
          "line-opacity": 0.6,
          "line-width": ["interpolate", ["linear"], ["zoom"], 5, 1, 10, 2, 14, 3.5],
          "line-dasharray": [2, 2],
        },
      });
      m.addLayer({
        id: "reach-lines",
        type: "line",
        source: "reaches",
        filter: ["get", "forecast"],
        layout: { "line-cap": "round", "line-join": "round", "line-sort-key": ["coalesce", ["get", "p"], -1] },
        paint: { "line-width": ["interpolate", ["linear"], ["zoom"], 5, 1.8, 10, 4, 14, 7] },
      });
      m.addLayer({
        id: "reach-selected",
        type: "line",
        source: "reaches",
        filter: ["==", ["get", "id"], -1],
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": "#f2c94c",
          "line-width": ["interpolate", ["linear"], ["zoom"], 5, 5, 10, 9, 14, 13],
          "line-opacity": 0.85,
        },
      });
      m.moveLayer("reach-selected", "reach-lines");
      // A wide invisible line, so a thin river is easy to tap.
      m.addLayer({
        id: "reach-hit",
        type: "line",
        source: "reaches",
        paint: { "line-color": "#000", "line-opacity": 0, "line-width": 16 },
      });
      m.on("click", "reach-hit", (e) => setSelected(Number(e.features?.[0]?.properties?.id)));
      m.on("click", (e) => {
        if (!m.queryRenderedFeatures(e.point, { layers: ["reach-hit"] }).length) setSelected(null);
      });
      m.on("mouseenter", "reach-hit", () => (m.getCanvas().style.cursor = "pointer"));
      m.on("mouseleave", "reach-hit", () => (m.getCanvas().style.cursor = ""));
      setReady(true);
    });
    map.current = m;
    return () => {
      m.remove();
      map.current = null;
    };
  }, []);

  useEffect(() => {
    const m = map.current;
    if (!ready || !m) return;
    (m.getSource("reaches") as maplibregl.GeoJSONSource).setData(features);
  }, [ready, features]);

  // Frame the rivers the first time they arrive.
  const framed = useRef(false);
  useEffect(() => {
    const m = map.current;
    const reaches = lines.data?.reaches;
    if (!ready || !m || !reaches?.length || framed.current) return;
    const bounds = new maplibregl.LngLatBounds();
    for (const r of reaches) for (const point of r.line) bounds.extend(point);
    m.fitBounds(bounds, { padding: 24, animate: false });
    framed.current = true;
  }, [ready, lines.data]);

  useEffect(() => {
    const m = map.current;
    if (!ready || !m) return;
    const chance: maplibregl.ExpressionSpecification = [
      "interpolate",
      ["linear"],
      ["get", "p"],
      ...CHANCE_STOPS.flat(),
    ] as unknown as maplibregl.ExpressionSpecification;
    const band: maplibregl.ExpressionSpecification = [
      "match",
      ["get", "band"],
      ...Object.entries(BAND_COLORS).flat(),
      NO_FORECAST,
    ] as unknown as maplibregl.ExpressionSpecification;
    m.setPaintProperty(
      "reach-lines",
      "line-color",
      view === "now" ? band : ["case", ["==", ["get", "p"], null], NO_FORECAST, chance],
    );
  }, [ready, view]);

  useEffect(() => {
    const m = map.current;
    if (!ready || !m) return;
    m.setFilter("reach-selected", ["==", ["get", "id"], selected ?? -1]);
  }, [ready, selected]);

  const error = lines.error ?? outlook.error;
  const chosen = selected !== null ? lines.data?.reaches.find((r) => r.id === selected) : undefined;

  return (
    <section className="map-page">
      <div className="map-controls">
        <h2>Map</h2>
        <div className="segmented" role="radiogroup" aria-label="Show">
          <button className={view === "now" ? "on" : ""} role="radio" aria-checked={view === "now"} onClick={() => setView("now")}>
            Now
          </button>
          {days.map((d, i) => (
            <button key={d} className={view === d ? "on" : ""} role="radio" aria-checked={view === d} onClick={() => setView(d)}>
              {i === 0 ? "Today" : weekday(d)}
            </button>
          ))}
        </div>
        <Legend view={view} />
      </div>
      {error && <p className="error">Couldn't load the map: {error}</p>}
      <div className="map-frame">
        <div ref={container} className="map" />
        {(lines.loading || outlook.loading) && <div className="map-loading muted small">Loading rivers…</div>}
        {chosen && (
          <ReachCard reach={chosen} found={byId.get(chosen.id)} view={view} onClose={() => setSelected(null)} />
        )}
      </div>
      <p className="muted small">
        River courses from American Whitewater. Dashed: no forecast (no gauge, or a dam River Rat can't forecast). Tap a river
        for its predicted flows; the reach page has the full forecast.
      </p>
    </section>
  );
}

function Legend({ view }: { view: View }) {
  if (view === "now") {
    return (
      <div className="map-legend small" aria-label="Legend">
        {(Object.keys(BAND_COLORS) as BandName[]).map((b) => (
          <span key={b}>
            <i style={{ background: BAND_COLORS[b] }} />
            {BAND_LABELS[b]}
          </span>
        ))}
      </div>
    );
  }
  return (
    <div className="map-legend small" aria-label="Legend">
      <span>Chance it runs</span>
      <span className="ramp" style={{ background: `linear-gradient(to right, ${CHANCE_STOPS.map(([p, c]) => `${c} ${p * 100}%`).join(", ")})` }} />
      <span>0% → 90%+</span>
    </div>
  );
}

function ReachCard({
  reach,
  found,
  view,
  onClose,
}: {
  reach: MapReach;
  found: OutlookReach | undefined;
  view: View;
  onClose: () => void;
}) {
  const day = found?.days.find((d) => d.day === view);
  return (
    <div className="map-card panel" role="dialog" aria-label={reach.river}>
      <button className="map-card-close" aria-label="Close" onClick={onClose}>
        ×
      </button>
      <a href={`#/reach/${reach.id}`} className="map-card-title">
        {reach.river}
      </a>
      <div className="muted small">
        {sectionLabel({ section: reach.section, altname: found?.altname ?? "" })}
        {reach.difficulty ? ` · ${classLabel(reach.difficulty)}` : ""}
      </div>
      {found ? (
        <>
          <p className="small">
            Now: <strong>{flow(found.now_value, found.metric)}</strong>, {BAND_LABELS[found.now_band].toLowerCase()}
            {found.estimate ? " (estimated)" : ""}
          </p>
          <p className="small muted map-card-label">Predicted daytime peak ({found.metric === "ft" ? "ft" : "cfs"}, median)</p>
          <div className="map-card-days">
            {found.days.map((d, i) => (
              <span key={d.day} className={d.day === view ? "on" : ""} title={`${percent(d.p_runnable)} chance it runs`}>
                <span className="muted">{i === 0 ? "Today" : weekday(d.day)}</span>
                <strong>{flowNumber(d.peak.p50, found.metric)}</strong>
              </span>
            ))}
          </div>
          {day && <p className="small">{percent(day.p_runnable)} chance it runs {view === found.days[0]?.day ? "today" : `on ${weekday(day.day)}`}</p>}
          {day && day.p_too_high_all_day >= 0.05 && (
            <p className="small warning">{percent(day.p_too_high_all_day)} chance it's too high all day</p>
          )}
        </>
      ) : (
        <p className="small muted">No forecast for this reach.</p>
      )}
      <a href={`#/reach/${reach.id}`} className="small">
        Full forecast →
      </a>
    </div>
  );
}
