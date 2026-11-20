"""Shared runtime state and helpers for the bird-manager API.

The WSGI plumbing, access control and entry point live in routes.py; the
handler modules (api_*.py) and routes.py both read the state bound here.
Everything that can be rebound at runtime (engine, _DB_PATH, live/staged
paths) must be accessed as a context attribute at call time, never captured
with ``from context import engine`` — a rebind would leave a stale copy.
"""

import os
import re
import sys
import time
import traceback

# Conditional imports — work both as package and as script entry point
try:
    from .database import (
        get_session, engine, _DB_PATH, rebind_engine,
    )
    from .crud_operations import CRUDOperations
    from .parsing_engine import ParsingEngine
    from .config_generator import (
        generate_split,
        _as_community_ids,
        _effective_values,
        _community_pairs,
    )
    from .diff_engine import DiffEngine
    from .config_manager import ConfigManager
    from .models import AuditLog, ensure_schema
    from .log import log
except ImportError:
    _parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _parent_dir not in sys.path:
        sys.path.insert(0, _parent_dir)
    from database import (
        get_session, engine, _DB_PATH, rebind_engine,
    )
    from crud_operations import CRUDOperations
    from parsing_engine import ParsingEngine
    from config_generator import (
        generate_split,
        _as_community_ids,
        _effective_values,
        _community_pairs,
    )
    from diff_engine import DiffEngine
    from config_manager import ConfigManager
    from models import AuditLog, ensure_schema
    from log import log


# Upgrade path for existing installs: create_all is idempotent and only adds
# tables and columns that are missing.
try:
    ensure_schema(engine)
except Exception:
    pass

# Initialize components
crud = CRUDOperations()
parser = ParsingEngine()

# Single base directory shared by generator, diff engine and config manager.
# Using abspath keeps behavior identical in package mode and script mode.
# Paths can be overridden via env (BIRD_LIVE_DIR / BIRD_STAGED_DIR); the
# defaults keep the historical app/live + app/staged layout on a bare host.
_BASE_DIR = os.path.abspath(os.path.dirname(__file__))
_STAGED_PATH = os.getenv('BIRD_STAGED_DIR', os.path.join(_BASE_DIR, 'staged'))
_LIVE_PATH = os.getenv('BIRD_LIVE_DIR', os.path.join(_BASE_DIR, 'live'))

diff_engine = DiffEngine(live_path=_LIVE_PATH, staged_path=_STAGED_PATH)
config_manager = ConfigManager(
    live_path=_LIVE_PATH,
    staged_path=_STAGED_PATH,
    db_session_factory=get_session,
)


# WSGI app — route handlers registered via decorator
ROUTES = []


def route(pattern, method='GET'):
    """Decorator to register a WSGI route handler."""
    def decorator(func):
        ROUTES.append({
            'pattern': pattern,
            'method': method.upper(),
            'handler': func,
        })
        return func
    return decorator


def _bool_env(name, default=False):
    value = (os.getenv(name) or '').strip().lower()
    if not value:
        return default
    return value in ('1', 'true', 'yes', 'on')


GENERIC_ERROR_TEXT = 'Внутренняя ошибка сервера'


def _error_message(exc):
    """Log the failure and return text that is safe to send to the client."""
    log.error(f"[ERROR] {type(exc).__name__}: {exc}")
    traceback.print_exc()
    if _bool_env('BIRD_DEBUG_ERRORS', False):
        return str(exc)
    return GENERIC_ERROR_TEXT


def _bind_paths_from_env():
    """Point the shared diff engine and config manager at the current env.

    Called once at import, and again whenever the environment changes underneath
    the process. Without it the manager is pinned to whatever BIRD_LIVE_DIR /
    BIRD_STAGED_DIR held at first import, while _apply_state_path(),
    publish_staged_generated() and the health check keep reading env-derived
    locations — so a retargeted run would write into the first run's tree.
    """
    global _STAGED_PATH, _LIVE_PATH
    _LIVE_PATH = os.getenv('BIRD_LIVE_DIR', os.path.join(_BASE_DIR, 'live'))
    _STAGED_PATH = os.getenv('BIRD_STAGED_DIR', os.path.join(_BASE_DIR, 'staged'))
    diff_engine.live_path = _LIVE_PATH
    diff_engine.staged_path = _STAGED_PATH
    config_manager.retarget(_LIVE_PATH, _STAGED_PATH)


def _bind_database_from_env():
    """Point this module's engine and path at the current DB_PATH.

    See docs/api_backend.md, "Общие сведения", for why the paths are bound
    explicitly rather than read per call.

    ``engine`` and ``_DB_PATH`` are imported from database by name, so they are
    bound to whatever DB_PATH held at first import. That matters beyond reads:
    restore_backup() disposes ``engine`` and os.replace()s the file at
    ``_DB_PATH``, so an unbound process would restore a database it is not using
    and destroy the one it is. Tests that point DB_PATH at their own tree call
    this before touching the database.
    """
    global engine, _DB_PATH
    engine, _DB_PATH = rebind_engine(os.getenv('DB_PATH', 'data/app.db'))


def _seed_staged():
    """Run the import-time bootstrap against whatever is currently bound.

    Default Community plus a first staged generation. Wrapped because a
    read-only or half-installed host must still import the module.
    """
    try:
        session = get_session()
        crud.ensure_default_community(session)
        _regenerate_staged(session)
    except Exception:
        pass


def _bird_settings():
    """Router id / local ASN for the generated config (env with defaults).

    ``BIRD_ROUTER_ID`` may be ``auto`` (or empty) — the config generator then
    detects the host's primary IPv4 address. An explicit value is used as-is.
    """
    router_id = (os.getenv('BIRD_ROUTER_ID') or '').strip()
    if not router_id or router_id.lower() == 'auto':
        router_id = None  # auto-detection happens in config_generator
    return {
        'router_id': router_id,
        'local_as': os.getenv('BIRD_LOCAL_AS', '65500'),
    }


def _is_editable_config(filename):
    if not filename or os.path.basename(filename) != filename:
        return False
    return filename == 'networks.conf'


def _db_bgp_peers(session):
    """Current BGP peers from the database, as generator-style dicts."""
    return [
        {
            'name': p.name,
            'local_as': p.local_as,
            'neighbor': p.neighbor,
            'neighbor_as': p.neighbor_as,
            'status': p.status,
            'export_all': bool(p.export_all),
        }
        for p in crud.get_bgp_peers(session)
    ]


# ---------------------------------------------------------------------------
# Cached per-ASN prefix counts for the BGP peer panel.
#
# ``get_bgp_peers`` only needs ``len(prefixes_for_asn)`` and
# ``asn in prefixes`` per peer, but computing them scans every network and every
# community.  On a database with tens of thousands of networks that is hundreds
# of milliseconds per call, repeated on every expand of the peers panel.  The
# result depends only on the (enabled) networks and communities, so we cache it.
#
# Invalidation is version-based: ``_bump_data_version`` runs inside
# ``_regenerate_staged``, which every successful mutation calls.  A TTL of
# ``_ASN_COUNTS_TTL`` seconds is an additional safety net against missed
# invalidations (external DB edits, another process, a forgotten hook) — it is
# NOT a background timer; recomputation happens only when the endpoint is
# actually called and the TTL has expired.
_DATA_VERSION = 0
_ASN_COUNTS_CACHE = {'version': -1, 'value': None, 'computed_at': 0.0}
_ASN_COUNTS_TTL = 300.0


def _bump_data_version():
    """Invalidate the cached per-ASN prefix counts."""
    global _DATA_VERSION
    _DATA_VERSION += 1


def _cached_peer_asn_prefix_counts(session) -> dict:
    """Return ``{asn: set(prefix)}`` for enabled networks/communities.

    Cached across requests; recomputed on a version bump (mutation) or when the
    TTL expires.  The value is plain data (sets of strings), never ORM objects,
    so it is safe to share between requests and threads.
    """
    global _ASN_COUNTS_CACHE
    now = time.time()
    if (_ASN_COUNTS_CACHE['version'] != _DATA_VERSION
            or (now - _ASN_COUNTS_CACHE['computed_at']) > _ASN_COUNTS_TTL):
        _ASN_COUNTS_CACHE['value'] = _peer_asn_prefix_counts(session)
        _ASN_COUNTS_CACHE['version'] = _DATA_VERSION
        _ASN_COUNTS_CACHE['computed_at'] = now
    return _ASN_COUNTS_CACHE['value']


def _regenerate_staged(session):
    """Best-effort regeneration of the staged config from current DB state.

    Returns the staged config path on success, ``None`` on failure so that
    callers (CRUD handlers) never fail because of configuration generation.
    """
    _bump_data_version()
    try:
        networks_list = crud.get_networks(session)
        communities_list = crud.get_communities(session)
        bgp_peers = _db_bgp_peers(session)
        active_ids = {
            community_id
            for network in networks_list
            if network.enabled is not False
            for community_id in _as_community_ids(network.community_ids)
        }
        changed = False
        for community in communities_list:
            if community.enabled is not False and community.id not in active_ids:
                community.enabled = False
                changed = True
        if changed:
            session.commit()

        networks = [{
            'prefix': n.prefix, 'description': n.description, 'status': n.status,
            'enabled': n.enabled,
            'community_ids': _as_community_ids(n.community_ids),
        } for n in networks_list]
        communities = [{
            'id': c.id, 'name': c.name, 'values': c.values, 'description': c.description,
            'enabled': c.enabled,
        } for c in communities_list]

        settings = _bird_settings()
        base_content = None
        for base_path in (
            os.path.join(_STAGED_PATH, 'networks.conf'),
            os.path.join(_LIVE_PATH, 'networks.conf'),
        ):
            try:
                with open(base_path, 'r', encoding='utf-8') as stream:
                    base_content = stream.read()
                break
            except OSError:
                pass
        staged_file = generate_split(
            networks, communities, base_path=_BASE_DIR,
            staged_dir=_STAGED_PATH,
            router_id=settings['router_id'], local_as=settings['local_as'],
            bgp_peers=bgp_peers, base_content=base_content,
            managed_include_dir=_LIVE_PATH,
        )
        config_manager.split_mode = True
        return staged_file
    except Exception:
        return None


# Bootstrap the directories we are actually bound to: default community, then
# a first staged generation. Wrapped because a read-only or half-installed
# host must still be able to import the module.
_seed_staged()


def _bgp_config_paths():
    """Live and staged copies of every file that can hold ``protocol bgp``.

    The generated peers moved to ``core.conf`` when the split bundle landed;
    ``networks.conf`` is still scanned because a database that was never
    migrated to the split layout keeps its BGP blocks there.
    """
    for name in ('core.conf', 'networks.conf'):
        for base in (_STAGED_PATH, _LIVE_PATH):
            yield os.path.join(base, name)


def _asn_already_in_use(session, neighbor_as):
    """True when the neighbor ASN is present in the DB or live/staged configs.

    Guards against a second peer sending to the same ASN on an already
    reachable neighbor (bird warns about sessions to the same eBGP ASN).
    Scans raw config text so manually edited blocks count too.
    """
    try:
        for p in crud.get_bgp_peers(session):
            if p.neighbor_as == str(neighbor_as):
                return True
    except Exception:
        pass
    asn_re = re.compile(r'neighbor\s+\S+\s+as\s+(\d+)')
    for path in _bgp_config_paths():
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                for matched in asn_re.findall(fh.read()):
                    if matched == str(neighbor_as):
                        return True
        except OSError:
            continue
    return False


def _neighbor_already_in_use(session, neighbor):
    """True when the neighbor address is present in the DB or live/staged config.

    Two BGP protocols towards one address never both come up: bird hands an
    incoming session to the first protocol whose listen request matches
    (proto/bgp/bgp.c ``bgp_find_proto``), and the remote router identifies its
    own neighbor by address, so the second OPEN does not match its config.
    Rejecting it here keeps the running config within what the peer can
    actually establish.
    """
    try:
        for p in crud.get_bgp_peers(session):
            if p.neighbor == neighbor:
                return True
    except Exception:
        pass
    neighbor_re = re.compile(r'neighbor\s+(\d{1,3}(?:\.\d{1,3}){3})\s+as\s+\d+')
    for path in _bgp_config_paths():
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                for matched in neighbor_re.findall(fh.read()):
                    if matched == neighbor:
                        return True
        except OSError:
            continue
    return False


def _peer_asn_prefix_counts(session) -> dict:
    """``{asn: set(prefix)}`` for the enabled networks and communities.

    Only the set of distinct prefixes per ASN is needed by the peer panel
    (``export_count`` and ``asn_found``), so we no longer build the full
    ``{prefix: set of community-pairs}`` grouping that the config generator
    uses.  That keeps the cold (uncached) computation cheaper.
    """
    networks = [
        {'prefix': net.prefix, 'community_ids': net.community_ids}
        for net in crud.get_networks(session)
        if net.enabled is not False
    ]
    communities = [
        {'id': comm.id, 'name': comm.name, 'values': comm.values}
        for comm in crud.get_communities(session)
        if comm.enabled is not False
    ]
    community_by_id = {c.get('id'): c for c in communities if c.get('id') is not None}
    grouped = {}
    for net in networks:
        prefix = net.get('prefix', '')
        if not prefix:
            continue
        asns_for_prefix = set()
        for community_id in _as_community_ids(net.get('community_ids')):
            comm = community_by_id.get(community_id)
            if not comm:
                continue
            values = _effective_values(comm.get('values', ''), comm.get('name', ''))
            for pair in _community_pairs(values):
                asns_for_prefix.add(pair[0])
        for asn in asns_for_prefix:
            grouped.setdefault(asn, set()).add(prefix)
    return grouped


class _RawResponse:
    """Non-JSON WSGI response (raw HTML/CSS/JS bodies)."""

    def __init__(self, status_phrase, content_type, body):
        self.status_phrase = status_phrase
        self.content_type = content_type
        self.body = body


def _audit_action(action, user_id):
    """Best-effort audit-log write for non-CRUD actions."""
    try:
        session = get_session()
        entry = AuditLog(action=action, user_id=user_id or 'system')
        session.add(entry)
        session.commit()
    except Exception:
        pass
