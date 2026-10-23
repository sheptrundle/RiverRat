#!/usr/bin/env bash
# On the laptop: pack what the server needs into deploy-bundle/ (infra/DEPLOY.md).
#   riverrat.dump  the database (catalog, gauges, basins, recent readings and rain, archives)
#   data.tar       per-gauge storm libraries and ratings (data/hourly, data/events, data/ratings)
# Rebuild the training tables first (README: events hourly / extract, ratings) if the
# history changed.
set -euo pipefail
cd "$(dirname "$0")/.."
out=${1:-deploy-bundle}
user=${POSTGRES_USER:-riverrat}
db=${POSTGRES_DB:-riverrat}
mkdir -p "$out"

echo "Dumping the database…"
docker compose exec -T db pg_dump -U "$user" -d "$db" --format=custom --no-owner > "$out/riverrat.dump"

echo "Packing model data…"
tar -C data/ -cf "$out/data.tar" hourly events ratings

(cd "$out" && shasum -a 256 riverrat.dump data.tar > SHA256SUMS)
du -sh "$out"/riverrat.dump "$out"/data.tar
echo "Upload with: rsync -avP $out/ <user>@<server>:RiverRat/deploy/"
