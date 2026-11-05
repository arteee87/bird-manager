#!/usr/bin/env bash
# Build a Debian package (.deb) for bird-manager.
#
# Usage:
#   scripts/build-deb.sh [--bump patch|minor|major] [--version X.Y.Z]
#                         [--install] [--keep-tree] [--no-publish]
#
# The common case needs no arguments at all:
#   scripts/build-deb.sh
# It builds, publishes and prints what it did.
#
# Version handling — the current version always lives in VERSION:
#   * --bump patch|minor|major raises that component and zeroes the ones to its
#     right, then builds: 1.2.3 patch -> 1.2.4, minor -> 1.3.0, major -> 2.0.0.
#     Use this to choose the bump yourself.
#   * Without --bump, a VERSION that differs from the last built one (recorded in
#     dist/.last_version) is taken as-is, so hand-editing VERSION still works.
#   * Without --bump and without a hand-edited VERSION, the patch component is
#     incremented, so a repeated build never reuses a version number.
#   * --version X.Y.Z forces an exact version and stores it in VERSION.
#
# Output: dist/bird-manager_<version>_all.deb
# After a successful build the package is published to the apt repository
# (/srv/repo), unless --no-publish is given. Install and restart the service
# only with --install: a build never touches the running system on its own.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
DIST_DIR="$REPO_ROOT/dist"
VERSION_FILE="$REPO_ROOT/VERSION"
LAST_VERSION_FILE="$DIST_DIR/.last_version"
PKG_NAME="bird-manager"
ARCH="all"
FORCE_VERSION=""
BUMP=""
DO_INSTALL=0
KEEP_TREE=0
DO_PUBLISH=1
APT_REPO_DIR="${APT_REPO_DIR:-/srv/repo}"
HEALTH_URL="http://127.0.0.1:5000/api/health"

log() { printf '[build-deb] %s\n' "$*"; }

err() { printf '[build-deb] ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
    # Print the whole leading comment block, so the help cannot drift out of
    # sync with the header the way a fixed line range does.
    sed -n '2,/^set -euo/p' "$0" | sed -n 's/^# \{0,1\}//p' | sed '$d'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --bump)
            [ -n "${2:-}" ] || err "--bump needs a level: patch, minor or major"
            BUMP="$2"; shift 2 ;;
        --version)
            [ -n "${2:-}" ] || err "--version needs a value, e.g. 1.2.3"
            FORCE_VERSION="$2"; shift 2 ;;
        --install) DO_INSTALL=1; shift ;;
        --keep-tree) KEEP_TREE=1; shift ;;
        --no-publish) DO_PUBLISH=0; shift ;;
        -h|--help) usage 0 ;;
        *) err "unknown argument: $1 (see --help)" ;;
    esac
done

case "$BUMP" in
    '') ;;
    patch|minor|major) ;;
    *) err "--bump accepts patch, minor or major (got '$BUMP')" ;;
esac

if [ -n "$BUMP" ] && [ -n "$FORCE_VERSION" ]; then
    err "--bump and --version are mutually exclusive"
fi

command -v dpkg-deb >/dev/null 2>&1 || err "dpkg-deb not found (install 'dpkg')"

# Accept X.Y.Z and the shorter X.Y / X, normalising to three components so the
# arithmetic below has something to work with.
normalise_version() {
    local raw="$1"
    case "$raw" in
        *[!0-9.]*) err "version '$raw' is not numeric (expected X.Y.Z)" ;;
    esac
    local major minor patch extra
    IFS='.' read -r major minor patch extra <<< "$raw"
    [ -n "$major" ] || major=0
    [ -n "$minor" ] || minor=0
    [ -n "$patch" ] || patch=0
    if [ -n "${extra:-}" ]; then
        err "version '$raw' has more than three components"
    fi
    echo "$major.$minor.$patch"
}

bump_component() {
    # 1.2.3 patch -> 1.2.4 ; minor -> 1.3.0 ; major -> 2.0.0
    local version="$1" level="$2"
    local major minor patch
    IFS='.' read -r major minor patch <<< "$(normalise_version "$version")"
    case "$level" in
        patch) echo "$major.$minor.$((patch + 1))" ;;
        minor) echo "$major.$((minor + 1)).0" ;;
        major) echo "$((major + 1)).0.0" ;;
    esac
}

# ---- resolve version -------------------------------------------------------
mkdir -p "$DIST_DIR"
CURRENT="$(tr -d '[:space:]' < "$VERSION_FILE")"
[ -n "$CURRENT" ] || err "VERSION file is empty"

LAST_BUILT=""
if [ -f "$LAST_VERSION_FILE" ]; then
    LAST_BUILT="$(tr -d '[:space:]' < "$LAST_VERSION_FILE")"
fi

if [ -n "$FORCE_VERSION" ]; then
    VERSION="$(normalise_version "$FORCE_VERSION")"
    log "forced version: $VERSION"
elif [ -n "$BUMP" ]; then
    VERSION="$(bump_component "$CURRENT" "$BUMP")"
    log "$BUMP bump: $CURRENT -> $VERSION"
elif [ -n "$LAST_BUILT" ] && [ "$CURRENT" = "$LAST_BUILT" ]; then
    # Nothing changed since the last build: never reuse a version number.
    VERSION="$(bump_component "$CURRENT" patch)"
    log "VERSION unchanged since the last build ($CURRENT), patch bumped: $VERSION"
else
    VERSION="$(normalise_version "$CURRENT")"
    if [ -z "$LAST_BUILT" ]; then
        log "first build, version from VERSION: $VERSION"
    else
        log "VERSION changed since the last build ($LAST_BUILT -> $CURRENT), building $VERSION"
    fi
fi

# Keep VERSION and the recorded last-build in sync with what we are shipping.
printf '%s' "$VERSION" > "$VERSION_FILE"

DEB_PATH="$DIST_DIR/${PKG_NAME}_${VERSION}_${ARCH}.deb"
log "package: $DEB_PATH"

# ---- stage tree ------------------------------------------------------------
STAGE="$(mktemp -d /tmp/bm-deb-build.XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT
log "staging tree: $STAGE"

UB="$STAGE/usr/lib/$PKG_NAME"
mkdir -p "$UB" "$STAGE/etc/$PKG_NAME" "$STAGE/lib/systemd/system" \
         "$STAGE/usr/share/doc/$PKG_NAME" "$STAGE/usr/share/man/man8" \
         "$STAGE/DEBIAN"

# Application tree (no dev/test artifacts).
for entry in app static templates; do
    [ -d "$REPO_ROOT/$entry" ] || err "missing $REPO_ROOT/$entry"
    cp -a "$REPO_ROOT/$entry" "$UB/"
done
for file in init_db.py requirements.txt VERSION; do
    [ -f "$REPO_ROOT/$file" ] || err "missing $REPO_ROOT/$file"
    cp -a "$REPO_ROOT/$file" "$UB/"
done

# Strip pycache and runtime-only directories from the shipped tree.
find "$UB" -name '__pycache__' -type d -prune -exec rm -rf {} +
rm -rf "$UB/app/live" "$UB/app/staged" "$UB/app/apply_state.json"

# Packaged default env (next to the modules) + operator conffile (in /etc).
ENV_CONTENT=$(cat <<'ENV'
DB_PATH=/var/lib/bird-manager/app.db
BIRD_LIVE_DIR=/etc/bird/live
BIRD_STAGED_DIR=/var/lib/bird-manager/staged
BIRD_BACKUP_DIR=/var/lib/bird-manager/backup
BIRD_BACKUP_RETENTION=5
BIRD_MAIN_CONFIG=/etc/bird/bird.conf
BIRD_BIN_PATH=/usr/sbin/bird
BIRDC_BIN_PATH=birdc
BIRD_SOCKET=
APP_PORT=5000
DEBUG_MODE=True
# Identifier for user's actions in journal and audit. Does not affect permissions/access.
USERNAME=User
BIRD_ROUTER_ID=auto
BIRD_LOCAL_AS=65500
# --- Access control -------------------------------------------------------
# Clients allowed to change the BIRD configuration (CIDR list, comma separated).
# "auto" is the network of this host's primary IPv4 interface. Docker and VPN
# bridge subnets are never added automatically — list them explicitly.
BIRD_TRUSTED_SOURCES=auto,127.0.0.1/32,::1/128
# Reverse proxies allowed to set X-Forwarded-For. Empty means the header is
# ignored completely, so a client cannot spoof its address.
BIRD_TRUSTED_PROXIES=
# Origins allowed to send POST/PUT/DELETE (exact scheme://host[:port]).
# Empty disables the Origin check; set it to block cross-site browser requests.
BIRD_TRUSTED_ORIGINS=
# Largest accepted request body in bytes; bigger requests get HTTP 413.
BIRD_MAX_BODY_BYTES=1048576
# When 1, 5xx responses include the exception text. Keep 0 in production.
BIRD_DEBUG_ERRORS=0
# HTTP bind address. 0.0.0.0 keeps the current behaviour; use 127.0.0.1 when a
# reverse proxy is the only intended entry point.
APP_HOST=0.0.0.0
ENV
)
printf '%s' "$ENV_CONTENT" > "$UB/.env"
cp "$UB/.env" "$STAGE/etc/$PKG_NAME/.env"

cp "$REPO_ROOT/packaging/bird-manager.service" "$STAGE/lib/systemd/system/"
cp "$REPO_ROOT/packaging/README.Debian" "$STAGE/usr/share/doc/$PKG_NAME/"
cp "$REPO_ROOT/packaging/debian/copyright" "$STAGE/usr/share/doc/$PKG_NAME/"

# Man page: substitute version and date, lint if groff is available, ship
# gzip-compressed. Keeping the version in the source would make it stale on
# every release but the one someone remembered to edit.
MAN_RENDERED="$STAGE/usr/share/man/man8/bird-manager.8"
sed -e "s/@@VERSION@@/$VERSION/g" \
    -e "s/@@MONTH@@/$(LC_ALL=C date +%B)/g" \
    -e "s/@@YEAR@@/$(date +%Y)/g" \
    "$REPO_ROOT/packaging/bird-manager.8" > "$MAN_RENDERED"
if command -v groff >/dev/null 2>&1; then
    groff -man -Tutf8 "$MAN_RENDERED" >/dev/null 2>&1 \
        || err "man page failed the groff lint"
fi
gzip -n -9 -c "$MAN_RENDERED" > "$MAN_RENDERED.gz"
rm -f "$MAN_RENDERED"
cp "$REPO_ROOT/packaging/debian/conffiles" "$STAGE/DEBIAN/"
cp "$REPO_ROOT/packaging/debian/templates" "$STAGE/DEBIAN/"
cp "$REPO_ROOT/packaging/debian/postinst" "$STAGE/DEBIAN/"
cp "$REPO_ROOT/packaging/debian/prerm" "$STAGE/DEBIAN/"
cp "$REPO_ROOT/packaging/debian/postrm" "$STAGE/DEBIAN/"
chmod 0755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm" "$STAGE/DEBIAN/postrm"
chmod 0644 "$STAGE/DEBIAN/conffiles" "$STAGE/DEBIAN/templates"

# control: fill version + installed size.
SIZE_KIB=$(du -sk "$STAGE/usr" "$STAGE/etc" 2>/dev/null | awk '{s += $1} END {printf "%d", s}')
sed -e "s/@@VERSION@@/$VERSION/g" -e "s/@@SIZE@@/$SIZE_KIB/g" \
    "$REPO_ROOT/packaging/debian/control" > "$STAGE/DEBIAN/control"

# ---- build ----------------------------------------------------------------
log "building .deb (Installed-Size: ${SIZE_KIB} KiB) ..."
dpkg-deb --build --root-owner-group "$STAGE" "$DEB_PATH" >/dev/null

printf '%s' "$VERSION" > "$LAST_VERSION_FILE"
log "recorded last built version: $VERSION"

# ---- publish to apt repository --------------------------------------------
if [ "$DO_PUBLISH" -eq 1 ]; then
    SUDO=""
    [ "$(id -u)" -eq 0 ] || SUDO="sudo"
    POOL_DIR="$APT_REPO_DIR/pool/main/b/$PKG_NAME"
    PUBLISH_SCRIPT="/usr/local/bin/publish-repo.sh"

    [ -d "$APT_REPO_DIR/dists" ] || err "apt repo not found at $APT_REPO_DIR (set APT_REPO_DIR)"
    [ -x "$PUBLISH_SCRIPT" ] || err "publish script not found: $PUBLISH_SCRIPT"

    log "publishing $DEB_PATH -> $APT_REPO_DIR ..."
    $SUDO install -m 0644 "$DEB_PATH" "$POOL_DIR/"
    $SUDO "$PUBLISH_SCRIPT"
else
    log "skipping publish (--no-publish)"
fi

if [ "$KEEP_TREE" -eq 0 ]; then
    rm -rf "$STAGE"
    trap - EXIT
fi

dpkg-deb --info "$DEB_PATH" | sed 's/^/  /'
dpkg-deb --contents "$DEB_PATH" | awk '{print $NF}' | sed 's/^/  /'

# ---- optional install ------------------------------------------------------
if [ "$DO_INSTALL" -eq 1 ]; then
    [ "$(id -u)" -eq 0 ] || err "--install requires root"
    log "installing $DEB_PATH ..."
    dpkg -i "$DEB_PATH"
    log "waiting for health endpoint ..."
    for i in $(seq 1 30); do
        if curl -fsS -m 2 "$HEALTH_URL" >/dev/null 2>&1; then
            log "health OK: $HEALTH_URL"
            systemctl status bird-manager.service --no-pager | sed 's/^/  /'
            exit 0
        fi
        sleep 1
    done
    err "service did not become healthy within 30s; check: journalctl -u bird-manager.service"
fi

log "done. install with:  sudo dpkg -i $DEB_PATH"