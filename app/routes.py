"""WSGI API routes for bird-manager using waitress."""

import gzip
import json
import os
import sys
import time
import threading
import re
import shutil
import subprocess
import tempfile
import traceback
import ipaddress
import html
from datetime import datetime
from urllib.parse import parse_qs, unquote


# Conditional imports — work both as package and as script entry point
try:
    from .crud_operations import CRUDOperations
    from .models import Community
    from .database import (
        get_session, close_all_sessions, engine, _DB_PATH, rebind_engine,
    )
    from .parsing_engine import ParsingEngine
    from .config_generator import (
        generate_split,
        is_bundle_filename,
        _as_community_ids,
        detect_router_id,
        prefix_pairs_by_asn,
        _peer_local_asn,
        _resolve_router_id,
        _effective_values,
        _community_pairs,
    )
    from .diff_engine import DiffEngine
    from .syntax_validator import SyntaxValidator
    from .config_manager import ConfigManager
    from .validators import (
        Validator, derive_community_values,
        validate_community_name, validate_community_ref,
    )
    from .models import AuditLog, Base, BgpPeer, ensure_schema
    from .backup import backup_info, create_backup, delete_backup, restore_backup
    from sqlalchemy.exc import IntegrityError
    _package_mode = True
except ImportError:
    _parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _parent_dir not in sys.path:
        sys.path.insert(0, _parent_dir)
    from crud_operations import CRUDOperations
    from database import (
        get_session, close_all_sessions, engine, _DB_PATH, rebind_engine,
    )
    from parsing_engine import ParsingEngine
    from config_generator import (
        generate_split,
        is_bundle_filename,
        _as_community_ids,
        detect_router_id,
        prefix_pairs_by_asn,
        _peer_local_asn,
        _resolve_router_id,
        _effective_values,
        _community_pairs,
    )
    from diff_engine import DiffEngine
    from syntax_validator import SyntaxValidator
    from config_manager import ConfigManager
    from validators import (
        Validator, derive_community_values,
        validate_community_name, validate_community_ref,
    )
    from models import AuditLog, Base, BgpPeer, ensure_schema
    from backup import backup_info, create_backup, delete_backup, restore_backup
    from sqlalchemy.exc import IntegrityError
    _package_mode = False

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


def _parse_wsgi_request(environ):
    """Parse waitress WSGI environ into a simple request object."""
    request_method = environ.get('REQUEST_METHOD', 'GET')
    path = environ.get('PATH_INFO', '/')
    query_string = environ.get('QUERY_STRING', '')
    
    # Parse body from wsgi.input
    content_length = int(environ.get('CONTENT_LENGTH', 0))
    body_data = {}
    if content_length > 0:
        try:
            raw_body = environ['wsgi.input'].read(content_length)
            if isinstance(raw_body, bytes):
                raw_body = raw_body.decode('utf-8')
            body_data = json.loads(raw_body) if raw_body else {}
        except Exception:
            body_data = {}

    # Parse query string params
    try:
        query_params = parse_qs(query_string)
    except Exception:
        query_params = {}

    class SimpleRequest:
        def __init__(self, method, path, body_data, query_params):
            self.method = method
            self.path = path
            self.body_data = body_data
            self._query_params = query_params

        @property
        def args(self):
            return self._query_params

    return SimpleRequest(request_method, path, body_data, query_params)


# ==================== Access control ====================

DEFAULT_TRUSTED_SOURCES = 'auto,127.0.0.1/32,::1/128'
DEFAULT_MAX_BODY_BYTES = 1024 * 1024
MUTATING_METHODS = frozenset({'POST', 'PUT', 'DELETE', 'PATCH'})
GENERIC_ERROR_TEXT = 'Внутренняя ошибка сервера'

STATUS_REASONS = {
    400: 'Bad Request',
    403: 'Forbidden',
    404: 'Not Found',
    413: 'Payload Too Large',
    500: 'Internal Server Error',
}

SECURITY_HEADERS = (
    ('X-Content-Type-Options', 'nosniff'),
    ('X-Frame-Options', 'DENY'),
    ('Referrer-Policy', 'no-referrer'),
    (
        'Content-Security-Policy',
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'",
    ),
    ('Cache-Control', 'no-store'),
)

# Parsed allowlists are cached per raw env string so a reload is cheap while
# tests can still override the environment between calls.
_NETWORK_CACHE = {}


def _bool_env(name, default=False):
    value = (os.getenv(name) or '').strip().lower()
    if not value:
        return default
    return value in ('1', 'true', 'yes', 'on')


def _int_env(name, default):
    value = (os.getenv(name) or '').strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _primary_ipv4_prefixlen(address):
    """Prefix length of ``address`` on this host, or None when unknown."""
    try:
        result = subprocess.run(
            ['ip', '-j', '-4', 'addr', 'show', 'scope', 'global'],
            check=False, capture_output=True, text=True, timeout=5,
        )
        interfaces = json.loads(result.stdout or '[]')
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    for interface in interfaces:
        for entry in interface.get('addr_info', []):
            if entry.get('local') == address:
                prefixlen = entry.get('prefixlen')
                if isinstance(prefixlen, int):
                    return prefixlen
    return None


def _auto_source_network():
    """Network of the host's primary IPv4 address.

    Detection failures fall back to a single-host /32 — the allowlist is never
    widened silently, and docker/VPN bridge subnets are excluded because the
    address is the one used for the default route.
    """
    try:
        address = ipaddress.IPv4Address(detect_router_id())
    except (ipaddress.AddressValueError, ValueError):
        return None
    prefixlen = _primary_ipv4_prefixlen(str(address))
    if prefixlen is None:
        return ipaddress.ip_network(f'{address}/32', strict=False)
    return ipaddress.ip_network(f'{address}/{prefixlen}', strict=False)


def _parse_networks(raw):
    networks = []
    for token in (raw or '').replace(';', ',').split(','):
        token = token.strip()
        if not token:
            continue
        if token.lower() == 'auto':
            network = _auto_source_network()
            if network is not None:
                networks.append(network)
            continue
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            print(f"[WARN] ignoring invalid address entry in allowlist: {token}", flush=True)
    return tuple(networks)


def _parse_origins(raw):
    origins = set()
    for token in (raw or '').replace(';', ',').split(','):
        token = token.strip().rstrip('/')
        if token:
            origins.add(token.lower())
    return frozenset(origins)


def _networks(raw):
    cached = _NETWORK_CACHE.get(raw)
    if cached is None:
        cached = _parse_networks(raw)
        _NETWORK_CACHE[raw] = cached
    return cached


def _access_settings():
    """Resolve source/origin allowlists and request limits from the env."""
    return {
        'sources': _networks(os.getenv('BIRD_TRUSTED_SOURCES', DEFAULT_TRUSTED_SOURCES)),
        'proxies': _networks(os.getenv('BIRD_TRUSTED_PROXIES', '')),
        'origins': _parse_origins(os.getenv('BIRD_TRUSTED_ORIGINS', '')),
        'max_body': _int_env('BIRD_MAX_BODY_BYTES', DEFAULT_MAX_BODY_BYTES),
        'debug_errors': _bool_env('BIRD_DEBUG_ERRORS', False),
    }


def _parse_ip(value):
    try:
        return ipaddress.ip_address((value or '').strip())
    except ValueError:
        return None


def _client_address(environ, proxies):
    """Effective client IP and its raw text.

    ``X-Forwarded-For`` is honoured only when the peer itself is a configured
    proxy; otherwise the header is attacker-controlled and ignored.
    """
    raw = (environ.get('REMOTE_ADDR') or '').strip()
    peer = _parse_ip(raw)
    if peer is None:
        return None, raw
    if proxies and any(peer in network for network in proxies):
        forwarded = environ.get('HTTP_X_FORWARDED_FOR', '')
        for candidate in reversed([part.strip() for part in forwarded.split(',') if part.strip()]):
            candidate_ip = _parse_ip(candidate)
            if candidate_ip is None:
                continue
            if any(candidate_ip in network for network in proxies):
                continue
            return candidate_ip, candidate
    return peer, raw


def _access_denied(environ, settings, method):
    """Return (status, reason) when the request must be rejected."""
    client_ip, raw_client = _client_address(environ, settings['proxies'])
    if client_ip is None:
        return 403, f'unparseable client address {raw_client!r}'
    sources = settings['sources']
    if sources and not any(client_ip in network for network in sources):
        return 403, f'source {client_ip} is not in BIRD_TRUSTED_SOURCES'
    if method in MUTATING_METHODS:
        origins = settings['origins']
        if origins:
            origin = (environ.get('HTTP_ORIGIN') or '').strip().rstrip('/').lower()
            if origin not in origins:
                return 403, f'origin {(environ.get("HTTP_ORIGIN") or "").strip()!r} is not in BIRD_TRUSTED_ORIGINS'
    return None


def _body_too_large(environ, max_body):
    raw = (environ.get('CONTENT_LENGTH') or '').strip()
    if not raw or max_body <= 0:
        return False
    try:
        length = int(raw)
    except ValueError:
        return False
    return length > max_body


def _error_message(exc):
    """Log the failure and return text that is safe to send to the client."""
    print(f"[ERROR] {type(exc).__name__}: {exc}", flush=True)
    traceback.print_exc()
    if _access_settings()['debug_errors']:
        return str(exc)
    return GENERIC_ERROR_TEXT


def _log_access_settings(settings):
    """Report the effective access policy once at startup."""
    sources = ', '.join(str(network) for network in settings['sources']) or '(any)'
    proxies = ', '.join(str(network) for network in settings['proxies']) or '(none)'
    origins = ', '.join(sorted(settings['origins'])) or '(origin check disabled)'
    print(f"[ACCESS] trusted sources: {sources}", flush=True)
    print(f"[ACCESS] trusted proxies: {proxies}", flush=True)
    print(f"[ACCESS] trusted origins: {origins}", flush=True)
    print(f"[ACCESS] max request body: {settings['max_body']} bytes", flush=True)
    if not settings['sources']:
        print("[WARN] BIRD_TRUSTED_SOURCES is empty — every client may change the BIRD configuration", flush=True)
    if not settings['origins']:
        print("[WARN] BIRD_TRUSTED_ORIGINS is empty — cross-origin browser requests are not checked", flush=True)


#: Bodies at least this large are gzipped when the client accepts it. Below the
#: threshold the compression header costs more than it saves.
GZIP_MIN_BYTES = 16 * 1024
#: zlib level 6 keeps the CPU cost irrelevant for a request that already reads
#: the files, and the network list is highly repetitive.
GZIP_LEVEL = 6


def _wants_gzip(environ):
    """True when the client advertised gzip support."""
    accept = environ.get('HTTP_ACCEPT_ENCODING') or ''
    return any(
        part.split(';')[0].strip().lower() == 'gzip'
        for part in accept.split(',')
    )


def _maybe_gzip(body, environ, content_type):
    """Return ``(body, headers)`` with the body gzipped when that is useful.

    The panel pulls whole lists — 7802 prefixes are 963 KB of highly repetitive
    JSON, and the same compresses to 66 KB. Only the payload changes: the access
    log keeps reporting the uncompressed size, so a log line still means what it
    did before.
    """
    if len(body) < GZIP_MIN_BYTES or not _wants_gzip(environ):
        return body, []
    if content_type.startswith(('text/', 'application/json', 'image/svg')):
        compressed = gzip.compress(body, GZIP_LEVEL)
        return compressed, [('Content-Encoding', 'gzip'), ('Vary', 'Accept-Encoding')]
    return body, []


def _response_headers(content_type, extra=()):
    return [('Content-Type', content_type), *extra, *SECURITY_HEADERS]


def _respond(start_response, status_phrase, content_type, body, environ, started_at, note=None):
    """Log and emit a response with the standard security headers."""
    status_code = int(status_phrase.split(' ', 1)[0])
    elapsed_ms = round((time.time() - started_at) * 1000, 1)
    method = environ.get('REQUEST_METHOD', 'GET')
    path = environ.get('PATH_INFO', '/')
    query_string = environ.get('QUERY_STRING', '')
    suffix = f' ?{query_string}' if query_string else ''
    prefix = f'[API] {method} {path}{suffix} -> {status_code} | {len(body)} B | {elapsed_ms} ms'
    if content_type.startswith('application/json'):
        preview = body.decode('utf-8')
        if len(preview) > 200:
            preview = preview[:200] + '...'
        print(prefix, flush=True)
        if note:
            print(f'[DENY] {note}', flush=True)
        print(f'[RESP] {preview}', flush=True)
    else:
        print(prefix + f' | content-type: {content_type}', flush=True)
        if note:
            print(f'[DENY] {note}', flush=True)
    payload, extra = _maybe_gzip(body, environ, content_type)
    start_response(status_phrase, _response_headers(content_type, extra))
    return [payload]


def _status_phrase(code):
    return f'{code} {STATUS_REASONS.get(code, "Error")}'


def _create_wsgi_app():
    """Create the WSGI application callable."""
    def wsgi_app(environ, start_response):
        request_started_at = time.time()
        settings = _access_settings()
        method = environ.get('REQUEST_METHOD', 'GET').upper()

        # The access gate runs before the body is read, so a rejected or
        # oversized request never allocates anything.
        denial = _access_denied(environ, settings, method)
        if denial is None and _body_too_large(environ, settings['max_body']):
            denial = (413, 'request body exceeds BIRD_MAX_BODY_BYTES')
        if denial is not None:
            code, reason = denial
            body = json.dumps({'error': reason, 'status': 'error'}).encode('utf-8')
            return _respond(
                start_response, _status_phrase(code), 'application/json', body,
                environ, request_started_at, note=reason,
            )

        request = _parse_wsgi_request(environ)

        # Find matching route
        matched_handler = None
        for entry in ROUTES:
            if entry['method'] != request.method.upper():
                continue
            
            pattern = entry['pattern']
            # Convert Flask-style pattern to regex
            param_names = []
            regex_parts = []
            i = 0
            while i < len(pattern):
                if pattern[i] == '<' and ':' in pattern[i:i+20]:
                    end = pattern.index('>', i)
                    param_type, param_name = pattern[i+1:end].split(':', 1)
                    param_names.append(param_name)
                    regex_parts.append(r'\d+' if param_type == 'int' else r'[^\s]+')
                    i = end + 1
                else:
                    regex_parts.append(re.escape(pattern[i]))
                    i += 1
            
            regex = '^' + ''.join(regex_parts) + '$'
            match = re.match(regex, request.path)
            
            if match:
                matched_handler = entry['handler']
                break

        def _send(status_phrase, content_type, body):
            """Log the outgoing response to stdout and pass it through WSGI."""
            return _respond(start_response, status_phrase, content_type, body,
                            environ, request_started_at)

        if not matched_handler:
            return _send('404 Not Found', 'application/json', json.dumps({'error': 'Not found'}).encode('utf-8'))

        try:
            result = matched_handler(request=request)
            if isinstance(result, _RawResponse):
                return _send(result.status_phrase, result.content_type, result.body)
            if isinstance(result, tuple):
                body_obj, status_code = result
                return _send(f'{status_code} OK', 'application/json', json.dumps(body_obj).encode('utf-8'))
            elif isinstance(result, dict):
                return _send('200 OK', 'application/json', json.dumps(result).encode('utf-8'))
            else:
                return _send('500 Internal Server Error', 'application/json', json.dumps({'error': 'Invalid handler response'}).encode('utf-8'))
        except Exception as e:
            print(f"[API] Unhandled exception for {request.method} {request.path}: {e}", flush=True)
            message = _error_message(e)
            return _send(_status_phrase(500), 'application/json',
                         json.dumps({'error': message, 'status': 'error'}).encode('utf-8'))
        finally:
            # Close DB sessions opened during this request — prevents QueuePool
            # exhaustion under concurrent dashboard polling (R4.1.3 auto refresh).
            close_all_sessions()

    return wsgi_app


class _RawResponse:
    """Non-JSON WSGI response (raw HTML/CSS/JS bodies)."""

    def __init__(self, status_phrase, content_type, body):
        self.status_phrase = status_phrase
        self.content_type = content_type
        self.body = body


# Project root = parent of the app/ package directory.
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(_APP_ROOT, 'templates')
STATIC_DIR = os.path.join(_APP_ROOT, 'static')

_MIME_TYPES = {
    '.css': 'text/css',
    '.js': 'text/javascript',
    '.html': 'text/html',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
}


def _app_version():
    """Shipped version from the VERSION file (dev and .deb layout)."""
    version_path = os.path.join(_APP_ROOT, 'VERSION')
    try:
        with open(version_path, 'r', encoding='utf-8') as fh:
            version = fh.read().strip()
        return version or '?'
    except OSError:
        return '?'


def _audit_user():
    return (os.getenv('USERNAME') or '').strip()[:50] or 'User'


@route('/', method='GET')
def serve_dashboard(request):
    """Render the dashboard HTML page (Module 4)."""
    html_path = os.path.join(TEMPLATE_DIR, 'dashboard.html')
    with open(html_path, 'r', encoding='utf-8') as fh:
        body = fh.read()
    body = body.replace('{{VERSION}}', _app_version())
    body = body.replace('{{USERNAME_HTML}}', html.escape(_audit_user(), quote=True))
    return _RawResponse('200 OK', 'text/html; charset=utf-8', body.encode('utf-8'))


@route('/static/<name:path>', method='GET')
def serve_static(request):
    """Serve files under static/ with the correct Content-Type.

    404 for missing files or paths outside the static directory.
    """
    rel_path = unquote(request.path[len('/static/'):])
    file_path = os.path.normpath(os.path.join(STATIC_DIR, rel_path))
    if not file_path.startswith(os.path.abspath(STATIC_DIR)):
        return _RawResponse('404 Not Found', 'text/plain', b'Not found')
    if not os.path.isfile(file_path):
        return _RawResponse('404 Not Found', 'text/plain', b'Not found')
    mime = _MIME_TYPES.get(os.path.splitext(file_path)[1], 'application/octet-stream')
    with open(file_path, 'rb') as fh:
        body = fh.read()
    return _RawResponse('200 OK', mime, body)


# ==================== Routes Definition ====================


# --- Configuration Management Routes ---

@route('/api/config/live', method='GET')
def get_config_live(request):
    """GET /api/config/live — return the live config file content."""
    try:
        filename = (request.args.get('filename') or ['networks.conf'])[0]
        if not _is_editable_config(filename):
            return {'error': 'Only networks.conf is editable', 'code': 'editable_only_networks_conf'}, 400
        live_file = os.path.join(_LIVE_PATH, filename)
        if not os.path.exists(live_file):
            return {'content': None, 'filename': filename}, 200
        # Explicit UTF-8, and a read failure reported as such: this endpoint
        # feeds the editor, so an undecodable file must not surface as a 500.
        with open(live_file, 'r', encoding='utf-8') as f:
            content = f.read()
        return {'content': content, 'filename': filename}, 200
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


@route('/api/config/preview', method='POST')
def preview_config(request):
    """POST /api/config/preview — preview changes without applying.

    Strips managed lines from the user's edited content, merges in fresh
    database-derived blocks, and returns the unified live→merged diff.
    """
    try:
        data = request.body_data if request.body_data else {}
        filename = data.get('filename', 'networks.conf')
        if not _is_editable_config(filename):
            return {'error': 'Only networks.conf is editable', 'code': 'editable_only_networks_conf'}, 400
        user_content = data.get('content', '')

        session = get_session()
        db_networks = [
            {
                'prefix': n.prefix, 'description': n.description, 'status': n.status,
                'enabled': n.enabled,
                'community_ids': _as_community_ids(n.community_ids),
            }
            for n in crud.get_networks(session)
        ]
        db_communities = [
            {'id': c.id, 'name': c.name, 'values': c.values, 'description': c.description, 'enabled': c.enabled}
            for c in crud.get_communities(session)
        ]
        db_bgp_peers = _db_bgp_peers(session)

        merged, diff_output = config_manager.preview_diff(
            filename, user_content, db_networks, db_communities, bgp_peers=db_bgp_peers
        )
        editor_content, editor_rows = config_manager.build_editor_view(
            filename, user_content, merged
        )

        # Live syntax validation of the merged content, with error rows mapped
        # to editor coordinates so the UI can highlight the offending line.
        syntax = None
        syntax_error_rows = []
        try:
            if config_manager.split_mode:
                preview_dir = tempfile.mkdtemp(prefix='preview-', dir=config_manager.staged_path)
                preview_networks = os.path.join(preview_dir, 'networks.conf')
                with open(preview_networks, 'w', encoding='utf-8') as f:
                    f.write(merged)
                # Same include block as the published entrypoint and the Apply
                # validation, with only networks.conf swapped for the edited
                # text. Building a separate list here silently missed generated
                # files and reported phantom parse errors.
                preview_main = os.path.join(preview_dir, 'main.conf')
                with open(preview_main, 'w', encoding='utf-8') as f:
                    f.write(config_manager._validation_include_block(
                        config_manager.staged_path, networks_path=preview_networks
                    ) + '\n')
                try:
                    syntax = SyntaxValidator().validate_content(
                        merged, bird_config_path=preview_main
                    )
                finally:
                    shutil.rmtree(preview_dir, ignore_errors=True)
            else:
                fd, tmp_path = tempfile.mkstemp(
                    prefix='preview_', suffix='.conf', dir=config_manager.staged_path
                )
                try:
                    with os.fdopen(fd, 'w', encoding='utf-8') as f:
                        f.write(merged)
                    syntax = SyntaxValidator().validate_content(
                        merged, bird_config_path=tmp_path
                    )
                finally:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
        except OSError:
            syntax = None

        if syntax and not syntax.get('valid'):
            syntax_error_rows = config_manager._editor_error_rows(
                user_content, merged, filename,
                syntax.get('error_lines', []), syntax.get('error_files', []),
            )

        return {
            'diff': diff_output,
            'merged': merged,
            'editor_content': editor_content,
            'rows': editor_rows,
            'syntax': syntax,
            'syntax_error_rows': syntax_error_rows,
            'filename': filename,
        }, 200
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


@route('/api/config/overview', method='GET')
def get_config_overview(request):
    """GET /api/config/overview — return the multi-file config panel model."""
    try:
        return config_manager.get_config_overview(), 200
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


# --- Existing routes below ---

@route('/api/networks', method='GET')
def get_networks(request):
    """Get all networks from the database."""
    try:
        session = get_session()
        networks = crud.get_networks(session)
        network_list = [{
            'id': net.id, 'prefix': net.prefix, 'description': net.description,
            'status': net.status, 'community_ids': net.community_ids,
            'enabled': net.enabled,
        } for net in networks]
        return {'networks': network_list, 'count': len(network_list)}, 200
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


@route('/api/networks/count', method='GET')
def get_network_count(request):
    """Get the number of network records without returning their data.

    Used by the dashboard to show collapsed lists: only the count is
    loaded on startup, list contents are fetched lazily on expand.
    """
    try:
        session = get_session()
        count = crud.count_networks(session)
        return {'count': count}, 200
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


def _prefix_review(prefix):
    """Build the 409 body for a prefix that is not already canonical.

    Returns ``None`` when the prefix is fine and the caller may proceed. The
    same shape is used by both the bulk parser and the single-record routes so
    the dialog is driven by one contract.
    """
    verdict = parser.classify_token(prefix)
    if verdict['status'] == 'valid':
        return None
    return {
        'error': 'Некорректный префикс',
        'code': 'invalid_prefix',
        'needs_confirmation': True,
        'invalid': [{
            'input': (prefix or '').strip(),
            'suggestion': verdict['suggestion'],
            'reason': verdict['reason'],
        }],
    }


@route('/api/networks', method='POST')
def create_network(request):
    """Create a new network record."""
    try:
        data = request.body_data if request.body_data else {}
        if not data or 'prefix' not in data:
            return {'error': 'Missing required field: prefix', 'code': 'missing_prefix'}, 400
        if 'enabled' in data and not isinstance(data['enabled'], bool):
            return {'error': 'enabled must be a boolean', 'code': 'enabled_not_boolean'}, 400

        review = _prefix_review(data['prefix'])
        if review is not None:
            return review, 409

        session = get_session()
        network = crud.create_network(
            session=session,
            prefix=data['prefix'],
            description=data.get('description', ''),
            status=data.get('status', 'pending'),
            community_ids=data.get('community_ids'),
            enabled=data.get('enabled', True),
        )
        _regenerate_staged(session)
        return {'network': {
            'id': network.id, 'prefix': network.prefix, 'description': network.description,
            'status': network.status, 'community_ids': network.community_ids,
            'enabled': network.enabled,
        }}, 201
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


@route('/api/networks/<int:network_id>', method='PUT')
def update_network(request):
    """Update an existing network record."""
    try:
        data = request.body_data if request.body_data else {}
        if 'enabled' in data and not isinstance(data['enabled'], bool):
            return {'error': 'enabled must be a boolean', 'code': 'enabled_not_boolean'}, 400
        if 'prefix' in data:
            review = _prefix_review(data['prefix'])
            if review is not None:
                return review, 409
        network_id = int(request.path.split('/')[-1])

        session = get_session()
        existing = crud.get_network_by_id(session, network_id)
        if not existing:
            return {'error': 'Network not found', 'code': 'network_not_found'}, 404

        network = crud.update_network(session=session, network_id=network_id, **data)
        if network:
            _regenerate_staged(session)
            return {'network': {
                'id': network.id, 'prefix': network.prefix, 'description': network.description,
                'status': network.status, 'community_ids': network.community_ids,
                'enabled': network.enabled,
            }}, 200
        return {'error': 'Network not found', 'code': 'network_not_found'}, 404
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


@route('/api/networks/<int:network_id>', method='DELETE')
def delete_network(request):
    """Delete a network record."""
    try:
        network_id = int(request.path.split('/')[-1])

        session = get_session()
        existing = crud.get_network_by_id(session, network_id)
        if not existing:
            return {'error': 'Network not found', 'code': 'network_not_found'}, 404

        network = crud.delete_network(session=session, network_id=network_id)
        if network:
            _regenerate_staged(session)
            return {
                'message': 'Network deleted',
                'network': {'id': network.id, 'prefix': network.prefix},
            }, 200
        return {'error': 'Network not found', 'code': 'network_not_found'}, 404
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


@route('/api/networks/bulk', method='POST')
def bulk_import_networks(request):
    """Bulk import networks from various input formats.

    The parser splits the batch in two: prefixes that are already canonical
    come back in ``parsed_networks``, everything else in ``invalid`` as
    ``{'input', 'suggestion', 'reason'}`` entries. The client shows those in
    the CIDR review dialog and either re-sends the suggestions or drops them;
    nothing is written by this call.
    """
    try:
        data = request.body_data if request.body_data else {}
        raw_data = data.get('raw_data', [])
        input_format = data.get('format', 'text')

        result = parser.parse_and_map(raw_data, input_format=input_format)
        return {
            'parsed_networks': result['networks'],
            'parsed_communities': result['communities'],
            'invalid': result['invalid'],
        }, 200
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


@route('/api/networks/bulk/commit', method='POST')
def bulk_commit_networks(request):
    """Commit already-parsed networks to the database with deduplication.

    Accepts the output of POST /api/networks/bulk (parse_and_map) and
    persists only new records; duplicates by prefix are skipped.
    """
    try:
        data = request.body_data if request.body_data else {}
        networks = data.get('networks', [])
        user_id = data.get('user_id', 'system')

        if not isinstance(networks, list) or len(networks) == 0:
            return {'error': 'Empty network list', 'code': 'empty_network_list'}, 400

        session = get_session()
        result = crud.bulk_commit_networks(session=session, networks=networks, user_id=user_id)
        _regenerate_staged(session)
        return {
            'created': result['created'],
            'skipped_duplicates': result['skipped_duplicates'],
            'skipped_invalid': result.get('skipped_invalid', 0),
            'audit_id': result['audit_id'],
        }, 201
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


@route('/api/communities', method='GET')
def get_communities(request):
    """Get all communities from the database."""
    try:
        session = get_session()
        communities = crud.get_communities(session)
        community_list = [{
            'id': comm.id, 'name': comm.name, 'values': comm.values, 'description': comm.description,
            'enabled': comm.enabled,
        } for comm in communities]
        return {'communities': community_list, 'count': len(community_list)}, 200
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


@route('/api/communities/count', method='GET')
def get_community_count(request):
    """Get the number of community records without returning their data.

    Used by the dashboard to show collapsed lists: only the count is
    loaded on startup, list contents are fetched lazily on expand.
    """
    try:
        session = get_session()
        count = crud.count_communities(session)
        return {'count': count}, 200
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


@route('/api/communities', method='POST')
def create_community(request):
    """Create a new community record.

    The name is the source of truth for the community values: it is validated
    as ``ASN:VALUE`` or a bare ``ASN``, and when the caller supplies no
    ``values`` the name's own value is stored so later renames cannot silently
    change what a prefix is tagged with.
    """
    try:
        data = request.body_data if request.body_data else {}
        if not data or 'name' not in data:
            return {'error': 'Missing required field: name', 'code': 'missing_name'}, 400
        if 'enabled' in data and not isinstance(data['enabled'], bool):
            return {'error': 'enabled must be a boolean', 'code': 'enabled_not_boolean'}, 400

        name, derived, error, code = validate_community_name(data['name'])
        if error:
            return {'error': error, 'code': code}, 400

        raw_values = data.get('values')
        if raw_values is not None and str(raw_values).strip():
            values, error, code = validate_community_ref(raw_values)
            if error:
                return {'error': error, 'code': code}, 400
        else:
            values = derived

        session = get_session()
        if name == '65534':
            community = crud.ensure_default_community(session)
            _regenerate_staged(session)
            return {'community': {
                'id': community.id, 'name': community.name, 'values': community.values, 'description': community.description,
                'enabled': community.enabled,
            }}, 200
        existing = session.query(Community).filter(Community.name == name).first()
        if existing is not None:
            _regenerate_staged(session)
            return {'community': {
                'id': existing.id, 'name': existing.name, 'values': existing.values, 'description': existing.description,
                'enabled': existing.enabled,
            }}, 200
        community = crud.create_community(
            session=session,
            name=name,
            values=values,
            description=data.get('description', ''),
            enabled=data.get('enabled', True),
        )
        _regenerate_staged(session)
        return {'community': {
            'id': community.id, 'name': community.name, 'values': community.values, 'description': community.description,
            'enabled': community.enabled,
        }}, 201
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


@route('/api/communities/<int:community_id>', method='PUT')
def update_community(request):
    """Update an existing community record.

    A renamed community keeps its values only when they were derived from the
    old name: then they are re-derived from the new one, because they carry no
    information of their own. A value the operator set explicitly, or a list of
    them, is left alone.
    """
    try:
        data = request.body_data if request.body_data else {}
        if 'enabled' in data and not isinstance(data['enabled'], bool):
            return {'error': 'enabled must be a boolean', 'code': 'enabled_not_boolean'}, 400
        community_id = int(request.path.split('/')[-1])

        session = get_session()
        existing = crud.get_community_by_id(session, community_id)
        if not existing:
            return {'error': 'Community not found', 'code': 'community_not_found'}, 404

        updates = dict(data)

        if 'values' in data and str(data.get('values') or '').strip():
            values, error, code = validate_community_ref(data['values'])
            if error:
                return {'error': error, 'code': code}, 400
            updates['values'] = values
        else:
            updates.pop('values', None)

        if 'name' in data:
            name, derived, error, code = validate_community_name(data['name'])
            if error:
                return {'error': error, 'code': code}, 400
            updates['name'] = name
            # Auto-managed means "equals what the old name implies". Anything
            # else was chosen on purpose and survives the rename.
            if (existing.values or '').strip() == (derive_community_values(existing.name) or ''):
                updates['values'] = derived

        if 'name' in updates:
            duplicate = session.query(Community).filter(
                Community.name == updates['name'],
                Community.id != community_id,
            ).first()
            if duplicate is not None:
                return {'error': 'Community name already exists', 'code': 'duplicate_community_name'}, 409

        community = crud.update_community(
            session=session, community_id=community_id, **updates
        )
        if community:
            _regenerate_staged(session)
            return {'community': {
                'id': community.id, 'name': community.name, 'values': community.values, 'description': community.description,
                'enabled': community.enabled,
            }}, 200
        return {'error': 'Community not found', 'code': 'community_not_found'}, 404
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


@route('/api/communities/<int:community_id>', method='DELETE')
def delete_community(request):
    """Delete a community record."""
    try:
        community_id = int(request.path.split('/')[-1])

        session = get_session()
        existing = crud.get_community_by_id(session, community_id)
        if not existing:
            return {'error': 'Community not found', 'code': 'community_not_found'}, 404

        community = crud.delete_community(
            session=session, community_id=community_id
        )
        if community:
            _regenerate_staged(session)
            return {
                'message': 'Community deleted',
                'community': {'id': community.id, 'name': community.name},
            }, 200
        return {'error': 'Community not found', 'code': 'community_not_found'}, 404
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


# --- BGP Peer Routes ---

@route('/api/bgp_peers', method='GET')
def get_bgp_peers(request):
    """Get all BGP peers from the database."""
    try:
        session = get_session()
        peers = crud.get_bgp_peers(session)
        asn_prefixes = _cached_peer_asn_prefix_counts(session)
        # The router id the generator wrote into the bundle, so a config we hand
        # out for the far end references the address BIRD really uses.
        router_id = _resolve_router_id(_bird_settings()['router_id'])
        peer_list = [{
            'id': p.id, 'name': p.name, 'local_as': p.local_as,
            'neighbor': p.neighbor, 'neighbor_as': p.neighbor_as,
            'status': p.status,
            'export_all': bool(p.export_all),
            # What this peer's own ASN-scoped filter will carry. An ASN with no
            # community exports nothing, and the UI says so instead of leaving
            # an empty panel to be discovered on the MikroTik.
            'export_count': len(asn_prefixes.get(
                _peer_local_asn({'local_as': p.local_as}), {}
            )),
            'asn_found': _peer_local_asn({'local_as': p.local_as}) in asn_prefixes,
            'router_id': router_id,
        } for p in peers]
        return {'bgp_peers': peer_list, 'count': len(peer_list)}, 200
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


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


@route('/api/bgp_peers/count', method='GET')
def get_bgp_peer_count(request):
    """Number of BGP peer records, without their contents.

    Mirrors /api/networks/count: the peer panel starts collapsed and shows only
    a counter, so the list itself is fetched lazily on first expand. The path
    cannot collide with /api/bgp_peers/<int:peer_id> — route patterns are
    anchored, and "count" is not a digit.
    """
    try:
        session = get_session()
        return {'count': crud.count_bgp_peers(session)}, 200
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


@route('/api/bgp_peers', method='POST')
def create_bgp_peer(request):
    """Create a BGP peer after full validation.

    Fields: name (BIRD identifier), local_as (default ``65500``, any valid
    ASN), neighbor (strict IPv4, no /mask, not already used by another peer /
    active config block), neighbor_as (any valid ASN that is not already used
    by another peer / active config block).
    """
    try:
        data = request.body_data if request.body_data else {}

        name, error, code = Validator.validate_bird_identifier(data.get('name'))
        if error:
            return {'error': 400, 'message': error, 'code': code}, 400
        neighbor_as, error, code = Validator.validate_asn(data.get('neighbor_as'), 'Neighbor ASN')
        if error:
            return {'error': 400, 'message': error, 'code': code}, 400
        neighbor, error, code = Validator.validate_ipv4(data.get('neighbor'))
        if error:
            return {'error': 400, 'message': error, 'code': code}, 400
        local_as_value = data.get('local_as') or '65500'
        local_as, error, code = Validator.validate_asn(local_as_value, 'Local ASN')
        if error:
            return {'error': 400, 'message': error, 'code': code}, 400

        session = get_session()
        if _asn_already_in_use(session, neighbor_as):
            return {
                'error': 400,
                'message': f'ASN {neighbor_as} уже используется другим пиром (peer id / конфиг)',
                'code': 'asn_already_in_use',
            }, 400
        if _neighbor_already_in_use(session, neighbor):
            return {
                'error': 400,
                'message': f'Neighbor {neighbor} уже используется другим пиром (peer id / конфиг)',
                'code': 'neighbor_already_in_use',
            }, 400

        peer = crud.create_bgp_peer(
            session=session,
            name=name,
            local_as=local_as,
            neighbor=neighbor,
            neighbor_as=neighbor_as,
            status=data.get('status', 'active'),
            user_id=data.get('user_id', 'system'),
            export_all=bool(data.get('export_all', False)),
        )
        _regenerate_staged(session)
        # Same fields the list endpoint returns, so the form can warn about a
        # missing ASN straight from the POST response instead of waiting for a
        # refresh.
        asn = _peer_local_asn({'local_as': peer.local_as})
        asn_prefixes = _cached_peer_asn_prefix_counts(session)
        return {'bgp_peer': {
            'id': peer.id, 'name': peer.name, 'local_as': peer.local_as,
            'neighbor': peer.neighbor, 'neighbor_as': peer.neighbor_as,
            'status': peer.status,
            'export_all': bool(peer.export_all),
            'export_count': len(asn_prefixes.get(asn, {})),
            'asn_found': asn in asn_prefixes,
        }}, 201
    except IntegrityError:
        # Lost a race against a concurrent create: the unique constraint is the
        # authority, report it the same way the pre-insert check does.
        return {
            'error': 400,
            'message': 'Peer с таким neighbor уже существует',
            'code': 'neighbor_duplicate',
        }, 400
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


@route('/api/bgp_peers/<int:peer_id>', method='DELETE')
def delete_bgp_peer(request):
    """Delete a BGP peer record."""
    try:
        peer_id = int(request.path.split('/')[-1])

        session = get_session()
        existing = crud.get_bgp_peer_by_id(session, peer_id)
        if not existing:
            return {'error': 'BGP peer not found', 'code': 'bgp_peer_not_found'}, 404

        peer = crud.delete_bgp_peer(
            session=session, peer_id=peer_id, user_id='system'
        )
        if peer:
            _regenerate_staged(session)
            return {
                'message': 'BGP peer deleted',
                'bgp_peer': {'id': peer.id, 'name': peer.name},
            }, 200
        return {'error': 'BGP peer not found', 'code': 'bgp_peer_not_found'}, 404
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


@route('/api/config/generate', method='POST')
def generate_config(request):
    """Generate BIRD configuration from database data."""
    try:
        session = get_session()
        config_path = _regenerate_staged(session)
        if config_path is None:
            return {'error': 'Config generation failed', 'code': 'config_generation_failed'}, 500
        return {'status': 'success', 'config_path': config_path}, 200
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


@route('/api/config/diff', method='GET')
def get_config_diff(request):
    """Get diff between live and staged configuration."""
    try:
        filename = request.args.get('filename', ['networks.conf'])[0] if 'filename' in request.args else 'networks.conf'
        if not _is_editable_config(filename):
            return {'error': 'Only networks.conf is editable', 'code': 'editable_only_networks_conf'}, 400
        diff_output = diff_engine.get_diff(filename)
        return {'diff': diff_output, 'filename': filename}, 200
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


@route('/api/config/file-rows', method='GET')
def get_config_file_rows(request):
    """Aligned live→staged rows of one generated bundle file.

    Read-only. Each row carries both sides, so the panel can render two
    columns of equal height and align them by index — a file pane next to a
    unified diff can never align, because the diff carries its own header
    lines and a different number of lines.

    The name is checked against the bundle's own file list, so nothing from the
    request can reach the filesystem. The counters and ``total_rows`` are
    exact; only the rows are capped, and ``truncated`` reports it.
    """
    try:
        filename = request.args.get('filename', [''])[0] if 'filename' in request.args else ''
        if not filename or not is_bundle_filename(filename):
            return {'error': 'Unknown bundle file', 'code': 'unknown_bundle_file'}, 400
        return config_manager.get_file_rows_view(filename), 200
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


@route('/api/config/file-diff', method='GET')
def get_config_file_diff(request):
    """Read-only live→staged diff for one generated bundle file.

    Read-only counterpart of the editable-file diff above. The name is checked
    against the bundle's own file list, so nothing from the request can reach
    the filesystem. The change counts are exact; the diff text is capped and
    ``truncated`` reports whether it was, with the full size in ``total_lines``.
    """
    try:
        filename = request.args.get('filename', [''])[0] if 'filename' in request.args else ''
        if not filename or not is_bundle_filename(filename):
            return {'error': 'Unknown bundle file', 'code': 'unknown_bundle_file'}, 400
        return config_manager.get_file_diff_view(filename), 200
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


@route('/api/config/diff/all', method='GET')
def get_all_config_diffs(request):
    """Get diffs for all files between live and staged."""
    try:
        all_diffs = diff_engine.get_all_diffs()
        count_diff = diff_engine.get_file_count_diff()
        return {'diffs': all_diffs, 'summary': count_diff}, 200
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


@route('/api/config/syntax-check', method='GET')
def check_syntax(request):
    """Check syntax validation of staged configuration."""
    try:
        filename = request.args.get('filename', ['networks.conf'])[0] if 'filename' in request.args else 'networks.conf'
        if not _is_editable_config(filename):
            return {'error': 'Only networks.conf is editable', 'code': 'editable_only_networks_conf'}, 400
        if config_manager.split_mode:
            result = config_manager.validate_staged_bundle()
        else:
            validator = SyntaxValidator()
            validator.staged_path = diff_engine.staged_path
            result = validator.validate_staged_config(filename)
        return result, 200
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


@route('/api/config/staged', method='GET')
def get_staged_config(request):
    """Get the current staged configuration content."""
    try:
        filename = request.args.get('filename', ['networks.conf'])[0] if 'filename' in request.args else 'networks.conf'
        if not _is_editable_config(filename):
            return {'error': 'Only networks.conf is editable', 'code': 'editable_only_networks_conf'}, 400
        content = diff_engine.get_staged_content(filename)
        return {'content': content, 'filename': filename}, 200
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


@route('/api/config/apply', method='POST')
def apply_config(request):
    """POST /api/config/apply — apply config to live and reload BIRD.

    If `content` is present in the body it is merged with database-derived
    blocks (variant B, see ConfigManager.apply_config_from_content);
    otherwise the existing staged file is applied.
    """
    try:
        data = request.body_data if request.body_data else {}
        filename = data.get('filename', 'networks.conf')
        if not _is_editable_config(filename):
            return {'error': 'Only networks.conf is editable', 'code': 'editable_only_networks_conf'}, 400
        user_id = data.get('user_id', 'system')
        user_content = data.get('content', '')
        has_content = 'content' in data and data.get('content') is not None

        if has_content:
            session = get_session()
            networks = [
                {
                    'prefix': n.prefix, 'description': n.description, 'status': n.status,
                    'enabled': n.enabled,
                    'community_ids': _as_community_ids(n.community_ids),
                }
                for n in crud.get_networks(session)
            ]
            communities = [
                {'id': c.id, 'name': c.name, 'values': c.values, 'description': c.description, 'enabled': c.enabled}
                for c in crud.get_communities(session)
            ]
            bgp_peers = _db_bgp_peers(session)
            result = config_manager.apply_config_from_content(
                filename, user_content, user_id, networks, communities, bgp_peers=bgp_peers
            )
        else:
            result = config_manager.apply_config(filename=filename, user_id=user_id)
        status_code = 200 if result['status'] in ('success', 'success_with_warning') else (404 if result.get('status') == 'file_not_found' else 400)
        return result, status_code
    except Exception as e:
        return {'error': _error_message(e), 'status': 'error', 'code': 'server_error'}, 500


@route('/api/config/rollback', method='POST')
def rollback_config(request):
    """Rollback the last applied configuration."""
    try:
        filename = request.args.get('filename', [None])[0] if 'filename' in request.args else None
        if not filename:
            return {'error': 'Missing required parameter: filename', 'code': 'missing_filename'}, 400
        if not _is_editable_config(filename):
            return {'error': 'Only networks.conf is editable', 'code': 'editable_only_networks_conf'}, 400

        user_id = request.args.get('user_id', ['system'])[0] if 'user_id' in request.args else 'system'
        result = config_manager.rollback_last_apply(filename=filename, user_id=user_id)
        if result['status'] in ('success', 'success_with_warning'):
            status_code = 200
        elif result.get('status') == 'backup_not_found':
            status_code = 404
        else:
            status_code = 400
        return result, status_code
    except Exception as e:
        return {'error': _error_message(e), 'status': 'error', 'code': 'server_error'}, 500


@route('/api/config/status', method='GET')
def get_config_status(request):
    """Get the last apply metadata for a configuration file."""
    try:
        filename = request.args.get('filename', ['networks.conf'])[0] if 'filename' in request.args else 'networks.conf'
        status_data = config_manager.get_apply_status(filename)
        # Add 'status' key to match API documentation
        status_data['status'] = 'success' if any(status_data.values()) else 'no_history'
        status_data['backup'] = backup_info(_DB_PATH)
        return status_data, 200
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


def _audit_action(action, user_id):
    """Best-effort audit-log write for non-CRUD actions."""
    try:
        session = get_session()
        entry = AuditLog(action=action, user_id=user_id or 'system')
        session.add(entry)
        session.commit()
    except Exception:
        pass


@route('/api/backup', method='POST')
def backup_snapshot(request):
    """POST /api/backup — create a rotating full snapshot (@@ db+staged+live)."""
    try:
        data = request.body_data if request.body_data else {}
        info = create_backup(_LIVE_PATH, _STAGED_PATH, _DB_PATH)
        _audit_action('BACKUP', data.get('user_id'))
        return {'status': 'success', 'backup': info}, 200
    except Exception as e:
        return {'error': _error_message(e), 'status': 'error', 'code': 'server_error'}, 500


def _restore_backup_request(request, backup_id=None):
    try:
        data = request.body_data if request.body_data else {}
        engine.dispose()
        try:
            result = restore_backup(_LIVE_PATH, _STAGED_PATH, _DB_PATH, backup_id=backup_id)
        except ValueError as exc:
            return {'error': str(exc), 'status': 'error', 'code': 'restore_error'}, 400
        if result['status'] != 'success':
            return result, 400
        ensure_schema(engine)
        restored_session = get_session()
        _regenerate_staged(restored_session)
        reload_success, reload_warning, reload_detail = config_manager._attempt_bird_reload()
        config_manager._write_apply_state(reload_warning, reload_detail)
        _audit_action('RESTORE_BACKUP', data.get('user_id'))
        return {
            'status': 'success',
            'message': result['message'],
            'reload_warning': reload_warning,
            'reload_detail': reload_detail,
            'backup': backup_info(_DB_PATH),
        }, 200
    except Exception as e:
        return {'error': _error_message(e), 'status': 'error', 'code': 'server_error'}, 500


@route('/api/backup/restore', method='POST')
def backup_restore(request):
    """POST /api/backup/restore — restore the newest snapshot."""
    return _restore_backup_request(request)


@route('/api/backup/<string:backup_id>/restore', method='POST')
def backup_restore_selected(request):
    """POST /api/backup/<backup_id>/restore — restore one selected snapshot."""
    backup_id = request.path.split('/')[-2]
    return _restore_backup_request(request, backup_id=backup_id)


@route('/api/backup/<string:backup_id>', method='DELETE')
def backup_delete(request):
    """DELETE /api/backup/<backup_id> — delete one snapshot."""
    try:
        backup_id = request.path.rsplit('/', 1)[-1]
        result = delete_backup(backup_id, _DB_PATH)
    except ValueError as exc:
        return {'error': str(exc), 'status': 'error', 'code': 'restore_error'}, 400
    if result['status'] != 'success':
        return result, 404
    data = request.body_data if request.body_data else {}
    _audit_action('DELETE_BACKUP', data.get('user_id'))
    return result, 200


@route('/api/bird/reload', method='POST')
def bird_reload(request):
    """POST /api/bird/reload — reconfigure BIRD through birdc."""
    try:
        data = request.body_data if request.body_data else {}
        success, warning, detail = config_manager._attempt_bird_reload()
        config_manager._write_apply_state(warning, detail)
        if success:
            _audit_action('BIRD_RECONFIGURE', data.get('user_id'))
        return {
            'status': 'ok' if success else 'error',
            'bird_running': os.path.exists(config_manager._bird_socket_path()),
            'error': detail,
        }, 200
    except Exception as e:
        return {'status': 'error', 'error': str(e), 'code': 'server_error'}, 200


@route('/api/bird/protocols', method='GET')
def get_bird_protocols(request):
    """GET /api/bird/protocols — live snapshot from `birdc show protocols all`.

    Read-only view of the running daemon. Every BGP session found in BIRD is
    reported, including protocols added to bird.conf by hand; peers configured
    in the panel but missing from the BIRD output are appended with
    ``in_bird: false`` so the discrepancy stays visible.
    """
    try:
        snapshot = config_manager._bird_protocols()
        managed_names = set()
        try:
            session = get_session()
            managed_names = {p.name for p in crud.get_bgp_peers(session)}
        except Exception:
            managed_names = set()

        peers = []
        for record in snapshot['bgp']:
            record['managed'] = record['name'] in managed_names
            peers.append(record)
        # Peers from the panel are only appended when BIRD answered: "not in
        # BIRD" is a statement about the daemon, so it needs a readable
        # protocol list behind it.
        if snapshot['error'] is None:
            for name in sorted(managed_names):
                if any(peer['name'] == name for peer in peers):
                    continue
                peers.append({
                    'name': name,
                    'state': None,
                    'bgp_state': None,
                    'channel_state': None,
                    'neighbor': None,
                    'neighbor_as': None,
                    'local_as': None,
                    'since': None,
                    'info': None,
                    'last_error': None,
                    'imported': None,
                    'exported': None,
                    'in_bird': False,
                    'managed': True,
                })
        return {
            'status': 'error' if snapshot['error'] else 'ok',
            'bird_running': snapshot['bird_running'],
            'error': snapshot['error'],
            'peers': peers,
            'service': snapshot['service'],
        }, 200
    except Exception as e:
        return {'status': 'error', 'error': _error_message(e), 'code': 'server_error'}, 200


@route('/api/audit', method='GET')
def get_audit_log(request):
    """Get recent audit log entries."""
    try:
        limit_str = request.args.get('limit', ['50'])[0] if 'limit' in request.args else '50'
        session = get_session()
        audit_entries = crud.get_audit_log(session, int(limit_str))

        log_list = [{
            'id': entry.id, 'action': entry.action, 'timestamp': str(entry.timestamp),
            'user_id': entry.user_id, 'diff_summary': entry.diff_summary,
        } for entry in audit_entries]

        return {'audit_log': log_list, 'count': len(log_list)}, 200
    except Exception as e:
        return {'error': _error_message(e), 'code': 'server_error'}, 500


@route('/api/health', method='GET')
def health_check(request):
    """Check API, main include migration, and last reload state."""
    state = config_manager._read_apply_state()
    main_valid, main_detail = config_manager._main_entrypoint_state()
    reload_warning = bool(state.get('reload_warning'))
    warning = reload_warning or not main_valid
    detail = state.get('reload_detail') if reload_warning else main_detail
    return {
        'status': 'degraded' if warning else 'healthy',
        'service': 'bird-manager',
        'reload_warning': warning,
        'reload_detail': detail,
    }, 200


# ==================== WSGI Entrypoint ====================

app = _create_wsgi_app()


def _listen_address():
    """Bind address for the HTTP server (env default keeps 0.0.0.0)."""
    return (os.getenv('APP_HOST') or '0.0.0.0').strip() or '0.0.0.0'


def _serve_with_timeout(host=None, port=5000, timeout_sec=10):
    """Serve the app for a limited time, then shut down gracefully."""
    def _shutdown(signum=None, frame=None):
        sys.exit(0)

    timer = threading.Timer(timeout_sec, _shutdown)
    timer.daemon = True
    timer.start()

    import waitress
    waitress.serve(app, host=host or _listen_address(), port=port)


if __name__ == '__main__':
    import argparse
    arg_parser = argparse.ArgumentParser(description='bird-manager API server')
    arg_parser.add_argument('--test-mode', action='store_true', dest='test_mode', default=False, help='Run for timeout seconds then exit')
    arg_parser.add_argument('--timeout', type=int, default=10, help='Seconds to run before exit (default: 10)')
    arg_parser.add_argument('--port', type=int, default=5000, help='Listen port')
    arg_parser.add_argument('--host', default=None, help='Bind address (default: APP_HOST or 0.0.0.0)')
    args = arg_parser.parse_args()

    host = args.host or _listen_address()
    _log_access_settings(_access_settings())
    print(f"[START] bird-manager API server launching on http://{host}:{args.port} ...", flush=True)
    try:
        if args.test_mode:
            _serve_with_timeout(host=host, port=args.port, timeout_sec=args.timeout)
            print("[OK] bird-manager test mode finished without startup errors.", flush=True)
        else:
            import waitress
            print(f"[OK] bird-manager API server started successfully on http://{host}:{args.port}", flush=True)
            waitress.serve(app, host=host, port=args.port)
    except Exception as e:
        print(f"[ERROR] Failed to start bird-manager API server: {e}", flush=True)
        sys.exit(1)
