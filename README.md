# bird-manager

Web dashboard and Debian service for managing BIRD networks and BGP communities.

SQLite is the only and enforced data source for the BIRD daemon, and bird-manager renders a split BIRD
configuration, keeps the editable base separate from generated files, and
reconciles the live configuration only after an explicit Apply.

## Features

- CRUD for networks and ASN:COMM communities
- Three-tab dashboard: Networks, BIRD configuration and System status
- Left-aligned Windows-style tabs with hash routing and lazy tab loading
- Per-network and per-community `enabled` flags
- Automatic community disabling when a community has no enabled networks
- Separate generated files: `bird-manager.conf`, `routes.conf`, `core.conf` and `export.conf`
- Per-peer BGP export: each session gets the prefixes of its own ASN, with an
  opt-in `export_all` for the whole table
- Editable `networks.conf` with user comments/settings only
- Stable single include in `/etc/bird/bird.conf`; the mutable include list lives in `/etc/bird/live/bird-manager.conf`
- BIRD parse validation and unprivileged `birdc configure` reload
- `healthy` / `degraded` health status
- Rotating local backups with per-snapshot restore and deletion
- Full backup/restore including SQLite, live/staged files and manifest

## Configuration layout

For a packaged installation:

```text
/etc/bird/live/bird-manager.conf  last applied ordered include list
/etc/bird/live/networks.conf       last applied editable base and router id
/etc/bird/live/routes.conf         last applied generated DB static routes
/etc/bird/live/core.conf           last applied device/direct/BGP protocols
/etc/bird/live/export.conf          last applied per-peer export filters
/var/lib/bird-manager/staged/      current editable base and generated candidate
/var/lib/bird-manager/live-bak/    rollback snapshot of the previously published bundle
/var/lib/bird-manager/app.db       SQLite is the only and enforced data source for the BIRD daemon
/etc/bird/bird.conf                operator config plus one stable manager include
```

`/etc/bird/live` only ever contains the files listed in `bird-manager.conf`:
rollback snapshots live in `/var/lib/bird-manager/live-bak`, so no `*.bak` or
`*.tmp` artifacts appear next to the published configuration. All timestamps
(journal, audit, backups, status) use the server machine's local timezone.

`/etc/bird/bird.conf` contains one stable include:

```bird
include "/etc/bird/live/bird-manager.conf";
```

The generated `bird-manager.conf` is ordered as:

```bird
include "/etc/bird/live/networks.conf";
include "/etc/bird/live/export.conf";
include "/etc/bird/live/routes.conf";
include "/etc/bird/live/core.conf";
```

A BIRD channel stores **one** export filter, and a repeated `export filter` line
silently overwrites the previous one. So each peer gets exactly one filter of its
own, named `export_to_<name>`, carrying only the networks whose Community ASN
matches that peer's `local_as`. Two sessions towards one MikroTik therefore
receive different prefix sets, and adding a Community cannot hide what another
peer already exports.

A peer created with `export_all` is switched to the shared `export_own_networks`
filter instead, which carries every active network. That filter is only emitted
when at least one peer asks for it.

When no Community carries a peer's ASN, its filter ends in `reject` and the panel
shows `ASN не найдена`: the peer receives nothing rather than silently the whole
table.

The web panel keeps `networks.conf` as the only editable file. It shows
read-only sections for `bird-manager.conf` and `core.conf`, plus summary rows
for generated `routes.conf`, `export.conf` and `policy_*.conf`. Apply never
modifies `/etc/bird/bird.conf`.

Syntax validation always parses the *whole* bundle through the same include
block the published entrypoint uses, with only `networks.conf` replaced by the
edited text. A parse error that belongs to another generated file is reported
by message — with the file name — and does not paint a highlight inside the
editor, because that row does not exist in the file being edited.

## Dashboard

The header stays above a single centered 75%-wide `.tabs` block. This outer block
owns the only `#cbd5e1` outline; `.tab-strip` and `.tab-panels` are layout-only
containers without frames. Inactive tabs keep a dark background, adjacent tabs
are divided by vertical lines, and the active white tab opens directly into the
content area.

- `Сети` — the database-backed lists side by side at half width each (networks grouped by
  `AS:COMMUNITY`, and the BGP peer list), plus the full-width bulk network form and the
  add-peer form below them. Both lists start collapsed and load on first expand, showing
  only their counter until then. Under each peer name the panel shows its export scope —
  `все сети` for `export_all`, `N префиксов` for a matched ASN, or `ASN не найдена` when no
  Community carries that ASN, so a peer that will receive nothing is visible before it is
  brought up. The add form carries the matching `Отдавать все ASN` checkbox, off by
  default, with the hint that only the given Local AS is exported otherwise.
- `BIRD` — staged configuration, generated-file overview and Apply. Peers are
  database-backed and live on `Сети`; the live `birdc` snapshot is on `Система`.
- `Система` — a live **BIRD peers** block, configuration status, BIRD restart/reconfiguration timestamps, a dedicated rotating-backup list and the audit log.

The header has no user input. The journal identifier is read from `USERNAME` in
`.env`; it labels audit entries only and does not grant permissions or access.

The page itself never scrolls. The `.tabs` block fills the viewport height
between the header and its margins. An open panel takes all free vertical space
while the ones around it keep their natural height; long content scrolls only
inside the individual panel that owns it. On `Система` the `BIRD peers` block
behaves like the two lists on `Сети`: opened, it grows to the free height and
pushes the panels below it down, and a panel opened next to it takes its share
so the `BIRD peers` bottom edge moves up while both scroll internally.

Tabs are persisted in the URL hash (`#/networks`, `#/bird`, `#/system`). The
`Сети` tab loads its data on first activation. The `BIRD` and `Система` tabs
instead **recompute on every entry**: `BIRD` re-reads `/api/config/overview` and
re-renders the sections and files that are currently open, and `Система`
re-reads the live session snapshot, the configuration status with its backup
list, and the audit log. The header ⟳ button on the `BIRD` tab does the same
thing, and the two paths are shared so their behaviour cannot drift apart.

Two details make that recompute honest rather than cosmetic:

- The lazily fetched line models are keyed by filename plus their counters, but
  a counter pair cannot detect content that changed behind an unchanged `+1 −1`
  (one line replaced by another). A recompute therefore clears the model cache
  and always reaches the server; only a plain re-open may be served from it.
- Rows are aligned by the server (`old`/`new`/`kind` per row), never recomputed
  in the browser, so the two columns cannot drift apart row by row.

### BIRD peers block

The first panel of the `Система` tab is a read-only snapshot of
`birdc show protocols all`:

- **Service protocols** — one `up`/`down` row per non-BGP protocol (`Static`,
  `Direct`, `Device`, and any other type BIRD reports).
- **BGP sessions** — a table with the columns `Managed`, `Peer`, `Neighbor`,
  `Neighbor AS`, `Local AS`, `Состояние`, `Префиксы`, `Since`, `Last error`.
  Protocols are classified by the `Proto` column, so every BGP session is
  listed — including ones added to `bird.conf` by hand, which show
  `Managed: no`. A peer configured in the panel but missing from the BIRD output
  is listed as `нет в BIRD` so the discrepancy stays visible.
- `Managed` is `yes` when the protocol is in the panel's peer list, otherwise
  `no`. `Префиксы` shows `rx`/`tx` (imported/exported); a dash means BIRD printed
  no `Routes` line, which is what a down session does — not a zero count.
  `Last error` is the ninth column and stays empty while there is no error.
- `Состояние` holds two badges in one cell. The first is the channel state
  (`UP`/`DOWN`, green when the channel is up, grey when it is not). The second is
  the BGP state — `Established`, `Active`, `idle` — taken from the `BGP state:`
  line of `birdc show protocols all` and falling back to the summary `State`
  column when the detail block is missing. It is drawn only when it differs from
  the channel state, so a redundant second badge never appears. Colours are
  semantic: green for `Established`/`up`, amber for any other state, because a
  session that is not up deserves attention. Hovering either badge shows a
  tooltip naming the birdc field the value came from, so it is always clear
  whether you are looking at the channel, the BGP state or the summary column.

The panel is open by default — it is the live state the operator watches.

The block refreshes when the `Система` tab activates, on the header ⟳ button
(which also restarts the timer) and automatically every 30 seconds while the tab
is active, the panel is expanded and the page is visible. Auto-poll failures
are shown inline instead of raising a toast, so a stopped daemon does not spam
notifications.

## Backup management

The `Система` tab contains a dedicated `Бекап` panel between configuration
status and the audit log. `BIRD_BACKUP_RETENTION` controls the number of local
snapshot slots (default `5`, valid range `1..99`). The panel shows `used/limit`,
free capacity, and one row per snapshot with its creation time, restore and
delete actions.

`POST /api/backup` creates a snapshot, `POST /api/backup/restore` restores the
newest one for compatibility, `POST /api/backup/<id>/restore` restores a selected
snapshot, and `DELETE /api/backup/<id>` removes it. Backup IDs are strict
non-path identifiers. Every restore validates the archived bundle with `bird -p`
before replacing live state and then runs `birdc configure`.

## Development

```sh
pip install -r requirements.txt
python3 init_db.py
python3 app/routes.py --port 5000
```

Open `http://localhost:5000`. `GET /api/health` reports service health.

Run the isolated test suite:

```sh
python3 -m pytest -q
node --check static/js/app.js   # синтаксис фронтенда
```

Further reading:

- [docs/api_backend.md](docs/api_backend.md) — HTTP API, модели, lifecycle конфигурации,
  бекапы, контроль доступа
- [docs/frontend.md](docs/frontend.md) — вкладки, редактор, panels, polling, API-клиент
- [docs/build.md](docs/build.md) — сборка пакета, публикация в apt, апгрейд, откат

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `DB_PATH` | `data/app.db` | SQLite database |
| `BIRD_LIVE_DIR` | `<app>/live` | Live files read by BIRD |
| `BIRD_STAGED_DIR` | `<app>/staged` | Generated staging files |
| `BIRD_MAIN_CONFIG` | `/etc/bird/bird.conf` | Read-only main config with stable manager include |
| `BIRD_BACKUP_RETENTION` | `5` | Local snapshot count, valid range 1–99 |
| `BIRD_MANIFEST_PATH` | `/var/lib/bird-manager/managed-files.json` | Generated-file manifest |
| `BIRD_PUBLISH_BAK_DIR` | `/var/lib/bird-manager/live-bak` | Rollback snapshot of the live bundle; keeps `/etc/bird/live` free of `.bak` files |
| `BIRD_BIN_PATH` | `bird` | BIRD parse validator |
| `BIRDC_BIN_PATH` | `birdc` | Reload command |
| `BIRD_SOCKET` | empty | BIRD control socket override |
| `BIRD_ROUTER_ID` | `auto` | Router ID or host auto-detection |
| `BIRD_LOCAL_AS` | `65500` | Local ASN |
| `USERNAME` | `User` | Identifier for user's actions in journal and audit. Does not affect permissions/access. |
| `APP_HOST` | `0.0.0.0` | HTTP bind address; use `127.0.0.1` when a reverse proxy is the only entry point |
| `BIRD_TRUSTED_SOURCES` | `auto,127.0.0.1/32,::1/128` | Clients allowed to reach the API; CIDR list, `auto` = primary IPv4 network of this host |
| `BIRD_TRUSTED_PROXIES` | empty | Proxies whose `X-Forwarded-For` is honoured; empty means the header is ignored |
| `BIRD_TRUSTED_ORIGINS` | empty | Origins allowed to send `POST`/`PUT`/`DELETE`; empty disables the Origin check |
| `BIRD_MAX_BODY_BYTES` | `1048576` | Request body limit; larger bodies get `413` |
| `BIRD_DEBUG_ERRORS` | `0` | `1` adds exception text to 5xx responses (debugging only) |

`/etc/bird-manager/.env` is the packaged runtime configuration. Restart the
service after changing it.

## Access control

The API has no authentication, so the source allowlist is the only gate in
front of the BIRD configuration. It is enforced before routing and before the
request body is read.

- Requests from outside `BIRD_TRUSTED_SOURCES` get `403`, and the denial is
  written to the journal with the client address and the reason.
- `auto` resolves to the network of the host's primary IPv4 interface, so the
  dashboard works on the local network out of the box. Docker and VPN bridge
  subnets are never added automatically — list them explicitly if a container
  must reach the panel.
- `X-Forwarded-For` is only read when the peer itself is listed in
  `BIRD_TRUSTED_PROXIES`; otherwise the header is ignored and cannot be used to
  fake an address.
- With `BIRD_TRUSTED_ORIGINS` set, `POST`/`PUT`/`DELETE` without a matching
  `Origin` are rejected, which blocks cross-site requests from a browser on the
  same network. `curl` and scripts send no `Origin` and need an entry too, so
  leave the variable empty while scripting against the API.
- Every response carries `X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy`, a strict `Content-Security-Policy` and `Cache-Control:
  no-store`. The dashboard has no inline script for exactly this reason.
- 5xx responses return a generic message; the traceback stays in the journal
  unless `BIRD_DEBUG_ERRORS=1`.

### Behind a reverse proxy

```nginx
location / {
    proxy_pass http://127.0.0.1:5000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

```env
APP_HOST=127.0.0.1
BIRD_TRUSTED_SOURCES=auto,127.0.0.1/32
BIRD_TRUSTED_PROXIES=127.0.0.1/32
BIRD_TRUSTED_ORIGINS=https://bird.example.net
```

With `APP_HOST=127.0.0.1` only the proxy can reach the service, so the allowlist
may stay at loopback; the proxy address belongs in `BIRD_TRUSTED_PROXIES` and
the public origin in `BIRD_TRUSTED_ORIGINS`.

## Packaging

Build and publish — the usual case, no arguments:

```sh
scripts/build-deb.sh
```

The script owns the version. It stores the current one in `VERSION` and the last
built one in `dist/.last_version`, then decides:

| Situation | Result |
|---|---|
| `VERSION` untouched since the last build | patch component is incremented, so a repeated build never reuses a number |
| `VERSION` edited by hand | taken as-is, and reported as a change from the last build |
| `--bump patch\|minor\|major` | that component is raised and the ones to its right zeroed: `1.2.3` → `1.2.4` / `1.3.0` / `2.0.0` |
| `--version X.Y.Z` | forces an exact version and stores it in `VERSION` |

So a minor release is `scripts/build-deb.sh --bump minor`, and a hotfix is
`scripts/build-deb.sh --bump patch`. A malformed version is rejected rather than
built, and `--bump` together with `--version` is an error instead of a silent
winner.

Other flags:

```sh
scripts/build-deb.sh --no-publish   # build only, do not touch the apt repository
scripts/build-deb.sh --install      # also install and wait for the health endpoint
scripts/build-deb.sh --keep-tree    # leave the staging tree behind for inspection
scripts/build-deb.sh --help         # print the full help
```

A build never installs anything by itself — the running system is only touched
with `--install`.

Install or upgrade by hand:

```sh
sudo dpkg -i dist/bird-manager_<version>_all.deb
sudo systemctl restart bird-manager
```

The package creates `/var/lib/bird-manager`, enables the systemd service and
reconciles generated files from SQLite into the staged candidate. During
upgrade it migrates a recognized old managed block to the stable include,
preserves unrelated main-config content and validates the candidate with
`bird -p` before committing it.

The upgrade never reloads BIRD unless it actually rewrote the main config, and it
keeps an operator-edited `/etc/bird-manager/.env` instead of overwriting it. See
[docs/build.md](docs/build.md) for the full pipeline and [docs/frontend.md](docs/frontend.md)
for the dashboard.

## Uninstall

`apt remove bird-manager` asks whether to delete the database, staged files,
backups, environment file and package-created live files. The default is No.
On Yes, bird-manager first inlines the current BIRD bundle into `bird.conf`,
validates it with `bird -p`, and reconfigures a running BIRD before deleting the
live files. No persistent backup of `bird.conf` is created. Unknown live files
are preserved. `apt purge` performs the full cleanup without asking.
