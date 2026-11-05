Disclaimer: this software is 100% vibecoded. I did not write a single line of code in this whole project. 
I needed this tool in my "home datacenter" infrastructure (and also to try working with the local llm models), so the feature set is limited and does not cover full BIRD functionality.
More features may be added later.

# bird-manager

Web dashboard and Debian service for managing BIRD networks and BGP communities.

SQLite is the only data source for the BIRD daemon. bird-manager renders a split BIRD
configuration, keeps the editable base separate from generated files, and
reconciles the live configuration only after an explicit Apply.

## Features

- CRUD for networks and ASN:COMM communities
- Three-tab dashboard: Networks, BIRD configuration and System status. With hash routing and lazy tab loading
- Per-network and per-community `enabled` flags
- Automatic community disabling when a community has no enabled networks
- Split generated files: `bird-manager.conf`, `routes.conf`, `core.conf`, `export.conf`
  and per-community/per-peer split files (`routes_<ASN>_<VALUE>.conf`, `export_peer_<name>.conf`)
- Per-peer BGP export: each session gets the prefixes of its own ASN, with an
  opt-in `export_all` for the whole table
- Editable `networks.conf` with user comments/settings only
- Stable single include in `/etc/bird/bird.conf`; mutable include list lives in
  `/etc/bird/live/bird-manager.conf`
- BIRD parse validation and unprivileged `birdc configure` reload
- `healthy` / `degraded` health status
- Rotating local backups with per-snapshot restore and deletion
- Full backup/restore including SQLite, live/staged files and manifest

## Installation

Add the apt repository and install:

```sh
curl -fsSL https://apt.arteee87.ru/bird-manager.asc | sudo gpg --dearmor -o /usr/share/keyrings/bird-manager.gpg
echo 'deb [signed-by=/usr/share/keyrings/bird-manager.gpg] https://apt.arteee87.ru stable main' \
  | sudo tee /etc/apt/sources.list.d/bird-manager.list
sudo apt update
sudo apt install bird-manager
```

The package creates `/var/lib/bird-manager`, enables the systemd service and
reconciles generated files from SQLite into the staged candidate. During
upgrade it migrates a recognized old managed block to the stable include,
preserves unrelated main-config content and validates the candidate with
`bird -p` before committing it.

## Configuration layout

```
/etc/bird/live/bird-manager.conf  ordered include list (managed)
/etc/bird/live/networks.conf       editable base and router id
/etc/bird/live/routes.conf        generated static routes (legacy)
/etc/bird/live/core.conf          device/direct/BGP protocols
/etc/bird/live/export.conf        per-peer export filters (legacy)
/var/lib/bird-manager/staged/     current editable base and generated candidate
/var/lib/bird-manager/live-bak/   rollback snapshot of the previous bundle
/var/lib/bird-manager/app.db      SQLite data source
/etc/bird/bird.conf               operator config + one stable manager include
```

`/etc/bird/bird.conf` contains a single stable include:

```bird
include "/etc/bird/live/bird-manager.conf";
```

Each BGP channel stores exactly one export filter; a repeated `export filter`
line silently overwrites the previous one. So each peer gets its own filter
(`export_to_<name>`) carrying only networks whose Community ASN matches its
`local_as`. A peer with `export_all` uses the shared `export_own_networks`
filter. A peer whose ASN has no matching Community gets an empty filter ending
in `reject`.

## Dashboard

Basic HTML+CSS page with 3 tabs. The goal was minimal size + minimal browser overhead.

Tabs:
- `Networks` — lists of networks grouped by `AS:COMMUNITY`, and the BGP peer list, plus the network import form and the
  add-peer form below them. Both lists start collapsed and lazy-load on first expand.
- `BIRD` — staged configuration, generated-file overview and Apply button.
- `System` — a live **BIRD peers** block, configuration status, BIRD restart/reconfiguration timestamps, a dedicated rotating-backup list and the audit log.

## Running app & tests

```sh
pip install -r requirements.txt
python3 init_db.py
python3 app/routes.py --port 5000
python3 -m pytest -q
node --check static/js/app.js   # frontend syntax
```

## Environment

Key variables (full list in `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `DB_PATH` | `data/app.db` | SQLite database |
| `BIRD_LIVE_DIR` | `<app>/live` | Live files read by BIRD |
| `BIRD_STAGED_DIR` | `<app>/staged` | Generated staging files |
| `BIRD_BIN_PATH` | `bird` | BIRD parse validator |
| `BIRD_LOCAL_AS` | `65500` | Local ASN |
| `APP_HOST` | `0.0.0.0` | HTTP bind address |
| `BIRD_TRUSTED_SOURCES` | `auto,127.0.0.1/32,::1/128` | Clients allowed to reach the API |
| `BIRD_TRUSTED_ORIGINS` | empty | Origins allowed to send `POST`/`PUT`/`DELETE` |
| `BIRD_DEBUG_ERRORS` | `0` | `1` adds exception text to 5xx responses |

`/etc/bird-manager/.env` is the packaged runtime configuration. Restart the
service after changing it.

## Access control

The API has no authentication; the source allowlist is the only gate.
- Requests from outside `BIRD_TRUSTED_SOURCES` get `403`.
- `X-Forwarded-For` is only read when the peer is listed in `BIRD_TRUSTED_PROXIES`.
- With `BIRD_TRUSTED_ORIGINS` set, `POST`/`PUT`/`DELETE` without a matching
  `Origin` are rejected.
- Every response carries `X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy`, a strict `Content-Security-Policy` and `Cache-Control: no-store`.

## Packaging

```sh
scripts/build-deb.sh              # build, publish to /srv/repo
scripts/build-deb.sh --install    # also install and wait for health endpoint
scripts/build-deb.sh --bump minor # bump version
```

The script owns the version (`VERSION` file + `dist/.last_version`). See
[docs/build.md](docs/build.md) for the full pipeline.

## Further reading

- [docs/api_backend.md](docs/api_backend.md) — HTTP API, models, configuration lifecycle, backups, access control
- [docs/frontend.md](docs/frontend.md) — tabs, editor, panels, polling, API client
- [docs/build.md](docs/build.md) — package build, publishing, upgrade, rollback

## License

MIT. See [LICENSE](LICENSE).
```
