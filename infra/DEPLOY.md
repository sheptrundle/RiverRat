# Deploying River Rat

The public site runs on one server with `docker-compose.prod.yml`: Postgres, the API, the worker (live jobs only), and Caddy, which serves the web app, proxies `/api`, and handles HTTPS. Training and the 20-year history stay on the laptop. The server gets a **bundle**: a database dump (~35 MB) and the per-gauge model data (~700 MB).

The target is Oracle Cloud's Always Free tier: an Ampere (ARM64) VM with up to 4 cores and 24 GB of RAM, at $0. Any Linux server with Docker works the same way, including a $5 VPS.

Before going public, read "Data and attribution" in the README. The Caddyfile sends `X-Robots-Tag: noindex` and `robots.txt` disallows crawling until you remove them.

## 1. Create the server (Oracle Cloud)

1. Sign up at <https://www.oracle.com/cloud/free/>. A card is required for verification; Always Free resources aren't charged. Pick a home region near you (e.g. US East, Ashburn). You can't change it later.
2. **Compute → Instances → Create instance**
   - Image: **Canonical Ubuntu 24.04**.
   - Shape: **Ampere → VM.Standard.A1.Flex**, 2 OCPUs and 12 GB of memory, which is plenty. If you get "out of capacity", try another availability domain or retry later; Always Free ARM capacity comes and goes.
   - Networking: create a new VCN with a public subnet and **assign a public IPv4 address**.
   - Add your SSH public key (`cat ~/.ssh/id_ed25519.pub`, or let Oracle generate one and download it).
   - Boot volume: the default 47 GB is enough.
3. **Open ports 80 and 443.** In the instance's subnet, open its **Security List → Add Ingress Rules**: source `0.0.0.0/0`, TCP, destination ports `80,443`. Add a second rule for UDP 443 (HTTP/3, optional).
4. Note the instance's **public IP**.

## 2. Prepare the server

```sh
ssh ubuntu@<public-ip>

# Oracle's Ubuntu images also block ports in the host firewall; open 80/443 there too.
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p udp --dport 443 -j ACCEPT
sudo netfilter-persistent save

# Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu && exit   # log out and back in for the group to apply

ssh ubuntu@<public-ip>
git clone https://github.com/sheptrundle/RiverRat.git
cd RiverRat
```

Create `RiverRat/.env` on the server (it's never committed):

```sh
POSTGRES_PASSWORD=<a long random password: openssl rand -hex 24>
USGS_API_KEY=<your key, as on the laptop>
# Your domain, for automatic HTTPS (step 4). Until then, ":80" serves plain HTTP by IP.
SITE_ADDRESS=:80
```

## 3. Send the bundle and start

On the laptop:

```sh
infra/bundle.sh                                         # -> deploy-bundle/
rsync -avP deploy-bundle/ ubuntu@<public-ip>:RiverRat/deploy/
```

On the server:

```sh
cd RiverRat
infra/restore.sh       # checks the bundle, loads data and database, builds, starts
```

The first build takes a few minutes. Then open `http://<public-ip>/`. The worker immediately runs the live jobs: readings, rain, forecasts, and the Outlook. Watch them with `docker compose -f docker-compose.prod.yml logs -f worker`.

## 4. A domain and HTTPS (Cloudflare)

1. Buy or move a domain to Cloudflare (Registrar sells at cost, ~$10/yr) and add it to your account.
2. **DNS → Add record**: type `A`, name `riverrat` (or `@`), IPv4 = the server's public IP. Leave it **DNS only (grey cloud)** for now.
3. On the server, set `SITE_ADDRESS=riverrat.example.com` in `.env`, then run `docker compose -f docker-compose.prod.yml up -d web`. Caddy gets a Let's Encrypt certificate within a minute; `https://riverrat.example.com` should work.
4. Optional: switch the record to **Proxied (orange cloud)** for Cloudflare's caching and DDoS protection, and set **SSL/TLS → Full (strict)**.

## Updating

| What changed | Do this |
|---|---|
| Code (git push) | On the server: `git pull && docker compose -f docker-compose.prod.yml up -d --build` |
| Training data (rebuilt hourly tables, events, ratings, catalog) | On the laptop `infra/bundle.sh` + rsync, then on the server `infra/restore.sh` |

`restore.sh` replaces the server's database with the laptop's. Archives the server collected since the last bundle (NBM rain forecasts, NOAA flow forecasts) are lost unless you first copy them back to the laptop. Do that before re-bundling once those archives matter for hindcasts:

```sh
ssh ubuntu@<public-ip> 'cd RiverRat && docker compose -f docker-compose.prod.yml exec -T db pg_dump -U riverrat -d riverrat --data-only -t rain_forecast -t rain_forecast_run -t noaa_forecast --format=custom' > server-archives.dump
```

## Operations

- **Status:** `docker compose -f docker-compose.prod.yml ps`, and `logs -f <service>`.
- **Health:** `curl https://<site>/api/health`.
- **Backups:** the database is rebuildable from the laptop, except the archives above. A weekly cron of that `pg_dump` to object storage (Oracle Object Storage's free 20 GB, or Cloudflare R2) covers them.
- **Restarts:** every service restarts automatically, including after a reboot (Docker starts at boot).
