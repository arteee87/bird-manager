"""WSGI entry point, access control and facade for bird-manager.

Runtime state and helpers live in context.py; the API handlers live in the
api_* modules (registered on import below). This module keeps the WSGI
plumbing, the access gate, the handlers pinned here by source-level tests
(dashboard, static, config preview, peers list) and the historical
app.routes namespace via re-exports.
"""

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
import ipaddress
import html
from urllib.parse import parse_qs, unquote


# Conditional imports — work both as package and as script entry point
try:
    from .models import AuditLog, BgpPeer, ensure_schema
    from .database import close_all_sessions
    from .config_generator import (
        _as_community_ids,
        detect_router_id,
        _peer_local_asn,
        _resolve_router_id,
    )
    from .syntax_validator import SyntaxValidator
    from .log import log
    _package_mode = True
except ImportError:
    _parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _parent_dir not in sys.path:
        sys.path.insert(0, _parent_dir)
    from models import AuditLog, BgpPeer, ensure_schema
    from database import close_all_sessions
    from config_generator import (
        _as_community_ids,
        detect_router_id,
        _peer_local_asn,
        _resolve_router_id,
    )
    from syntax_validator import SyntaxValidator
    from log import log
    _package_mode = False

# Shared runtime state and helpers live in context.py. The re-exports keep
# the historical app.routes namespace intact for tests and tooling; the two
# bind wrappers additionally sync the facade copies after a rebind.
try:
    from . import context
    from .context import (
        crud, parser, diff_engine, config_manager,
        engine, _DB_PATH, get_session,
        _BASE_DIR, _LIVE_PATH, _STAGED_PATH,
        ROUTES, route,
        _seed_staged, _bird_settings, _is_editable_config, _db_bgp_peers,
        _bump_data_version, _cached_peer_asn_prefix_counts,
        _peer_asn_prefix_counts, _regenerate_staged, _bgp_config_paths,
        _asn_already_in_use, _neighbor_already_in_use,
        _audit_action, _error_message, _RawResponse,
        GENERIC_ERROR_TEXT, _bool_env,
    )
except ImportError:
    import context
    from context import (
        crud, parser, diff_engine, config_manager,
        engine, _DB_PATH, get_session,
        _BASE_DIR, _LIVE_PATH, _STAGED_PATH,
        ROUTES, route,
        _seed_staged, _bird_settings, _is_editable_config, _db_bgp_peers,
        _bump_data_version, _cached_peer_asn_prefix_counts,
        _peer_asn_prefix_counts, _regenerate_staged, _bgp_config_paths,
        _asn_already_in_use, _neighbor_already_in_use,
        _audit_action, _error_message, _RawResponse,
        GENERIC_ERROR_TEXT, _bool_env,
    )


def _bind_paths_from_env():
    """Rebind context to the current env and keep the facade copies in sync.

    See context._bind_paths_from_env for why the binding is explicit. Tests
    read routes._LIVE_PATH / routes._STAGED_PATH after calling this.
    """
    context._bind_paths_from_env()
    global _LIVE_PATH, _STAGED_PATH
    _LIVE_PATH, _STAGED_PATH = context._LIVE_PATH, context._STAGED_PATH


def _bind_database_from_env():
    """Rebind the DB engine/path in context and sync the facade copies.

    See context._bind_database_from_env for why an unbound process would
    restore a database it is not using.
    """
    context._bind_database_from_env()
    global engine, _DB_PATH
    engine, _DB_PATH = context.engine, context._DB_PATH


# Writes to the rebound names on this facade must reach context too: tests
# monkeypatch routes._LIVE_PATH / routes._STAGED_PATH directly, and the
# helpers that read them live in context.
class _RoutesModule(sys.modules[__name__].__class__):
    _FORWARDED = frozenset({'_LIVE_PATH', '_STAGED_PATH', 'engine', '_DB_PATH'})

    def __setattr__(self, name, value):
        if name in self._FORWARDED:
            setattr(context, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _RoutesModule


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
            log.warning(f"[WARN] ignoring invalid address entry in allowlist: {token}")
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


def _log_access_settings(settings):
    """Report the effective access policy once at startup."""
    sources = ', '.join(str(network) for network in settings['sources']) or '(any)'
    proxies = ', '.join(str(network) for network in settings['proxies']) or '(none)'
    origins = ', '.join(sorted(settings['origins'])) or '(origin check disabled)'
    log.info(f"[ACCESS] trusted sources: {sources}")
    log.info(f"[ACCESS] trusted proxies: {proxies}")
    log.info(f"[ACCESS] trusted origins: {origins}")
    log.info(f"[ACCESS] max request body: {settings['max_body']} bytes")
    if not settings['sources']:
        log.warning("[WARN] BIRD_TRUSTED_SOURCES is empty — every client may change the BIRD configuration")
    if not settings['origins']:
        log.warning("[WARN] BIRD_TRUSTED_ORIGINS is empty — cross-origin browser requests are not checked")


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
        log.info(prefix)
        if note:
            log.warning(f'[DENY] {note}')
        log.info(f'[RESP] {preview}')
    else:
        log.info(prefix + f' | content-type: {content_type}')
        if note:
            log.warning(f'[DENY] {note}')
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
            log.info(f"[API] Unhandled exception for {request.method} {request.path}: {e}")
            message = _error_message(e)
            return _send(_status_phrase(500), 'application/json',
                         json.dumps({'error': message, 'status': 'error'}).encode('utf-8'))
        finally:
            # Close DB sessions opened during this request — prevents QueuePool
            # exhaustion under concurrent dashboard polling (R4.1.3 auto refresh).
            close_all_sessions()

    return wsgi_app


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


# --- Existing routes below ---


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


# Handler modules register their routes on import (historical section
# order; patterns are anchored, so matching does not depend on it).
try:
    from . import api_networks  # noqa: F401 — registers routes on import
    from . import api_communities  # noqa: F401 — registers routes on import
    from . import api_peers  # noqa: F401 — registers routes on import
    from . import api_config  # noqa: F401 — registers routes on import
    from . import api_backup  # noqa: F401 — registers routes on import
    from . import api_system  # noqa: F401 — registers routes on import
except ImportError:
    import api_networks  # noqa: F401 — registers routes on import
    import api_communities  # noqa: F401 — registers routes on import
    import api_peers  # noqa: F401 — registers routes on import
    import api_config  # noqa: F401 — registers routes on import
    import api_backup  # noqa: F401 — registers routes on import
    import api_system  # noqa: F401 — registers routes on import


# ==================== WSGI Entrypoint ====================

app = _create_wsgi_app()


def _listen_address():
    """Bind address for the HTTP server (env default keeps 0.0.0.0)."""
    return (os.getenv('APP_HOST') or '0.0.0.0').strip() or '0.0.0.0'


def _serve_with_timeout(host=None, port=5000, timeout_sec=10):
    """Serve the app for a limited time, then shut down gracefully.

    ``waitress.serve`` blocks forever, and ``sys.exit`` raised in a timer
    thread only terminates that thread, so the previous implementation never
    actually stopped the server. We use the lower-level server object and
    ``close()`` it from the timer instead.
    """
    import waitress

    server = waitress.create_server(app, host=host or _listen_address(), port=port)

    def _shutdown():
        server.close()

    timer = threading.Timer(timeout_sec, _shutdown)
    timer.daemon = True
    timer.start()
    try:
        server.run()
    finally:
        timer.cancel()


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
    log.info(f"[START] bird-manager API server launching on http://{host}:{args.port} ...")
    try:
        if args.test_mode:
            _serve_with_timeout(host=host, port=args.port, timeout_sec=args.timeout)
            log.info("[OK] bird-manager test mode finished without startup errors.")
        else:
            import waitress
            log.info(f"[OK] bird-manager API server started successfully on http://{host}:{args.port}")
            waitress.serve(app, host=host, port=args.port)
    except Exception as e:
        log.error(f"[ERROR] Failed to start bird-manager API server: {e}")
        sys.exit(1)
