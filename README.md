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

```sh
docker compose up -d db
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn riverrat.api.main:app --reload
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

### Web on the host

```sh
cd web
pnpm install
pnpm dev      # proxies /api to localhost:8000
```

## Data and attribution

River Rat uses river information from [American Whitewater](https://www.americanwhitewater.org/), gauge data from the [USGS](https://waterdata.usgs.gov/), and forecasts from [NOAA](https://water.noaa.gov/). Scraped or downloaded data is never committed to this repository.

River Rat is not an official forecast. Never rely on it alone for safety decisions on the river.
