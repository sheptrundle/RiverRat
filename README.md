# River Rat

Will your river be runnable on the day you want to go? River Rat combines live gauges, rain forecasts, and a model of how each river responds to rain to give an honest probability, up to 6 days out.

**Status:** early development (Phase 0: foundations). See [DESIGN.md](DESIGN.md) for the design and roadmap.

## Layout

| Path | What |
|---|---|
| `backend/` | Python: data ingestion, forecasting models, FastAPI (`riverrat` package) |
| `web/` | React + TypeScript (Vite) |
| `DESIGN.md` | Design document |

## Local development

Requirements: Docker (Docker Desktop or [Colima](https://github.com/abiosoft/colima)), [uv](https://docs.astral.sh/uv/), [pnpm](https://pnpm.io/), Node 22+.

### Everything in containers

```sh
cp .env.example .env    # optional, the defaults work
docker compose up --build
```

- Web: http://localhost:5173
- API: http://localhost:8000/api/health (interactive docs at `/docs`)
- Postgres + PostGIS: `localhost:5432` (user, password, and database are all `riverrat`)

The API container applies database migrations on startup.

### Backend on the host

If the repo lives in an iCloud-synced folder (e.g. Desktop), iCloud marks `.venv` files hidden and Python 3.13 then ignores them. Keep the venv and the data folder in iCloud-excluded folders: `mkdir backend/.venv.nosync data.nosync && ln -s .venv.nosync backend/.venv && ln -s data.nosync data` before `uv sync`.

GRIB decoding uses ecCodes. On macOS `uv sync` installs it bundled with the Python package; on Linux install the system library first (`apt-get install libeccodes0`), as the Dockerfile and CI do.

```sh
docker compose up -d db
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn riverrat.api.main:app --reload
uv run pytest
uv run ruff check . && uv run ruff format --check .

# Sync the American Whitewater reach catalog (facts only, 1 request / 5 s)
uv run python -m riverrat.jobs.sync_reaches --index-only   # ~1,500 pilot reaches, 1 request
uv run python -m riverrat.jobs.sync_reaches                # + reach details, ~2 h first run

# USGS gauges (needs USGS_API_KEY in .env; 1,000 requests/hour)
uv run python -m riverrat.jobs.usgs sites      # locations + periods of record
uv run python -m riverrat.jobs.usgs live       # last 3 h of 15-min readings -> Postgres
uv run python -m riverrat.jobs.usgs backfill   # 15-min history since 2002 -> data/usgs/iv/

# NOAA forecasts (NWPS keeps no history, so archive every few hours)
uv run python -m riverrat.jobs.noaa link       # gauges -> NWS location IDs + NWM reaches
uv run python -m riverrat.jobs.noaa archive    # store new RFC + NWM short-range forecasts

# Watersheds (after usgs sites)
uv run python -m riverrat.jobs.basins          # NLDI basin polygon per USGS gauge

# Rain history (after basins): hourly basin means from AORC, ~4 min per year
uv run python -m riverrat.jobs.rain backfill   # 2002 onward -> data/rain/aorc/{year}.parquet
uv run python -m riverrat.jobs.rain recent     # Stage IV since AORC ends -> Postgres basin_rain
uv run python -m riverrat.jobs.rain_forecast archive  # NBM rain percentiles -> Postgres rain_forecast

# Training tables and storm events (after the flow and rain backfills; rebuilds from scratch)
uv run python -m riverrat.jobs.events hourly   # flow + basin rain per hour -> data/hourly/{site}.parquet
uv run python -m riverrat.jobs.events extract  # past storms and responses -> data/events/events.parquet

# Score the analog model on storms it never saw (library <= 2019; 2023+ is the held-out test)
uv run python -m riverrat.jobs.hindcast analog # 2020-22 storms -> data/skill/analog_2020-2022.parquet
```

Long backfills can run in a container so they survive closing the terminal:

```sh
docker compose run -d --name riverrat-rain worker python -m riverrat.jobs.rain backfill
docker logs -f riverrat-rain
```

### Scheduled jobs

The `worker` container (`docker compose up -d worker`) runs recurring jobs from `riverrat/jobs/scheduler.py`. Each job runs at startup and then on its interval; `docker compose logs -f worker` shows them. They only run while Docker is up, so on a laptop expect gaps while it sleeps.

| Job | Every | Steps |
|---|---|---|
| `noaa-archive` | 3 h | `noaa link`, `noaa archive` |
| `rain-recent` | 1 h | `rain recent --max-hours 48` (new Stage IV hours, plus revisions from the last 2 days) |
| `rain-forecast` | 1 h | `rain_forecast archive` (NBM 00/06/12/18Z runs from the last 2 days not yet stored) |

After changing backend code, rebuild with `docker compose up -d --build worker`.

### Web on the host

```sh
cd web
pnpm install
pnpm dev      # proxies /api to localhost:8000
```

## Data and attribution

River Rat uses river information from [American Whitewater](https://www.americanwhitewater.org/), gauge data from the [USGS](https://waterdata.usgs.gov/), and forecasts from [NOAA](https://water.noaa.gov/). Scraped or downloaded data is never committed to this repository.

River Rat is not an official forecast. Never rely on it alone for safety decisions on the river.
