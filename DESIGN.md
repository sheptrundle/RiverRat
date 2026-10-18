# River Rat — Design Document

> **Status:** v0.1 draft · 2026-10-18 · Owner: sheptrundle
> This is a living document. Update the Decision Log (§15) whenever a choice changes.

## 1. Vision
River Rat answers one question for whitewater paddlers: **"Will my river be runnable on the day I want to go?"** It answers up to 6 days ahead, with an honest probability, so you don't have to guess the morning of.

It combines four things:
1. A catalog of US whitewater reaches and their runnable flow bands (from American Whitewater).
2. Live gauge readings (USGS) and NOAA's river forecasts.
3. Observed and forecast precipitation over each river's **watershed**.
4. A model that learned from 20+ years of history how each gauge responds to rain, given the river's current condition.

### 1.1 MVP user stories
- **Outlook (rolling, today through day 5, so 6 days max):** "It's Wednesday. Which rivers have a real chance of being in on any of the next few days?" The answer is a ranked table with a probability per day, broken down by flow band. You can focus on specific days (e.g., Saturday) or on the next N days. The cap is 6 days because rain forecasts beyond about 5 days are barely better than climatology (§6.3).
- **What-if rain:** "If the basin gets 1.5 in. Friday night, what happens?" The answer is a predicted hydrograph plus the past events that looked like this one.

### 1.2 Later
- A map of reaches colored by status
- Favorites and alerts
- A dam-release calendar
- More regions
- A public launch

### 1.3 Non-goals
- **Not an official forecast or a safety tool.** Every page carries a disclaimer and links to NWS and AW.
- No trip reports or social features.
- No native apps; the site is desktop-first web.
- No Canada for now.

## 2. Guiding principles
1. **Predict at gauges, present at reaches.** A gauge has a history we can model. A reach is what a paddler cares about, and it maps to a gauge plus flow bands.
2. **Separate "how much rain" from "how the river responds."**
   - The rain amount comes from NOAA forecasts. It is uncertain, and improving it is not our job.
   - The river's response is learned from history. This is our core work.
   - Combining the two through scenarios gives probabilities, and the What-if slider comes almost for free.
3. **Probabilities, not promises.** Every forecast is a distribution. When we say 70%, it must happen about 70% of the time, and we measure that.
4. **Earn the headline.** Our model is the main forecast for a gauge only where hindcasts show it beats persistence, climatology, and NOAA.
5. **One feature pipeline for training and serving.** The same code builds features in both places, so the model never sees different inputs live than it saw in training.
6. **Every external source sits behind an adapter.** APIs change: USGS's legacy API dies in Q1 2027, and AW's site was rebuilt in 2025–26.
7. **Cheap, boring infrastructure.** One small VPS, Postgres, and cron. Heavy work such as backfills and training runs on the laptop.
8. **Be a good citizen.** Cache responses, rate-limit, identify ourselves, and attribute AW, USGS, and NOAA.

## 3. Core insight: there are three kinds of rivers
Rivers don't all respond to rain the same way. Every gauge is classified into a **regime**, and each regime is handled differently.

| Regime | Typical basin | Response to rain | What predicts flow days ahead | Pilot examples |
|---|---|---|---|---|
| **A. Flashy (rain-driven)** | < ~500 mi² | Rises in hours, peaks within a day, drops in 1–3 days | Forecast rain (QPF). Skill is limited by the weather forecast. | Southern Appalachian creeks |
| **B. Routed (large)** | ~500–10,000 mi² | Rises over 1–4 days, with long recessions | Water already upstream (upstream gauges), then recent rain, then QPF | James at Richmond (~6,700 mi²), New at Thurmond (~6,700 mi²), Cheat |
| **C. Regulated** | Any basin below a dam | Set by dam operators; rain matters only indirectly | Release schedules and reservoir operations, plus undammed tributaries | Gauley (Summersville Dam), Ocoee (TVA), Nantahala, Russell Fork |
| D. Snowmelt (later) | Western | Melt driven by temperature | Snowpack (SWE) plus temperature forecast | Not in the pilot |

The owner's reference rivers cover all three regimes:
- **James:** Regime B.
- **New:** Regime B, partly C. Bluestone Dam sits above it, and the undammed Greenbrier joins it.
- **Gauley:** Regime C.

**What each regime implies**
- **Regime A:** Lead time is capped by rain-forecast skill, so uncertainty is wide on days 3–5. Use hourly resolution, and give rain intensity real weight.
- **Regime B:** Good news for planning days ahead. By Wednesday, much of Saturday's water is already in the system. Upstream gauges are the best predictors. NOAA River Forecast Center (RFC) forecasts usually exist and set a strong bar to beat.
- **Regime C:** Show scheduled releases as near-certain (AW provides `nextRelease`). For unscheduled operations, model only the **intervening** watershed below the dam and treat the dam's outflow as an input.

**Classification** is automatic, with a manual override. It uses:
- drainage area
- the Richards-Baker flashiness index, computed from history
- upstream dams, from NLDI navigation plus the National Inventory of Dams
- USGS regulation remarks
- the AW gauge type (`CALCULATED` virtual gauges are often dam outflows)

## 4. Data sources
Everything below was checked as live on 2026-10-18 unless marked *(verify in spike)*.

### 4.1 Reaches: American Whitewater (scraped)
- **AW has no public API.** The legacy `/content/River/detail/id/{id}/.json` endpoints and `/graphql` no longer return data since the site was rebuilt on Next.js.
- Reach pages at `/content/River/view/river-detail/{id}/main` embed structured data in the Next.js payload (`self.__next_f.push(...)`). Here is an excerpt from the Upper Gauley page (id 2378):
  ```json
  "stub": {"id":"2378","river":"Gauley","section":"...","difficulty":"IVtoV","states":["USA-WVR"]},
  "geometry": {"type":"LineString","coordinates":[[...]]},
  "length": 12, "averageGradient": 36,
  "correlations": [{"isPrimary": true,
    "gaugeInfo": {"id":"36510909","name":"Gauley at Summersville (cfs)","source":"CALCULATED"},
    "correlationDetails": {"data": {"metric":"cfs","beginLowRunnable":400,"endHighRunnable":5000}},
    "latestReading": {"value":2755,"dateTime":"...","qualifier":"NORMAL"},
    "status": {"status":"medium-runnable"}}],
  "nextRelease": {"date":"2027-09-16","link":"..."},
  "alerts": [ ... ]
  ```
- `robots.txt` allows these pages. §10 covers the scraper design and the permission plan.
- Gauge `source` values seen so far:
  - `USGS` (Lower Gauley → USGS 03192000).
  - `CALCULATED`: AW's own virtual gauges, which must be mapped to real sources by hand.
  - Other types such as TVA, USACE, and utilities are likely *(verify in spike)*.

### 4.2 Gauges: observations and NOAA flow forecasts
| Source | What we use it for | Access | Notes |
|---|---|---|---|
| **USGS Water Data OGC API** `api.waterdata.usgs.gov/ogcapi/v0` | Live and historical discharge (param 00060, cfs) and stage (00065, ft); site metadata; drainage area | Collections `continuous`, `latest-continuous`, `daily`, `monitoring-locations`, `time-series-metadata`, `peaks`, `field-measurements`. A free API key gives higher rate limits (see the `X-RateLimit-*` headers). Python client: the `dataretrieval` **`waterdata`** module. | **Legacy `waterservices.usgs.gov` is decommissioned in Q1 2027, with degradation from Nov 2026. Never build on it.** How far back `continuous` goes is unknown *(verify)*; fall back to `daily` for old years. |
| **NOAA NWPS API** `api.water.noaa.gov/nwps/v1` | Observed flow plus the **official RFC forecast** (`/gauges/{id}/stageflow`); rating curves (`/gauges/{id}/ratings`); gauges upstream and downstream (`/gauges/{id}/riverflow`); **NWM forecasts per reach** (`/reaches/{reachId}/streamflow?series=short_range\|medium_range\|medium_range_blend\|long_range\|analysis_assimilation`) | REST/JSON. Accepts an NWS location ID or a USGS ID. | **No history.** We archive its forecasts ourselves from Phase 1. |
| **NWM Retrospective v3.0** (AWS Open Data) | Simulated NWM flow for every reach, 1979–2023. Lets us bias-correct NWM without waiting for our own archive. | Zarr/NetCDF on S3 | NWM v3.1 was scheduled for operations in Aug 2026; record the model version. |
| **USACE CWMS Data API** `cwms-data.usace.army.mil/cwms-data` | Reservoir outflow and pool level for regulated runs (Summersville, Bluestone, Gathright…) | REST | Phase 5. TVA and utility (Duke, Brookfield) schedules come in the same phase. |

### 4.3 Precipitation: observed
| Product | Resolution | Period | Role |
|---|---|---|---|
| **Stage IV** (NCEP radar + gauge mosaic, quality-checked by the RFCs) | 4 km, hourly | 2002 → real time (NOMADS, about 1–2 h latency; archive at NCAR EOL) | **The canonical product for both training and live runs**, so the model sees the same rain data in both |
| MRMS multi-sensor QPE (AWS `noaa-mrms-pds`) | 1 km, hourly (updated every 2 min) | ~2020 → real time | Later upgrade for small basins, and to fill the last 1–2 hours |
| AORC v1.1 (AWS Zarr) | ~1 km, hourly | 1979 → recent | Training fallback if the Stage IV backfill is painful. Since 2002 its precipitation is derived from Stage IV. |

### 4.4 Precipitation: forecast
| Product | Role |
|---|---|
| **NBM v4.x** (National Blend of Models): 2.5 km, out to about 11 days, includes **probabilistic QPF percentiles** | **Primary scenario source**, because it gives calibrated rain uncertainty. Its AWS archive goes back to about 2020 *(verify)*, which enables realistic hindcasts. |
| HRRR: 3 km, 0–48 h | Phase 6: sharper detail on convective storms for days 0–2 |
| GEFS: 31 members, 16 days | Phase 6: physically consistent multi-day scenarios |
| NWS API `api.weather.gov` gridpoint QPF | Display convenience only (point forecasts) |

Gridded products are fetched with **Herbie**. It downloads only the precipitation fields from the GRIB2 files on AWS (byte-range requests), so bandwidth stays tiny.

### 4.5 Geospatial and other inputs
- **USGS NLDI** (`api.water.usgs.gov/nldi`):
  - `/linked-data/nwissite/USGS-{id}/basin` returns the watershed polygon upstream of a gauge (tested and working).
  - `/navigation/{UM|UT|DM}/...` returns upstream tributaries and upstream gauges.
  - A position query by point returns the NHDPlus **COMID**. **The NWM `feature_id` is the NHDPlusV2 COMID**, which is how gauges and reaches link to NWM forecasts.
  - It also serves catchment characteristics (slope, soils, land cover) that we use as static model features.
- **NHDPlus V2** flowlines and catchments give the flow-path length to the outlet, used for travel-time bands.
- **National Inventory of Dams:** upstream dams set the regulation flag.
- **Temperature** (for rain vs. snow and for evapotranspiration): NBM for forecasts; AORC or PRISM for history.
- **Snow** (SNODAS): only in Phase 6, for the West.

## 5. Which precipitation matters for a given river?
**Short answer:** the rain that falls on the gauge's **watershed**, meaning every square mile that drains to that gauge. It is weighted by area, and its timing depends on how far the water has to travel. It is *not* the rain near the river or at the nearest town.

A reach is predicted through its correlated gauge, and AW's flow bands are already defined in terms of that gauge. So the relevant rain for a section is the rain over **that gauge's** watershed. For ungauged reaches (later), delineate the watershed from the reach's take-out point instead.

### 5.1 Pipeline
1. **Delineate** the watershed upstream of each modeled gauge (NLDI `basin`). Check its area against the USGS published drainage area and flag mismatches over 10%. For non-USGS points (virtual gauges, put-ins), use NLDI by COMID (split catchment) or USGS StreamStats.
2. **Compute weights.** Overlay the basin on each rain grid (Stage IV, NBM, …) and compute each grid cell's exact fractional coverage with `exactextract`. Store the weights once per (grid, basin). Basin rain for any hour is then a sparse dot product, `P_basin(t) = Σ wᵢ · Pᵢ(t)`, which handles thousands of basins in milliseconds.
3. **Split into travel-time bands.** Divide each basin into zones by flow distance to the gauge, using NHDPlus path length (for example near, middle, and far thirds). Rain near the gauge arrives first and headwater rain arrives later, and the model learns each band's timing. Basins under about 100 mi² use a single band.
4. **For regulated rivers, use the intervening area only.** Rain above a dam fills the reservoir, not the river. The relevant area is basin(gauge) minus basin(dam), and the dam's outflow becomes an input. Example: the New River at Thurmond = Bluestone Dam outflow (routed downstream) + the Greenbrier (undammed) + local inflow.
5. **Treat upstream gauges as rain already turned into flow.** For Regime B, NLDI upstream navigation finds tributary and mainstem gauges. Their lagged flows are the strongest predictors (e.g., James at Cartersville → James at Richmond).
6. **Add elevation bands** (later) to split rain from snow in the WV highlands and the West.

### 5.2 Time windows that matter
- **Event rain** (the current or forecast storm) drives the peak.
- **Antecedent rain** (the last 1–30 days) sets how wet the ground is, which decides how much of the storm becomes runoff.
- **Season** matters on its own. The same rain at the same level makes less runoff in July, when trees are in leaf and drawing water, than in March.

### 5.3 Validation
Each event's runoff ratio (runoff volume ÷ basin rain volume) must be physically plausible, roughly 0.05–0.8. Outliers point to a bad delineation, the wrong gauge mapping, or regulation we haven't flagged.

## 6. Prediction design

### 6.1 The owner's idea, formalized
> "How did the gauge react when it got X rain at Y current level?"

This is an **empirical rainfall–runoff model**, and it's the right core. Three refinements make it work:
1. **Y (current level) is only a proxy for how wet the basin is.** Add **antecedent rain** and **season**, because the same level behaves differently in summer than in spring.
2. **X (rain) must be basin-averaged** (§5), and its timing and intensity matter. One inch in 1 hour is not the same as one inch over 24 hours.
3. **In history we know the rain exactly; for the future we only have a *forecast* of X.** So we learn the response from history, then run many plausible future rain scenarios through it to get a probability.

### 6.2 Anatomy of a forecast
```
             ┌────────────────── current state ──────────────────┐
             │ flow now, trend, antecedent rain, season,         │
             │ upstream gauges, dam outflow                      │
             └─────────────────────────┬─────────────────────────┘
NBM QPF percentiles ─▶ S rain scenarios ─▶ Response model M ─▶ S×K flow trajectories ─▶ calibration ─▶ reach products
(What-if: 1 user scenario)               (learned from history)  (6 days / 144 h, hourly) (isotonic)
```
- **Response model M**
  - Input: the state at t₀ plus the basin rain trajectory for the next 144 h, by band.
  - Output: quantiles of flow at each horizon.
  - It is trained on *observed* rain ("perfect forecast" training), so it learns only the hydrology.
- **Rain scenarios**
  - Built from NBM probabilistic QPF: basin-averaged percentiles for each 6-hour period, giving about 20–50 trajectories (S).
  - *Approximation 1:* the basin average of grid percentiles is treated as the percentile of the basin average.
  - *Approximation 2:* the 6-hour periods are treated as rank-correlated (a wet period implies wet neighbors).
  - The calibration layer absorbs the resulting error. GEFS members replace this method in Phase 6.
- **Hydrologic uncertainty**
  - For each scenario, draw K quantile levels `u ~ U(0,1)` and hold each `u` fixed across all horizons.
  - This gives smooth, internally consistent trajectories. It's a cheap stand-in for a full copula.
- **Calibration layer:** isotonic regression maps raw ensemble probabilities to observed frequencies. It is fit on hindcasts, separately for each regime.
- **What-if:** the same model run on one user-defined rain scenario.
- **Rain needed:** the model run in reverse. Scale the forecast's timing pattern up or down to find the smallest basin rain total that gives at least a 50% chance of reaching the low band.

### 6.3 Forecast products (per reach, per day)
Owner decisions:
- A river counts as *runnable if it is in range at any point during the day*, using Low/Med/High bands.
- The Outlook is a **rolling window of today plus up to 5 days (6 days max)**, not weekend-only. The cap is a config value, `OUTLOOK_MAX_DAYS = 6`.

**Why 6 days**
- Rain-forecast skill drops steeply after day 3, and beyond about day 5 it's close to climatology.
- Days 4–6 are mostly driven by recession and very uncertain rain.
- The UI fades confidence by lead time and labels those days "low confidence."

**Per-day products**
- **Days covered:** each local calendar day from today through `today + OUTLOOK_MAX_DAYS − 1`.
- **Window:** each day's daylight hours, local time (sunrise to sunset at the put-in). For today, the window covers only the remaining daylight.
- **From each trajectory**, record:
  - the **highest band reached** during the window
  - whether the flow was in range at any hour
- **Outputs:**
  - **`P(runnable at any point)`**: the headline number.
  - **Band distribution of the day's peak:** `P(too low)`, `P(low)`, `P(medium)`, `P(high)`, `P(too high)`, shown as a stacked bar.
  - A `P(too high all day)` warning.
  - The expected peak (p10 / p50 / p90) and when it occurs.
  - The median number of hours in range.
  - **Rain needed vs. forecast rain**, e.g. "needs about 1.2 in.; forecast is 0.4–1.6 in."
  - **Drivers**, in plain text: e.g. "Depends mostly on Friday night's storm," or "Still dropping from Tuesday's high water; recession alone keeps it in through Saturday."
- **Regulated reaches** show the release calendar instead of a rain-needed estimate.

### 6.4 Model ladder: build in order; each level must beat the one before it in hindcasts
| Level | Model | What it is | Why |
|---|---|---|---|
| **M0** | Baselines | Persistence; **recession only** (fitted exponential decay, i.e. the "no more rain" scenario); day-of-year climatology; **NOAA** NWM (bias-corrected by quantile-mapping NWM Retrospective against observed flow) and official RFC forecasts | The bar to beat. Recession alone already answers "will it still be in on Saturday?" |
| **M1** | **Analog / event model** | Extract each gauge's past rain events and responses: start flow Q₀, rain P, intensity, duration, antecedent index, season, peak Qp, time to peak. Find the k events nearest to the current state plus scenario, and scale their normalized hydrographs. | It's directly the owner's idea, and it is **interpretable** ("14 similar past events peaked at 2,100–6,800 cfs"). It powers the What-if explanations. It is weak at extrapolating and at back-to-back storms. |
| **M2** | **Pooled quantile gradient boosting** (LightGBM) | One regional model across all pilot gauges, trained on forecast issue times every 6 h through history. Features are listed in §6.5. Targets are `log(Q(t₀+h)/Q₀)` for h ∈ {3, 6, 12, 24, 36, 48, 72, 96, 120, 144} h, with quantile loss from p05 to p95. | Probabilistic, fast, and robust. Pooling helps gauges with short records, and the NWM forecast can be a feature. **Expected to be the primary model.** |
| **M3** | **LSTM** (NeuralHydrology, multi-timescale) | A sequence model on hourly and daily inputs plus static basin attributes, trained on all basins jointly | State of the art in large-sample hydrology (Kratzert et al. 2019; Google Flood Hub). A research track once M2 is solid. |

For each gauge, the displayed forecast is the best model by hindcast CRPS, or a skill-weighted blend of models. M1 stays on as the explanation engine even if M2 or M3 wins.

### 6.5 Feature catalog
| Group | Features |
|---|---|
| State | `log Q₀`; 3 / 6 / 24 h trend; Q 24 h and 72 h ago; baseflow (Lyne–Hollick filter); hours since the last peak |
| Antecedent | Basin rain sums over 6 h, 24 h, 72 h, 7 d, and 30 d; antecedent precipitation index (daily decay ~0.9); optional NWM soil moisture |
| Future rain (by band) | Sums for 0–6, 6–12, 12–24, 24–48, 48–72, 72–96, 96–120, and 120–144 h; maximum 1-h and 3-h intensity |
| Season / temperature | Day-of-year sine and cosine; 7-day mean temperature; freezing flag (winter, ice) |
| Network | Upstream gauge flows at lags; dam outflows (regulated rivers) |
| NOAA | NWM forecast at horizon h (bias-corrected); RFC forecast where one exists |
| Static | Drainage area, slope, elevation, forest %, soil permeability (NLDI characteristics), flashiness index, baseflow index, regime |

### 6.6 Training data
- **One hourly aligned table per gauge:**
  - flow: the hourly mean of 15-minute data, or daily values for old years and big basins
  - basin rain by band: Stage IV, 2002 onward
  - temperature
- **Data quality:**
  - Drop readings flagged as ice-affected or equipment malfunction.
  - Prefer approved data over provisional.
  - Model **discharge (cfs)** as the canonical variable. Use stage (ft) only where no rating curve exists, and convert AW's ft-based ranges with NWPS or USGS ratings.
- **Split by time, never randomly.** Neighboring hours are correlated, so a random split leaks answers into training.
  - Train on ≤ 2019, validate on 2020–2022, and test on 2023 through the present.
  - Also hold out some gauges entirely (a **spatial holdout**) to test whether the pooled model generalizes.
- **Storage:** history lives in Parquet files (partitioned by gauge and year) and is queried with DuckDB. It stays on the laptop, not the server.

### 6.7 Evaluation: how we know it works
- **Two hindcast modes:**
  - *(a) Perfect rain:* use observed rain as the "forecast." This measures pure hydrologic skill.
  - *(b) Real forecast:* use archived NBM forecasts (2020 onward). This shows what a user would actually have seen.
- **Metrics:**
  - **CRPS** (the standard score for probabilistic forecasts) and skill against persistence, climatology, and NWM.
  - **Brier score and reliability diagrams** for `P(runnable)`: does 70% happen 70% of the time?
  - Hit and false-alarm rates for "comes in" at 1–5 days of lead time.
  - Error in peak size and timing.
  - NSE and KGE (standard hydrology scores) for perfect-rain hydrographs.
- **Track record:** every reach page shows how its forecasts have performed, for trust and honesty.
- **Start archiving** NBM basin QPF, NWM, and RFC forecasts **in Phase 1**. Every day archived is free evaluation data later.

### 6.8 Regime-specific handling
- **A (flashy):** hourly everything, plus intensity features. Expect forecasts from day 3 on to be dominated by rain uncertainty.
- **B (routed):** 3-hourly or daily resolution is fine. Use upstream-gauge features. RFC and NWM forecasts are strong baselines, and we must beat them.
- **C (regulated):**
  1. Show scheduled releases (AW `nextRelease`, plus TVA, USACE, and utility calendars) as near-certain.
  2. Otherwise, model the intervening area and use persistence, or a forecast, of the dam outflow (from CWMS).
  3. Always flag these forecasts as lower confidence.

## 7. System architecture
```
External                       Python jobs (cron / systemd timers)    Storage                     Serving
AW reach pages ── weekly ────▶ sync_reaches ───────┐
USGS OGC API ─── 15 min ─────▶ ingest_gauges ──────┤      ┌──────────────────────┐
NWPS (RFC+NWM) ─ hourly ─────▶ ingest_noaa_flow ───┼────▶ │ PostgreSQL + PostGIS │ ◀── FastAPI ──▶ React + TS (Vite)
Stage IV ─────── hourly ─────▶ ingest_precip_obs ──┤      └──────────▲───────────┘     (JSON)
NBM (Herbie) ─── hourly ─────▶ ingest_precip_fcst ─┘                 │
                                     every 3 h ▶ run_forecasts ──────┘  (loads model artifacts)

Laptop: backfill_history → Parquet + DuckDB → train / hindcast → model artifacts ──rsync──▶ VPS
```

**Backend package (`riverrat`)**
- `sources/`: one adapter per external source (`aw`, `usgs`, `nwps`, `stage4`, `mrms`, `nbm`, `cwms`). Each adapter fetches, validates with Pydantic, and upserts idempotently. Raw responses are cached so runs can be replayed and tests can use real data.
- `geo/`: watershed delineation, grid weights, and travel-time bands.
- `features/`: the **single** feature pipeline, used by both training and serving.
- `models/`: M0–M3 behind one interface: `fit(dataset)` and `predict(state, scenarios) → Ensemble`.
- `forecast/`: rain scenarios, the ensemble, calibration, and reach products.
- `jobs/`: command-line entry points, e.g. `python -m riverrat.jobs.ingest_gauges`. Every run is logged to `job_run`.
- `api/`: a read-only FastAPI app.

**Scheduling:** cron or systemd timers; no Airflow or Dagster. Failures ping healthchecks.io (free tier).

**Hosting ($0–10/mo)**
- One small VPS (e.g., Hetzner, ~2 vCPU and 4 GB RAM, about $5–8/mo) runs Docker Compose with four services:
  - `postgres` (with PostGIS)
  - `api`
  - `worker`
  - `caddy`, which handles TLS and serves the built frontend
- A nightly `pg_dump` goes to the free tier of Cloudflare R2 or Backblaze B2.
- The server keeps only recent observations (~60 days), current forecasts, and the forecast-summary archives. Deep history stays on the laptop.
- A domain costs about $10/yr.
- Local development uses the same Compose file.

**Load check:** about 300–600 pilot gauges × 50 trajectories × 144 hours with LightGBM takes seconds to minutes on 2 vCPUs.

## 8. Data model (PostgreSQL + PostGIS)
| Table | Key columns |
|---|---|
| `reach` | `id`, `aw_id` (unique), `river`, `section`, `altname`, `difficulty`, `length_mi`, `gradient_fpm`, `geom` (LineString), `states[]`, `aw_updated_at`, `scraped_at`, `source` (`aw`/`manual`) |
| `gauge` | `id`, `source` (`USGS`/`NWS`/`USACE`/`TVA`/`AW_CALC`/`VIRTUAL`), `source_id`, `name`, `geom` (Point), `drainage_area_sqmi`, `params`, `nws_lid`, `nwm_feature_id` (COMID), `regime`, `regulation_notes`, `active` |
| `virtual_gauge_term` | `gauge_id`, `component_gauge_id`, `coef`, `lag_h`. A virtual gauge = Σ coef · component(t − lag). |
| `reach_gauge` | `reach_id`, `gauge_id`, `is_primary`, `metric` (cfs/ft), `bands` (jsonb: ordered breakpoints for too_low / low / medium / high / too_high, each with a `source` of `aw`, `derived`, or `user`) |
| `basin`, `basin_band` | `gauge_id`, (`band_no`), `geom`, `area_km2`, `area_check_ratio`, `mean_flow_dist_km` |
| `grid_weight` | `grid_id`, `gauge_id`, `band_no`, `cell_idx`, `weight` |
| `observation` | `gauge_id`, `param`, `ts`, `value`, `qualifier`. Partitioned by month; about 60 days kept on the server. |
| `precip_basin_obs` | `gauge_id`, `band_no`, `ts`, `product`, `mm` |
| `precip_basin_fcst` | `gauge_id`, `band_no`, `issued_at`, `valid_start`, `valid_end`, `p10…p90`, `mean`. **Archived forever.** |
| `noaa_flow_fcst` | `gauge_id`, `source` (`nwm_sr`/`nwm_mr`/`rfc`), `issued_at`, `valid_time`, `flow`. **Archived forever.** |
| `forecast_run` | `id`, `issued_at`, `model_version`, `status` |
| `flow_forecast` | `run_id`, `gauge_id`, `valid_time`, `q05…q95` |
| `reach_outlook` | `run_id`, `reach_id`, `day`, `p_runnable`, `band_probs` (jsonb), `p_too_high_all_day`, `peak_q10/q50/q90`, `peak_time`, `rain_needed_in`, `drivers` (jsonb) |
| `rain_response` | `run_id`, `gauge_id`, `day`, `rain_in`, `p_runnable`, `peak_q50`. A precomputed curve for the slider. |
| `analog_event` | `gauge_id`, `start_ts`, `rain_in`, `max_1h_in`, `duration_h`, `q0`, `qpeak`, `t_peak_h`, `api_7d`, `doy`, `hydrograph` (normalized array) |
| `model_skill` | `gauge_id`, `model_version`, `horizon_h`, `metric`, `value`, `period` |
| `job_run` | `job`, `started_at`, `finished_at`, `status`, `detail` |

Times are stored in UTC and displayed in the reach's local time zone. Migrations use Alembic.

**Bands**
- AW provides `beginLowRunnable` and `endHighRunnable`, plus a status string such as `low-runnable` or `medium-runnable`.
- Our default bands split [min, max] into thirds in log space.
- In Phase 1 we compare this split against AW's status strings across hundreds of live readings and adjust it to match.
- The user can override any breakpoint on any reach.

## 9. API (FastAPI; read-only for now)
| Endpoint | Returns |
|---|---|
| `GET /api/outlook?horizon=1..6&days=YYYY-MM-DD,…&state=&difficulty=` | The Outlook: per-day products for the next `horizon` days (default 6, max `OUTLOOK_MAX_DAYS`). Reaches are ranked by the best `p_runnable` over the selected days (all days if none are selected), with band probabilities and rain needed vs. forecast. |
| `GET /api/reaches?state=&q=&bbox=` · `GET /api/reaches/{id}` | Reach list and detail: gauges, bands, AW link |
| `GET /api/reaches/{id}/outlook` | Per-day products and drivers |
| `GET /api/gauges/{id}/observations?since=` | The observed hydrograph |
| `GET /api/gauges/{id}/forecast` | Our quantile fan plus the NWM and RFC series |
| `GET /api/gauges/{id}/rain-response?day=` | The precomputed curve of P(runnable) and peak vs. rain amount |
| `GET /api/gauges/{id}/whatif?rain_in=&start=&duration_h=` | Hydrograph quantiles, P(runnable) per day, and the nearest analog events. Computed live and cached by rounded inputs. |
| `GET /api/gauges/{id}/skill` | Track record |

The OpenAPI schema generates TypeScript types (`openapi-typescript`), so the frontend and backend can't drift apart.

## 10. American Whitewater scraper
**Approach** (owner decision: scrape AW's public pages)
- Fetch `/content/River/view/river-detail/{id}/main`.
- Extract the `self.__next_f.push([1,"…"])` chunks, unescape them, and concatenate them.
- Pull out the JSON objects (`stub`, `geometry`, the sidebar props including `correlations`, `nextRelease`, `alerts`) with a balanced-brace extractor keyed on property names.
- Validate the result with Pydantic models.

**Discovery of reach IDs** *(Phase 1 spike)*: use state index pages if they expose IDs; otherwise do a polite crawl over the ID range and record which IDs exist.

**Politeness**
- One connection at a time, with at least 5 seconds between requests.
- A descriptive User-Agent that includes a contact email.
- A weekly refresh covering pilot states only: about 600 reaches, roughly 1 hour per week.
- Honor any robots.txt changes, and back off on errors.

**Robustness**
- Store the raw HTML gzipped, so pages can be re-parsed without re-fetching.
- Use saved pages as test fixtures.
- Alert when the payload shape changes.
- Manual edits to `reach` and `reach_gauge` survive re-syncs.

**Gauge mapping**
- `USGS` gauges map directly.
- `CALCULATED` gauges go through a manual mapping table to real sources. Example: the Upper Gauley's "Gauley at Summersville (cfs)" ↔ USGS 03189600, Gauley below Summersville Dam *(verify)*.
- Unknown sources show AW's latest reading with no prediction until we build an adapter.

**Ethics and permission**
- Scraping is acceptable for a private tool.
- **Before any public launch**, ask AW for permission or a data feed. AW is a member-funded nonprofit, so join and offer to share our forecasts back.
- Never republish reach descriptions; show summary fields plus a "Read on AW" link, and attribute AW prominently.
- Because AW sits behind an adapter, switching to an official feed or to manual curation means changing one module.

## 11. Frontend (desktop-first)
**Stack:** React + TypeScript + Vite (a single-page app served as static files), TanStack Query, Tailwind, and **uPlot** (fast time-series charts) or Observable Plot. MapLibre GL comes in Phase 5 for the map, with free tiles (OpenFreeMap or Protomaps).

**Outlook (home page)**
- A rolling grid: one column per day from today through day 5 (6 max).
- A "next N days" control (1–6), optional day chips to focus on specific days (e.g., Saturday) and sort by them, and filters for region and difficulty.
- A dense, sortable table of reaches. Each row shows:
  - name and class
  - current flow with a trend arrow
  - a stacked band bar per day, with `P(runnable)`; later days fade to show lower confidence
  - rain needed vs. forecast
  - confidence
- Favorites are stored in localStorage and pinned to the top.
- Desktop-first, with side-by-side comparison, but still usable on a phone.

**Reach page**
- A header with river, section, class, gradient, and an AW link.
- The main chart shows the past 7 days observed plus a 6-day forecast fan (p10–p90 shaded, p50 as a line), over horizontal shading for the bands. NWM and RFC forecasts can be toggled on.
- A per-day outlook strip.
- A "Why" panel: the basin's rain forecast by day, the current level vs. the seasonal norm, and a summary of the analog events.
- The What-if panel.
- The track record.
- A small map of the watershed.

**What-if panel**
- **Sliders:** rain total (0–4 in.), start time, and storm type (3-h burst, 12-h typical, or 36-h soaker).
- **Outputs:**
  - the updated hydrograph fan
  - P(runnable) per day
  - the rain-response curve, with the forecast rain range overlaid
  - the nearest past analog events, with sparklines (e.g., "Mar 12 2021: 1.4 in. starting at 900 cfs → 4,100 cfs in 11 h")

**Methodology page:** how it works, its limits, attribution, and the disclaimer.

## 12. Repository layout
```
RiverRat/
├── DESIGN.md
├── README.md
├── docker-compose.yml
├── backend/
│   ├── pyproject.toml            # uv-managed, Python 3.12+
│   ├── src/riverrat/             # config.py, db/, sources/, geo/, features/, models/, forecast/, jobs/, api/
│   ├── tests/                    # pytest; fixtures incl. cached AW pages, USGS/NWPS responses
│   └── notebooks/                # exploration, hindcasts
├── web/                          # Vite + React + TS (pnpm)
├── infra/                        # Caddyfile, timers/cron, backup scripts
└── data/                         # gitignored: parquet lake, raw caches, model artifacts
```
Phase 0 deletes the IntelliJ Java/Gradle skeleton (`build.gradle`, `settings.gradle`, `gradlew*`, `gradle/`, `.gradle/`, `src/`) and rewrites `.gitignore` for Python and Node.

**Key libraries**
- **Backend:** httpx, pydantic, SQLAlchemy 2 + GeoAlchemy2, Alembic, FastAPI.
- **Data and geospatial:** pandas/polars, DuckDB, xarray, rioxarray, cfgrib, Herbie, exactextract, geopandas/shapely/pyproj, dataretrieval.
- **Modeling:** LightGBM, scikit-learn, and later PyTorch + NeuralHydrology.
- **Tooling:** uv, ruff, pyright, and pytest for Python; pnpm, ESLint, and Vitest for the web; GitHub Actions for CI.

## 13. Roadmap
| Phase | Goal | Exit criteria |
|---|---|---|
| **0. Foundations** | Restructure the repo; set up Compose (PostGIS), config, CI, and the pilot state list | `docker compose up` gives an empty database plus hello-world API and web |
| **1. Data spine** | AW scraper (NC, TN, GA, SC, KY, VA, WV, MD); gauge registry and USGS ingest (live plus history backfill); NWPS ingest; **start archiving NOAA forecasts**; NLDI basins and area checks; regime classification v1; band-split check | The database holds pilot reaches with bands and live flows, and a notebook can plot any reach's history |
| **2. Rain + baselines → first usable product** | Stage IV backfill (2002 onward) into basin-rain Parquet; live Stage IV; NBM QPF ingest and archive; M0 baselines incl. bias-corrected NWM; the API and **Outlook v1** (6-day) running on baselines and NOAA | You can check any of the next 6 days on real rivers |
| **3. Analog model + What-if** | Event extraction, M1, rain-response curves, What-if API and UI, perfect-rain hindcast harness | The slider works, analog explanations appear, and first skill numbers exist |
| **4. Probabilistic ML** | M2 LightGBM quantiles, NBM scenario ensembles, calibration, real-forecast hindcasts (NBM 2020+), per-gauge model selection, track-record UI; deploy to the VPS | Outlooks are calibrated and beat M0 and NWM in pilot hindcasts |
| **5. Regulated rivers + polish** | Release calendars (AW `nextRelease`, TVA, USACE CWMS), virtual gauges, intervening-basin models, map page, personal band overrides, alerts | The Gauley and New are handled honestly, and the map is live |
| **6. Research + growth** | LSTM, HRRR/GEFS scenarios, MRMS, snowmelt (West), other regions; public-launch prep (AW permission, accounts) | Open-ended |

## 14. Risks
| Risk | Mitigation |
|---|---|
| AW changes its page structure or blocks scraping | Adapter, cached raw HTML, test fixtures, parse alerts, and manual overrides. Predictions depend on AW only for the catalog and bands. |
| Going public without AW's permission | A hard gate: ask first (§10). |
| USGS legacy API shutdown (Q1 2027) | Build only on the OGC API; use an API key; backfill gradually within rate limits. |
| Rain-forecast error dominates from day 3 on | Probabilistic UI, a "rain needed" figure, and calibration. Never overclaim. |
| Rain data differs between training and live runs | Use the same product (Stage IV) for both, and monitor bias if MRMS is added. |
| Regulated rivers look predictable but aren't | Regime classification, release calendars, and confidence flags. |
| Winter ice and snow in the WV highlands | Drop ice-affected data; add temperature features; flag winter forecasts as lower confidence. |
| A 4 km rain grid is coarse for tiny creeks | Exact area weighting now; MRMS (1 km) later. |
| Limits of a $5 VPS | History and training stay on the laptop; the server holds recent windows and summaries. |
| Safety and liability | Disclaimers; links to NWS and AW; never presented as official. |

## 15. Decision log
| Date | Decision |
|---|---|
| 2026-10-18 | Personal tool first, architected to go public later |
| 2026-10-18 | Pilot region: Southern Appalachians plus VA/WV/MD. The owner paddles the New, Gauley, and James most; these are reference rivers, not the scope. The design works nationwide. |
| 2026-10-18 | Stack: Python (FastAPI, data, ML) + TypeScript (React/Vite). The Java skeleton will be deleted in Phase 0. |
| 2026-10-18 | Our learned model is the core; NOAA (NWM, RFC, NBM) serves as inputs and benchmarks |
| 2026-10-18 | AW data comes from polite scraping of public pages; get AW's permission before any public launch |
| 2026-10-18 | Hosting budget is $0–10/mo: one VPS plus training on the laptop |
| 2026-10-18 | MVP = Outlook + What-if slider; the map and alerts come later |
| 2026-10-18 | The Outlook is a rolling window of today plus up to 5 days (6 max), not weekend-only. Model horizon is 144 h; `OUTLOOK_MAX_DAYS = 6` is configurable. |
| 2026-10-18 | Runnable = in range at any point during daylight, with Low/Med/High bands plus personal overrides |
| 2026-10-18 | Desktop-first UI |

### Open questions (resolve in Phase 1 spikes)
- How to discover AW reach IDs; the full list of AW gauge `source` types; whether our band split matches AW's status strings.
- How far back the USGS OGC `continuous` collection goes, and the real rate limits with a key.
- When the NBM archive on AWS starts, and which QPF percentile fields and periods each model run provides.
- Stage IV backfill route: NCAR EOL bulk download vs. AORC Zarr subsetting.

## 16. Glossary
- **cfs:** cubic feet per second, a unit of flow.
- **Stage:** water height at a gauge, in feet.
- **Rating curve:** the relationship that converts stage to flow.
- **Hydrograph:** flow plotted over time.
- **Recession:** the falling limb of the hydrograph after a peak.
- **QPF:** Quantitative Precipitation Forecast, i.e. forecast rain amounts.
- **QPE:** Quantitative Precipitation Estimate, i.e. observed rain from radar plus gauges.
- **RFC:** one of NOAA's 13 River Forecast Centers, which issue official river forecasts.
- **NWM:** NOAA's National Water Model. It simulates flow on about 2.7 million US stream reaches.
- **NBM:** National Blend of Models, a calibrated blend of NOAA weather forecasts.
- **Antecedent conditions:** how wet the basin already is before a storm.
- **Runoff ratio:** the fraction of rain that leaves the basin as streamflow.
- **Hindcast:** running the forecast system on past dates to measure how well it would have done.
- **CRPS:** Continuous Ranked Probability Score, an error score for probabilistic forecasts where lower is better.
- **Calibration / reliability:** whether stated probabilities match observed frequencies.
