FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /uvx /bin/

# ecCodes (GRIB decoding) from Debian. The pip-bundled build is used only on macOS: on
# Linux its bundled C libraries clash with psycopg's and crash the interpreter at exit.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libeccodes0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# Install dependencies first so code changes don't invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY . .
RUN uv sync --locked --no-dev

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn riverrat.api.main:app --host 0.0.0.0 --port 8000"]
