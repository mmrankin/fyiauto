# Deploying fyiAuto to fyiAuto.com

## The constraint that drives everything

fyiAuto serves a **~8 GB SQLite** that is **rebuilt nightly** from on-prem SQL
Servers on private IPs (`10.1.2.17` inventory, `10.1.1.10` decode + ip2location).
The nightly sync therefore has to run somewhere that can reach those private
hosts. The simplest, safest topology is: **run the app + sync on a box inside
your network and expose it publicly through a Cloudflare Tunnel.** No public IP,
no inbound firewall holes, free TLS + CDN.

```
fyiAuto.com -> Cloudflare (DNS + TLS + CDN) -> cloudflared (outbound) -> waitress :5055 -> Flask
                                                  on-prem host that can reach 10.x SQL Servers
```

## Host

A small always-on **Linux box inside the network** is ideal (NUC / VM / existing
server). The macOS box works to start (it's what runs today), but a public
consumer site is better off not on a desktop that sleeps/updates. Requirements:
outbound internet + LAN access to the two SQL Servers. ~16 GB disk free for the DB.

## 1. The app as a service

Production server is **waitress** (`serve_prod.py`), not the Flask dev server.

- **macOS:** already wired — `run_server.sh` + `~/Library/LaunchAgents/com.fyiauto.web.plist`
  (KeepAlive). Reload after changes: `launchctl kickstart -k gui/$(id -u)/com.fyiauto.web`.
- **Linux:** use `deploy/fyiauto-web.service` (systemd).

Nightly data refresh runs separately (`com.fyiauto.dailysync`, 08:00 America/Chicago
on macOS; on Linux add a cron/systemd-timer running `sync.py --full`).

## 2. Point the domain at Cloudflare

1. Create a free Cloudflare account, **Add a site** -> `fyiAuto.com`.
2. At your **domain registrar**, change the nameservers to the two Cloudflare
   gives you. (Propagation: minutes to a few hours.)

## 3. Create the tunnel

On the serving host:

```bash
brew install cloudflared            # macOS;  Linux: see cloudflare docs / apt repo
cloudflared tunnel login            # browser -> authorize the fyiAuto.com zone
cloudflared tunnel create fyiauto   # prints a Tunnel UUID + credentials json path
```

Edit `deploy/cloudflared-config.yml` — paste the UUID and the credentials-file
path. Then create the DNS records and test:

```bash
cloudflared tunnel route dns fyiauto fyiAuto.com
cloudflared tunnel route dns fyiauto www.fyiAuto.com
cloudflared tunnel --config deploy/cloudflared-config.yml run
```

Visit https://fyiAuto.com — it should reach the local app over HTTPS.

## 4. Run the tunnel as a service

- **macOS:** `deploy/com.fyiauto.tunnel.plist` -> `~/Library/LaunchAgents/`, then
  `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.fyiauto.tunnel.plist`.
- **Linux:** `sudo cloudflared service install` (uses `/etc/cloudflared/config.yml`).

## 5. Production checklist

- **`.env` on the host** — copy `.env.example` -> `.env`, fill DB passwords +
  `ANTHROPIC_API_KEY` (for the AI search) + a strong `FLASK_SECRET_KEY`.
- **Public URLs** — set `SELL_MY_CAR_URL`, `TRADEIN_FALLBACK_URL`,
  `LEAD_FORM_BASE_URL` to your public hostnames (e.g. `https://trade.fyiAuto.com`)
  instead of `localhost`/`10.1.1.117`, and add those hostnames to the tunnel
  ingress + DNS if you expose the sister apps too.
- **Cloudflare caching** — add a Cache Rule to cache anonymous `GET` HTML for a
  short TTL (e.g. 5 min) plus `/static/*` long-lived. Data only changes nightly,
  so this offloads most traffic and hides the cold-start facet latency on novel
  filter combos.
- **First-load latency** — the startup warm thread covers the homepage + the
  "Shop by" categories; uncommon deep filter combos take a few seconds on first
  hit, then cache until the next nightly sync. Cloudflare HTML caching largely
  masks this for anonymous visitors.
- **Scale-up path (later, not now)** — SQLite is fine for this read-heavy load
  with WAL. If traffic outgrows one box, move the synced data to Postgres and
  run multiple app instances behind the tunnel / a load balancer.
