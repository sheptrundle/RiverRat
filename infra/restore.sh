#!/usr/bin/env bash
# On the server: load the bundle in deploy/ (from infra/bundle.sh) and start the site.
# Replaces the server's database with the laptop's, including archives collected on the
# server since the last bundle; see infra/DEPLOY.md, "Updating".
set -euo pipefail
cd "$(dirname "$0")/.."
compose=(docker compose -f docker-compose.prod.yml)
set -a; source .env; set +a
user=${POSTGRES_USER:-riverrat}
db=${POSTGRES_DB:-riverrat}

(cd deploy && shasum -a 256 -c SHA256SUMS)

echo "Unpacking model data…"
mkdir -p data
rm -rf data/hourly data/events data/ratings
tar -C data -xf deploy/data.tar

echo "Restoring the database…"
"${compose[@]}" stop api worker 2>/dev/null || true
"${compose[@]}" up -d db
until "${compose[@]}" exec -T db pg_isready -U "$user" -d "$db" >/dev/null; do sleep 2; done
"${compose[@]}" exec -T db psql -U "$user" -d postgres -c "DROP DATABASE IF EXISTS $db WITH (FORCE)" -c "CREATE DATABASE $db"
"${compose[@]}" exec -T db pg_restore -U "$user" -d "$db" --no-owner --exit-on-error < deploy/riverrat.dump

echo "Starting the site…"
"${compose[@]}" up -d --build
"${compose[@]}" ps
