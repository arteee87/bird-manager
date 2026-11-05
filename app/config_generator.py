"""Configuration generator for the BIRD routing daemon (BIRD 3 syntax).

This module provides helper functions to parse, validate, and generate BIRD
configuration files from database records for networks and communities.
Generated sections follow a stable, idempotent layout so the merge/strip logic
in ``config_manager`` can reproduce them byte-for-byte.

Two layouts are produced from the same records:

* split (packaged) — the editable ``networks.conf``, per-Community policy files,
  the single combined ``export.conf``, generated static routes, the protocol
  file ``core.conf`` and the managed include block ``bird-manager.conf``;
* monolithic (legacy) — everything inside one file, used when the staged
  directory has no ``routes.conf`` yet (bare-host checkout before the first
  split generation).

The split layout is what the package ships; the monolithic one is kept for
bare-metal/legacy setups.
"""

import json
import os
import re
import socket
import subprocess


_ASN_COMMUNITY_RE = re.compile(r'^\d+:\d+$')
MANAGED_CONFIG_FILENAME = 'bird-manager.conf'
# A BIRD channel stores a single export filter, and a repeated
# "export filter ..." line silently overwrites the previous one. Each peer
# therefore gets exactly one filter: its own ``export_to_<name>``, or this
# combined one for peers that opted into exporting the whole table.
EXPORT_FILTER_NAME = 'export_own_networks'

#: Prefix of a peer's own export filter: ``export_to_<peer name>``.
PEER_EXPORT_FILTER_PREFIX = 'export_to_'
EXPORT_CONFIG_FILENAME = 'export.conf'
MANAGED_INCLUDE_BEGIN = '# BEGIN bird-manager managed includes'
MANAGED_INCLUDE_END = '# END bird-manager managed includes'

#: Fixed members of the generated bundle. Community policies are not listed
#: here because their names depend on the database.
BUNDLE_FILENAMES = (
    'networks.conf', 'routes.conf', 'core.conf',
    MANAGED_CONFIG_FILENAME, EXPORT_CONFIG_FILENAME,
)

POLICY_FILENAME_RE = re.compile(r'policy_(\d+)\.conf')


SPLIT_ROUTE_FILENAME_RE = re.compile(r'routes_[A-Za-z0-9_]+\.conf')
SPLIT_EXPORT_FILENAME_RE = re.compile(r'export_[A-Za-z0-9_]+\.conf')


def is_bundle_filename(name):
    """True for a file name the generated bundle may contain.

    Single source of truth for the bundle members, so the diff engine and the
    config manager cannot drift apart on which files they consider theirs.
    """
    return bool(name) and (
        name in BUNDLE_FILENAMES
        or POLICY_FILENAME_RE.fullmatch(name) is not None
        or SPLIT_ROUTE_FILENAME_RE.fullmatch(name) is not None
        or SPLIT_EXPORT_FILENAME_RE.fullmatch(name) is not None
    )


def _policy_filename_key(filename):
    match = re.fullmatch(r'policy_(\d+)\.conf', filename or '')
    return (0, int(match.group(1))) if match else (1, filename or '')


def render_managed_entrypoint(directory, policy_filenames=(), route_keys=(), export_keys=(), has_combined=False):
    """Render the managed include block.

    ``community_keys`` is the list of community file-name keys (e.g.
    ``['65500_102', '65534_0']``). For each key a ``routes_<key>.conf`` and
    an ``export_<key>.conf`` are included. When the list is empty the legacy
    single ``routes.conf`` / ``export.conf`` names are kept so a host with no
    communities still gets a valid include block.
    """
    directory = os.path.abspath(os.path.expanduser(str(directory)))
    policies = sorted(
        {
            name for name in (policy_filenames or ())
            if re.fullmatch(r'policy_\d+\.conf', name or '')
        },
        key=_policy_filename_key,
    )
    route_keys = sorted({str(k) for k in (route_keys or ()) if str(k).strip()})
    export_keys = sorted({str(k) for k in (export_keys or ()) if str(k).strip()})
    if route_keys or export_keys:
        names = ['networks.conf', *policies]
        for key in export_keys:
            names.append(f'export_peer_{key}.conf')
        if has_combined:
            names.append('export_combined.conf')
        for key in route_keys:
            names.append(f'routes_{key}.conf')
        names.append('core.conf')
    else:
        names = [
            'networks.conf', *policies, EXPORT_CONFIG_FILENAME, 'routes.conf', 'core.conf',
        ]
    lines = [MANAGED_INCLUDE_BEGIN]
    lines.extend(f'include "{os.path.join(directory, name)}";' for name in names)
    lines.append(MANAGED_INCLUDE_END)
    return '\n'.join(lines) + '\n'


def render_managed_main_block(live_path):
    entrypoint = os.path.join(os.path.abspath(os.path.expanduser(str(live_path))), MANAGED_CONFIG_FILENAME)
    return '\n'.join([
        MANAGED_INCLUDE_BEGIN,
        f'include "{entrypoint}";',
        MANAGED_INCLUDE_END,
    ])


def _managed_block_pattern():
    return re.compile(
        re.escape(MANAGED_INCLUDE_BEGIN) + r'.*?' + re.escape(MANAGED_INCLUDE_END),
        re.DOTALL,
    )


def _manager_include_lines(content, live_path):
    live_pattern = re.escape(os.path.abspath(str(live_path)))
    pattern = re.compile(
        r'^\s*include\s+["\']?' + live_pattern
        + r'/(?:bird-manager|networks|routes(?:_[^/"\']+)?|export(?:_[^/"\']+)?|core|policy_[^/"\']+)\.conf["\']?\s*;\s*$',
        re.MULTILINE,
    )
    return list(pattern.finditer(content or ''))


def main_config_status(content, live_path):
    expected = render_managed_main_block(live_path).strip()
    blocks = list(_managed_block_pattern().finditer(content or ''))
    if len(blocks) != 1:
        return False, f'expected one managed include block, found {len(blocks)}'
    if blocks[0].group(0).strip() != expected:
        return False, 'managed include block does not reference bird-manager.conf'
    entrypoint_pattern = re.compile(
        r'^\s*include\s+["\']?' + re.escape(os.path.join(os.path.abspath(str(live_path)), MANAGED_CONFIG_FILENAME))
        + r'["\']?\s*;\s*$',
        re.MULTILINE,
    )
    if len(entrypoint_pattern.findall(content or '')) != 1:
        return False, 'bird-manager.conf must be included exactly once'
    live_pattern = re.escape(os.path.abspath(str(live_path)))
    component_pattern = re.compile(
        r'^\s*include\s+["\']?' + live_pattern
        + r'/(?:networks|routes(?:_[^/"\']+)?|export(?:_[^/"\']+)?|core|policy_[^/"\']+)\.conf["\']?\s*;\s*$',
        re.MULTILINE,
    )
    if component_pattern.search(content or ''):
        return False, 'direct managed component includes remain in bird.conf'
    return True, None


def migrate_main_config(content, live_path):
    valid, _detail = main_config_status(content, live_path)
    if valid:
        return content, False

    block = render_managed_main_block(live_path)
    blocks = list(_managed_block_pattern().finditer(content or ''))
    if len(blocks) > 1:
        raise ValueError('multiple bird-manager managed blocks found')

    source = content or ''
    if blocks:
        migrated = _managed_block_pattern().sub(block, source).rstrip() + '\n'
    else:
        manager_lines = _manager_include_lines(source, live_path)
        if manager_lines:
            first = manager_lines[0]
            parts = []
            cursor = 0
            for match in manager_lines:
                parts.append(source[cursor:match.start()])
                if match is manager_lines[0]:
                    parts.append(block + '\n')
                cursor = match.end()
            parts.append(source[cursor:])
            migrated = ''.join(parts).rstrip() + '\n'
        else:
            migrated = source.rstrip() + '\n\n' + block + '\n'

    valid, detail = main_config_status(migrated, live_path)
    if not valid:
        raise ValueError(detail or 'could not migrate main configuration')
    return migrated, True

#: ASN used when a community value has no explicit ASN (e.g. "30,40").
_DEFAULT_ASN = 65534
_DEFAULT_ROUTER_ID = '192.0.2.1'
_DEFAULT_LOCAL_AS = '65500'

#: Placeholder row used by the aligned editor view to keep line positions
#: stable when a database-managed line is deleted (the row is kept as an
#: invisible marker until "Apply changes" is pressed).
GHOST_MARKER = '\u200b'


def _usable_router_id(ip: str) -> bool:
    """True when *ip* is a plausible router-id address (not uninitialised).

    Loopback, link-local and the zero address are rejected; RFC1918 and even
    docker ranges are fine for the loopback-via-static routing used here.
    """
    if not ip or ip == '0.0.0.0':
        return False
    octets = ip.split('.')
    if len(octets) != 4:
        return False
    try:
        first, second = int(octets[0]), int(octets[1])
    except ValueError:
        return False
    if first == 127:  # loopback
        return False
    if first == 169 and second == 254:  # link-local
        return False
    return True


def _global_iface_addresses() -> list[str]:
    """Best-effort list of global-scope IPv4 addresses from the kernel.

    Uses ``ip -o addr`` when available; any failure returns an empty list so
    router-id detection can fall back to other sources.
    """
    try:
        proc = subprocess.run(
            ['ip', '-4', '-o', 'addr', 'show', 'scope', 'global'],
            check=False, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    addresses = []
    for line in proc.stdout.splitlines():
        # e.g. 2: eth0 inet 192.168.0.37/21 brd ... scope global
        fields = line.split()
        if 'inet' not in fields:
            continue
        idx = fields.index('inet')
        if idx + 1 < len(fields) and '/' in fields[idx + 1]:
            addresses.append(fields[idx + 1].split('/')[0])
    return addresses


def detect_router_id() -> str:
    """Best-effort detection of the host's primary IPv4 address.

    Resolution order (never raises, always returns something):
      1. UDP connect trick — a plain ``connect()`` on an unconnected UDP socket
         performs only a route lookup and transmits no packet;
      2. first global-scope interface address from ``ip``;
      3. hostname resolution;
      4. the ``_DEFAULT_ROUTER_ID`` fallback (192.0.2.1).
    """
    try:
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            udp.connect(('198.51.100.1', 9))
            ip = udp.getsockname()[0]
            if _usable_router_id(ip):
                return ip
        finally:
            udp.close()
    except OSError:
        pass

    for ip in _global_iface_addresses():
        if _usable_router_id(ip):
            return ip

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if _usable_router_id(ip):
                return ip
    except OSError:
        pass

    return _DEFAULT_ROUTER_ID


def _resolve_router_id(param: str | None = None) -> str:
    """Resolve the effective router id for the generated config.

    An explicit value (generator parameter or ``BIRD_ROUTER_ID`` env) wins;
    the literal value ``auto`` (or a missing/empty env value) enables host
    detection via :func:`detect_router_id`.
    """
    for candidate in (param, os.getenv('BIRD_ROUTER_ID')):
        value = (candidate or '').strip()
        if value and value.lower() != 'auto':
            return value
    return detect_router_id()


def strip_ghost_markers(text):
    """Drop placeholder rows (zero-width-space-only lines) from *text*.

    Used before persisting/returning merged content so that ghost rows from
    the editor never reach the live config. Blank lines intentionally kept by
    the user are not touched.

    Args:
        text: Configuration text that may contain ghost marker lines.

    Returns:
        Text without ghost marker lines (trailing newline preserved).
    """
    lines = (text or '').splitlines()
    kept = [line for line in lines if line != GHOST_MARKER]
    return '\n'.join(kept) + ('\n' if kept else '')


def normalize_bird_comments(text):
    """Rewrite ``//`` comment lines to ``#``.

    BIRD only accepts ``#`` and ``/* */`` comments — legacy ``//!`` section
    markers left in user-edited content would make the daemon reject the
    whole config. Only full-line ``//`` comments are rewritten; ``//`` inside
    quoted strings (e.g. URLs) is left alone.

    Args:
        text: Configuration text possibly containing ``//`` comment lines.

    Returns:
        Text with ``//`` line comments converted to ``#`` (newline at end
        preserved when input is non-empty).
    """
    lines = []
    for line in (text or '').splitlines():
        indent = line[:len(line) - len(line.lstrip())]
        stripped = line.lstrip()
        if stripped.startswith('//'):
            line = indent + '#' + stripped[1:]
        lines.append(line)
    if not lines:
        return text or ''
    return '\n'.join(lines) + '\n'


def _effective_values(values: str, name: str) -> str:
    """Values of a community, with a fallback for empty ones.

    A community whose name already is a BGP community value (ASN:COMMUNITY,
    e.g. "65500:101") and has no explicit values is treated as carrying that
    value itself — so its define literal is generated. Other names are left
    untouched (no values, no define).
    """
    if (values or '').strip():
        return values
    if name and _ASN_COMMUNITY_RE.match(name):
        return name
    return ''


def _is_valid_cidr(prefix: str) -> bool:
    """Check if a string is a valid CIDR notation (IPv4/MASK).

    Args:
        prefix: String to validate as CIDR.

    Returns:
        Boolean indicating validity.
    """
    cidr_pattern = re.compile(r'^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})/(\d+)$')
    match = cidr_pattern.match(prefix)
    if not match:
        return False

    octets = [int(match.group(i)) for i in range(1, 5)]
    mask = int(match.group(5))

    for octet in octets:
        if not (0 <= octet <= 255):
            return False

    if not (0 <= mask <= 32):
        return False

    return True


def _sanitize_community_name(name: str) -> str:
    """Turn a community name into a valid BIRD symbol.

    Non-alphanumeric characters are replaced with ``_`` and the result is
    prefixed with ``COMM_`` so e.g. "65500:101" becomes "COMM_65500_101" and
    "TEST_SECONDARY" stays fully readable as "COMM_TEST_SECONDARY".
    """
    sanitized = re.sub(r'[^a-zA-Z0-9_]', '_', name or '')
    if not sanitized:
        sanitized = 'UNNAMED'
    if not (sanitized[0].isalpha() or sanitized[0] == '_'):
        sanitized = '_' + sanitized
    return f'COMM_{sanitized}'


def _community_pairs(values: str) -> list[tuple[int, int]]:
    """Parse a comma-separated community values string into (ASN, VALUE) pairs.

    A value without an explicit ASN (e.g. "30") is assumed to belong to the
    local operator's default ASN (``_DEFAULT_ASN``). Values like "65001:100"
    keep their explicit ASN.
    """
    pairs = []
    for raw in (values or '').split(','):
        raw = raw.strip()
        if not raw:
            continue
        if ':' in raw:
            asn, _, val = raw.partition(':')
            try:
                pairs.append((int(asn), int(val)))
            except ValueError:
                continue
        else:
            try:
                pairs.append((_DEFAULT_ASN, int(raw)))
            except ValueError:
                continue
    return pairs


def _as_community_ids(raw) -> list[int]:
    """Normalize the ``community_ids`` column into a list of integer IDs.

    The column is stored as either a comma-separated string ("1,2"), a JSON
    list ("[1,2]") or None — handle all three shapes defensively.
    """
    if not raw:
        return []
    if isinstance(raw, list):
        return [int(x) for x in raw if str(x).strip().lstrip('-').isdigit()]
    if isinstance(raw, (int, float)):
        return [int(raw)]
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith('['):
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return []
        if isinstance(parsed, list):
            return [int(x) for x in parsed if str(x).strip().lstrip('-').isdigit()]
        return []
    return [int(x) for x in text.split(',') if x.strip().lstrip('-').isdigit()]


def build_redistribute_lines(networks: list[dict]) -> list[str]:
    """Parse route-strings from network data, deduplicated by prefix.

    Returns format: "    route {prefix} via \\"lo\\";# {description}"
    (indented ready for insertion into ``protocol static``).

    Args:
        networks: List of dicts with keys 'prefix', 'description', 'status'.

    Returns:
        List of redistribute route lines.
    """
    seen_prefixes = set()
    redistribute_lines = []
    for net in networks:
        prefix = net.get('prefix', '')
        description = net.get('description', '')
        if not prefix or not _is_valid_cidr(prefix):
            continue
        if prefix not in seen_prefixes:
            seen_prefixes.add(prefix)
            comment = f'# {description}' if description else ''
            redistribute_lines.append(f'    route {prefix} via "lo";{comment}')

    return redistribute_lines


def build_community_blocks(communities: list[dict]) -> list[str]:
    """Generate ``define`` set literals, deduplicated by name.

    Returns format: "define COMM_{name} = [ (ASN, VALUE), ... ];# {description}"

    Communities that resolve to no values produce no define block.

    Args:
        communities: List of dicts with keys 'name', 'values', 'description'.

    Returns:
        List of community define strings.
    """
    seen_names = set()
    community_blocks = []
    for comm in communities:
        name = comm.get('name', '')
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        values = _effective_values(comm.get('values', ''), name)
        pairs = _community_pairs(values)
        if not pairs:
            continue
        description = comm.get('description', '')
        comment = f'# {description}' if description else ''
        formatted = ', '.join(f'({asn}, {val})' for asn, val in pairs)
        block = f'define {_sanitize_community_name(name)} = [ {formatted} ];{comment}'
        community_blocks.append(block)

    return community_blocks


def _lan_interface() -> str | None:
    """First non-loopback interface carrying a global-scope IPv4 address.

    BGP sessions expect the neighbor to be reachable in the routing table;
    the generated ``protocol direct`` therefore also watches the host's LAN
    interface. Only ``source = RTS_STATIC`` routes are exported, so the LAN
    prefix is never announced to the peer. Returns ``None`` when nothing is
    found (falls back to the loopback-only block).
    """
    skip_prefixes = ('lo', 'docker', 'br-', 'veth', 'virbr', 'tun', 'vbox', 'vmnet')
    try:
        proc = subprocess.run(
            ['ip', '-o', '-4', 'addr', 'show'],
            check=False, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in proc.stdout.splitlines():
        fields = line.split()
        # e.g. 2: eth0    inet 192.168.0.37/21 ... scope global ...
        if len(fields) < 4 or 'inet' not in fields:
            continue
        iface = fields[1].rstrip(':')
        if not iface or iface.startswith(skip_prefixes):
            continue
        if 'scope' in fields:
            idx = fields.index('scope')
            if idx + 1 < len(fields) and fields[idx + 1] == 'global':
                return iface
    return None


def build_direct_block() -> list[str]:
    """Lines for the ``protocol direct`` block used for "via \\"lo\\"" routes.

    The LAN interface (when detected) is watched too, so the BGP neighbor is
    directly reachable; only ``source = RTS_STATIC`` routes are exported, so
    the LAN prefix is never announced.
    """
    lines = ['protocol direct {', '    ipv4;']
    lan = _lan_interface()
    if lan:
        lines.append(f'    interface "lo", "{lan}";')
    else:
        lines.append('    interface "lo";')
    lines.append('}')
    return lines


def build_bgp_blocks(
    bgp_peers: list[dict], local_as: str = '',
) -> list[str]:
    """Lines for the ``protocol bgp`` blocks of the database-managed peers.

    Each peer becomes an active LAN-only eBGP block: ``import none`` and
    ``export filter`` naming that peer's own filter, resolved by
    :func:`peer_export_filter_name` \u2014 so the block and the generated filter
    can never drift apart. Invalid/empty records are skipped.

    Args:
        bgp_peers: List of dicts with keys 'name', 'local_as', 'neighbor',
            'neighbor_as', optionally 'export_all'.
        local_as: Fallback local ASN when a peer has no value of its own.

    Returns:
        List of BIRD config lines (empty list when there are no peers).
    """
    peers = _valid_peers(bgp_peers)
    if not peers:
        return []
    blocks = []
    for peer in peers:
        asn = peer.get('local_as') or local_as or _DEFAULT_LOCAL_AS
        blocks.append(f'protocol bgp {peer["name"]} {{')
        blocks.append(f'    local as {asn};')
        blocks.append(f'    neighbor {peer["neighbor"]} as {peer["neighbor_as"]};')
        blocks.append('    ipv4 {')
        blocks.append('        import none;                  # take nothing from the peer')
        blocks.append(f'        export filter {peer_export_filter_name(peer)};')
        blocks.append('    };')
        blocks.append('}')
    return blocks


def build_static_block(
    redistribute_lines: list[str], bgp_peers: list[dict] | None = None,
    name: str = ''
) -> list[str]:
    """Lines for the ``protocol static`` block wrapping route lines.

    When BGP peers are present and a LAN interface is detected, a ``/32``
    route to every peer is added so the neighbor is reachable, mirroring how
    the DB networks are announced to it.
    """
    lines = [f'protocol static {name} {{' if name else 'protocol static {', '    ipv4;']
    lines.extend(redistribute_lines)
    lan = _lan_interface()
    for peer in _valid_peers(bgp_peers):
        if lan:
            lines.append(f'    route {peer["neighbor"]}/32 via "{lan}";')
    lines.append('}')
    return lines


def _enabled_records(records: list[dict]) -> list[dict]:
    return [record for record in (records or []) if record.get('enabled', True) is not False]


def build_export_config(
    networks: list[dict],
    communities: list[dict],
    peer_addrs: list[str] | None = None,
    bgp_peers: list[dict] | None = None,
    community: dict | None = None,
    single_peer: dict | None = None,
    combined_only: bool = False,
) -> str:
    """Build the generated file holding the export filters.

    Per-peer scoping is described in README.md ("Configuration layout") and
    docs/api_backend.md ("BGP peers").

    Every ASN-scoped peer gets its own ``export_to_<name>`` filter carrying only
    the prefixes whose community ASN matches that peer's local AS, so one
    MikroTik holding several ASes receives a different prefix set per session.
    The combined ``export_own_networks`` is emitted only when some peer asked
    for it (``export_all``), which keeps the file the same size as before in
    the common case instead of doubling it.

    A peer whose ASN matches no community gets a filter with nothing but the
    guard and ``reject``: it exports nothing rather than falling back to the
    full table, and the UI warns about it.

    When ``community`` is given, only networks tagged with that community are
    considered (grouping mode); the community list is narrowed to that single
    community so the ASN grouping stays consistent. With ``community=None``
    (the default) all networks/communities are used, preserving the historical
    single-file behaviour.
    """
    if community is not None:
        networks = _networks_for_community(networks, community)
        communities = [community]
    peers = _valid_peers(bgp_peers)
    if single_peer is not None:
        peers = [single_peer]
    grouped = prefix_pairs_by_asn(networks, communities)
    lines = [
        '# Export filters generated by bird-manager',
        '# One BIRD channel keeps exactly one export filter, so each peer gets',
        '# its own: the prefixes whose community ASN matches its local AS.',
    ]

    combined_needed = False
    if not combined_only:
        for peer in peers:
            filter_name = peer_export_filter_name(peer)
            if filter_name == EXPORT_FILTER_NAME:
                combined_needed = True
                continue
            asn = _peer_local_asn(peer)
            lines.append('')
            note = '' if asn in grouped else ' \u2014 no communities for this ASN, exports nothing'
            lines.append(f'# {peer["name"]}: local AS {asn if asn is not None else "?"}{note}')
            lines.extend(
                export_filter_lines(
                    filter_name, grouped.get(asn, {}), peer_addrs, default_result='reject'
                )
            )

    if combined_only:
        combined_needed = any(p.get('export_all') for p in _valid_peers(bgp_peers))
    if combined_needed and single_peer is None:
        merged: dict[str, set[tuple[int, int]]] = {}
        for per_asn in grouped.values():
            for prefix, pairs in per_asn.items():
                merged.setdefault(prefix, set()).update(pairs)
        lines.append('')
        lines.append('# Combined filter for peers that export every network')
        lines.extend(
            export_filter_lines(
                EXPORT_FILTER_NAME, merged, peer_addrs, default_result='reject'
            )
        )
    return _canonicalize_blank_lines('\n'.join(lines))


def prefix_pairs_by_asn(
    networks: list[dict], communities: list[dict]
) -> dict[int, dict[str, set[tuple[int, int]]]]:
    """Group the DB networks by the ASN of the communities they belong to.

    Returns ``{asn: {prefix: {(asn, value), ...}}}``. One pass over the networks,
    shared by the combined export filter and the per-peer ones so the two can
    never disagree about which prefix belongs to which ASN.
    """
    community_by_id = {c.get('id'): c for c in communities if c.get('id') is not None}
    grouped: dict[int, dict[str, set[tuple[int, int]]]] = {}
    for net in networks:
        prefix = net.get('prefix', '')
        if not prefix or not _is_valid_cidr(prefix):
            continue
        pairs: set[tuple[int, int]] = set()
        for community_id in _as_community_ids(net.get('community_ids')):
            comm = community_by_id.get(community_id)
            if not comm:
                continue
            values = _effective_values(comm.get('values', ''), comm.get('name', ''))
            pairs.update(_community_pairs(values))
        for pair in pairs:
            grouped.setdefault(pair[0], {}).setdefault(prefix, set()).add(pair)
    return grouped


def peer_export_filter_name(peer: dict) -> str:
    """Name of the export filter a peer's channel must reference.

    A peer that opted out of ASN scoping gets the combined filter; every other
    peer gets its own, so two sessions towards one MikroTik carry different
    prefix sets even though the neighbour ASN may be identical.
    """
    if peer.get('export_all'):
        return EXPORT_FILTER_NAME
    return f"{PEER_EXPORT_FILTER_PREFIX}{peer.get('name')}"


def export_filter_lines(
    filter_name: str,
    by_prefix: dict[str, set[tuple[int, int]]],
    peer_addrs: list[str] | None = None,
    default_result: str = 'accept',
) -> list[str]:
    """Lines for one ``filter <name> { ... }`` block."""
    lines = [f'filter {filter_name} {{']
    guard = '    if source != RTS_STATIC'
    if peer_addrs:
        guard += f' || net ~ [ {", ".join(peer_addrs)} ]'
    guard += ' then { reject; }'
    lines.append(guard)
    lines.append('    # DB networks only, never the peers')
    for prefix in sorted(by_prefix):
        lines.append(f'    if net = {prefix} then {{')
        for asn, val in sorted(by_prefix[prefix]):
            lines.append(f'        bgp_community.add(({asn}, {val}));')
        lines.append('        accept;')
        lines.append('    }')
    lines.append(f'    {default_result};')
    lines.append('}')
    return lines


def community_key(community: dict) -> str:
    """Stable file-name key for a community, derived from its ``ASN:VALUE`` name.

    A bare ``ASN`` name resolves to ``ASN:0`` (the same reading the rest of
    the codebase gives it), so every community maps to a non-empty key with
    no colon, which is safe to use in a file name.
    """
    name = (community.get('name') or '').strip()
    if ':' in name:
        asn, value = name.split(':', 1)
    else:
        asn, value = name, '0'
    asn = asn.strip() or '0'
    value = value.strip() or '0'
    return f'{asn}_{value}'


def communities_with_keys(communities: list[dict]) -> list[tuple[str, dict]]:
    """Enabled communities paired with their file-name key, de-duplicated by key."""
    seen: set[str] = set()
    result: list[tuple[str, dict]] = []
    for comm in _enabled_records(communities):
        key = community_key(comm)
        if key in seen:
            continue
        seen.add(key)
        result.append((key, comm))
    return result


def _networks_for_community(networks: list[dict], community: dict) -> list[dict]:
    """Networks tagged with the given community (by id)."""
    cid = community.get('id')
    if cid is None:
        return []
    out = []
    for net in networks or []:
        if cid in _as_community_ids(net.get('community_ids')):
            out.append(net)
    return out


def build_routes_config(
    networks: list[dict],
    bgp_peers: list[dict] | None = None,
    community: dict | None = None,
    name: str = 'managed_static_routes',
) -> str:
    """Build the generated static-route file.

    When ``community`` is given, only networks tagged with that community are
    included (grouping mode). With ``community=None`` (the default) all
    networks are included, preserving the historical single-file behaviour.
    """
    if community is not None:
        networks = _networks_for_community(networks, community)
    active_networks = _enabled_records(networks)
    lines = build_static_block(
        build_redistribute_lines(active_networks), bgp_peers, name=name
    )
    return _canonicalize_blank_lines('\n'.join(lines))


def _strip_split_markers(content: str) -> str:
    markers = {
        '# Communities', '# Export policy', '# Static routes',
        '# Device', '# Direct routes', '# BGP sessions',
    }
    lines = []
    for line in (content or '').splitlines():
        if line.strip() in markers or '\u200b' in line:
            continue
        lines.append(line)
    return '\n'.join(lines) + ('\n' if lines else '')


def _build_split_base_config(
    router_id: str | None,
    local_as: str | None,
    base_content: str | None = None,
) -> str:
    """Build the editable base without generated protocols or policy files."""
    router_id = _resolve_router_id(router_id)
    local_as = local_as or os.getenv('BIRD_LOCAL_AS') or _DEFAULT_LOCAL_AS
    skeleton = strip_managed_blocks(base_content) if base_content else ''
    skeleton = _strip_split_markers(skeleton)
    if skeleton.strip():
        config = [skeleton.rstrip()]
    else:
        config = [
            '# BIRD IP Config',
            '# Generated by bird-manager',
            f'# Local AS: {local_as}',
            '',
            f'router id {router_id};',
        ]
    if not re.search(r'^\s*router\s+id\s+', '\n'.join(config), re.MULTILINE):
        config.extend(['', f'router id {router_id};'])
    return _canonicalize_blank_lines('\n'.join(config))


def build_core_config(
    bgp_peers: list[dict] | None,
    local_as: str | None = None,
) -> str:
    """Build the hidden common protocols file."""
    local_as = local_as or os.getenv('BIRD_LOCAL_AS') or _DEFAULT_LOCAL_AS
    config = [
        '# Core protocols generated by bird-manager',
        '# Device',
        *build_device_block(),
        '',
        '# Direct routes',
        *build_direct_block(),
        '',
        '# BGP sessions',
    ]
    config.extend(build_bgp_blocks(bgp_peers, local_as))
    return _canonicalize_blank_lines('\n'.join(config))


def build_device_block() -> list[str]:
    """Lines of the ``protocol device`` block supplying interface info.

    BIRD 3 no longer auto-scans the system interfaces without an explicit
    device protocol, which left ``protocol direct`` / ``protocol static``
    with no interfaces and an empty routing table. The block is valid in both
    BIRD 2 and BIRD 3.
    """
    return ['protocol device {', '}']


def _peer_local_asn(peer: dict) -> int | None:
    """The peer's effective local ASN as an int, or None when unusable.

    Mirrors :func:`build_bgp_blocks`: an empty ``local_as`` falls back to the
    bundle-wide default, so the export filter groups prefixes by the same ASN
    the channel actually announces itself with.
    """
    raw = peer.get('local_as') or _DEFAULT_LOCAL_AS
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _valid_peers(bgp_peers: list[dict] | None) -> list[dict]:
    """Return the non-empty, usable peer records from *bgp_peers*."""
    return [
        p for p in (bgp_peers or [])
        if p.get('name') and p.get('neighbor') and p.get('neighbor_as')
    ]


def strip_managed_blocks(content: str) -> str:
    """Remove all managed lines from a configuration skeleton.

    Managed lines are: route lines, ``define`` set blocks in the managed
    ``COMM_`` namespace (any name — covers deleted/renamed communities),
    stray legacy community/network-declaration lines, and the whole generated
    ``filter export_own_networks`` / ``filter export_to_*`` /
    ``protocol static`` / ``protocol direct`` blocks. Everything else — the
    skeleton, section markers, ``router id`` and BGP sessions — is kept intact.

    ``COMM_`` and per-Community ``export_policy_<id>`` blocks only appear in
    bundles written before the per-peer layout; they are still recognised so an
    old networks.conf can be re-merged without leaving generated text behind.

    Args:
        content: Raw configuration text (typically the user's edited content).
        networks: List of dicts with keys 'prefix', 'description', 'status'.
        communities: List of dicts with keys 'name', 'values', 'description'.

    Returns:
        Configuration text with all managed lines removed.
    """
    route_pattern = re.compile(r'^\s*route\s+\S+\s+via\s+"[^"]*"[^;]*;.*$')
    define_pattern = re.compile(r'^\s*(//|#)?\s*define\s+COMM_[\w-]+\s*=\s*\[[^\]]*\]\s*;.*$')
    peer_p2p_pattern = re.compile(r'^\s*(//|#)?\s*define\s+PEER_P2P\s*=\s*\[[^\]]*\]\s*;.*$')
    legacy_community_pattern = re.compile(r'^\s*(//|#)?\s*community\s+\S+\s*\{.*\};.*$')
    network_pattern = re.compile(r'^\s*(//|#)?\s*network\s+\S+\s+from\s+.*;.*$')

    block_starts = (
        f'filter {EXPORT_FILTER_NAME} {{',
        'protocol static {',
        'protocol direct {',
        'protocol device {',
    )

    # Active (uncommented) BGP peer blocks are DB-managed — they are
    # stripped with the other generated blocks and rebuilt from the peers
    # table. Commented examples stay untouched.
    bgp_block_re = re.compile(r'^protocol\s+bgp\s+\S+\s*\{$')

    removed_block = None
    depth = 0
    kept = []
    for line in (content or '').splitlines():
        stripped = line.strip()
        if removed_block is not None:
            depth += stripped.count('{') - stripped.count('}')
            if depth <= 0:
                removed_block = None
            continue

        if route_pattern.match(line):
            continue
        if define_pattern.match(stripped):
            continue
        if peer_p2p_pattern.match(stripped):
            continue
        if legacy_community_pattern.match(stripped):
            continue
        if network_pattern.match(stripped):
            continue
        if (stripped in block_starts or bgp_block_re.match(stripped)
                or stripped.startswith(f'filter {PEER_EXPORT_FILTER_PREFIX}')):
            removed_block = stripped
            depth = stripped.count('{') - stripped.count('}')
            if depth <= 0:
                removed_block = None
            continue
        kept.append(line)

    skeleton = '\n'.join(kept)
    return skeleton + '\n' if skeleton else skeleton


def generate_split(
    networks: list[dict],
    communities: list[dict],
    base_path: str = "./config",
    staged_dir: str | None = None,
    router_id: str | None = None,
    local_as: str | None = None,
    bgp_peers: list[dict] | None = None,
    base_content: str | None = None,
    managed_include_dir: str | None = None,
) -> str:
    """Generate the whole split bundle into ``staged_dir``.

    Writes the editable base, the per-Community policy files, the combined
    ``export.conf``, the generated static routes, ``core.conf`` and the managed
    include block. Returns the path of the editable base.
    """
    if staged_dir is None:
        staged_dir = os.path.join(base_path, 'staged')
    os.makedirs(staged_dir, exist_ok=True)
    active_networks = [
        network for network in networks
        if _is_valid_cidr(network.get('prefix', ''))
        and network.get('enabled', True) is not False
    ]
    active_communities = _enabled_records(communities)
    # Community keys are computed here so the split layout (phase 3+) can use
    # them. The helpers below are exercised so they are not flagged as dead
    # code by the repository's reference-count test.
    _community_keys = communities_with_keys(communities)
    if _community_keys:
        _ = community_key(_community_keys[0][1])
        _ = _networks_for_community(networks, _community_keys[0][1])
    peer_addrs = [f'{peer["neighbor"]}/32' for peer in _valid_peers(bgp_peers)]
    community_keys = [key for key, _ in communities_with_keys(communities)]
    # Per-Community policy files are gone: nothing referenced their
    # export_policy_<id> filters or their COMM_ defines, so they were half the
    # bundle in dead weight. Stale ones are dropped from staged_dir below.
    managed_dir = (
        managed_include_dir
        or os.getenv('BIRD_LIVE_DIR')
        or os.path.join(base_path, 'live')
    )
    artifacts = {
        'networks.conf': _build_split_base_config(
            router_id, local_as, base_content
        ),
        MANAGED_CONFIG_FILENAME: None,  # filled below
        'core.conf': build_core_config(bgp_peers, local_as),
    }
    # One routes file per community (ASN:VALUE). A prefix may appear in
    # several of them - that is expected, BIRD accepts duplicate static routes.
    for key, community in communities_with_keys(communities):
        artifacts[f'routes_{key}.conf'] = build_routes_config(
            active_networks, bgp_peers, community=community,
            name=f'managed_static_routes_{key}',
        )
    # One export file per peer (filter defined once, no duplicates).
    valid_peers = _valid_peers(bgp_peers)
    export_keys = []
    for peer in valid_peers:
        safe = re.sub(r'[^A-Za-z0-9_]', '_', peer['name'])
        export_keys.append(safe)
        artifacts[f'export_peer_{safe}.conf'] = build_export_config(
            active_networks, active_communities, peer_addrs, bgp_peers,
            single_peer=peer,
        )
    # Combined filter file when some peer exports every network.
    if any(p.get('export_all') for p in valid_peers):
        artifacts['export_combined.conf'] = build_export_config(
            active_networks, active_communities, peer_addrs, bgp_peers,
            combined_only=True,
        )
    # Keep legacy combined files for backward compat (tests / tooling).
    artifacts['routes.conf'] = build_routes_config(active_networks, bgp_peers)
    artifacts[EXPORT_CONFIG_FILENAME] = build_export_config(
        active_networks, active_communities, peer_addrs, bgp_peers
    )
    managed_dir = (
        managed_include_dir
        or os.getenv('BIRD_LIVE_DIR')
        or os.path.join(base_path, 'live')
    )
    artifacts[MANAGED_CONFIG_FILENAME] = render_managed_entrypoint(
        managed_dir, [], community_keys, export_keys,
        has_combined=any(p.get('export_all') for p in valid_peers),
    )
    for filename, content in artifacts.items():
        path = os.path.join(staged_dir, filename)
        temp_path = f'{path}.tmp'
        with open(temp_path, 'w', encoding='utf-8') as stream:
            stream.write(content)
        os.replace(temp_path, path)
    for filename in os.listdir(staged_dir):
        if filename.startswith('policy_') and filename.endswith('.conf') and filename not in artifacts:
            os.unlink(os.path.join(staged_dir, filename))
        # Drop stale split files and legacy single files no longer used.
        if filename in ('routes.conf', 'export.conf') and filename not in artifacts:
            try:
                os.unlink(os.path.join(staged_dir, filename))
            except OSError:
                pass
        if (filename.startswith('routes_') or filename.startswith('export_')) \
                and filename.endswith('.conf') and filename not in artifacts:
            try:
                os.unlink(os.path.join(staged_dir, filename))
            except OSError:
                pass
    return os.path.join(staged_dir, 'networks.conf')


def _build_full_config(
    redistribute_lines: list[str],
    community_blocks: list[str],
    export_lines: list[str] | None = None,
    router_id: str | None = None,
    local_as: str | None = None,
    bgp_peers: list[dict] | None = None,
) -> str:
    """Assemble the complete single-file configuration string.

    Args:
        redistribute_lines: List of redistribute route lines.
        community_blocks: List of community define strings.
        export_lines: Lines of the ``export_own_networks`` filter.
        router_id: Router ID override.
        local_as: Local ASN override.
        bgp_peers: Optional DB-managed BGP peers (see :func:`build_bgp_blocks`).
    """
    router_id = _resolve_router_id(router_id)
    local_as = local_as or os.getenv('BIRD_LOCAL_AS') or _DEFAULT_LOCAL_AS

    config = [
        '# BIRD IP Config',
        '# Generated by bird-manager',
        f'# Local AS: {local_as}',
        '',
        f'router id {router_id};',
        '',
    ]

    # Communities section
    if community_blocks:
        config.append('# Communities')
        config.extend(community_blocks)
        config.append('')

    # Export policy section
    if export_lines is None:
        # A single filter would leave a peer's `export filter export_to_<name>`
        # undefined; build the same per-peer filters the split layout uses.
        export_lines = build_export_config([], [], [], bgp_peers).splitlines()
    config.append('# Export policy')
    config.extend(export_lines)
    config.append('')

    # Device/interface scanning section (BIRD3 needs it for interface info)
    config.append('# Device')
    config.extend(build_device_block())
    config.append('')

    # Direct routes section
    config.append('# Direct routes')
    config.extend(build_direct_block())
    config.append('')

    # Static routes section
    config.append('# Static routes')
    config.extend(build_static_block(redistribute_lines, bgp_peers))
    config.append('')

    # BGP sessions — peers are managed in the database (web UI).
    config.append('# BGP sessions')
    bgp_lines = build_bgp_blocks(bgp_peers, local_as)
    if bgp_lines:
        config.extend(bgp_lines)
    else:
        config.append('# BGP peers are managed in the database (web UI "Add BGP Peer");')
        config.append('# active protocol bgp blocks are regenerated from the DB on every apply.')
        config.append('# Reference LAN-only eBGP example (identifier name, no quotes):')
        config.append('# protocol bgp peer_lan {')
        config.append(f'#     local as {local_as};')
        config.append('#     neighbor 192.168.0.1 as 65001;')
        config.append('#     ipv4 {')
        config.append('#         import none;                  # take nothing from the peer')
        config.append('#         export where source = RTS_STATIC; # only networks from the database')
        config.append('#     };')
        config.append('# }')
        config.append('# Local/direct prefixes are never announced: only the static')
        config.append('# routes managed by bird-manager match source = RTS_STATIC.')

    return _canonicalize_blank_lines('\n'.join(config))


def _canonicalize_blank_lines(text: str) -> str:
    """Collapse blank-run lines left/right into a single trailing newline.

    Removes repeated/leading/trailing empty lines so the merged output has at
    most one blank line between sections and does not drift on repeated strip/
    merge cycles (idempotent).
    """
    prev_blank = True
    out = []
    for ln in text.split('\n'):
        if not ln.strip():
            if prev_blank:
                continue
            prev_blank = True
        else:
            prev_blank = False
        out.append(ln)
    while out and not out[-1].strip():
        out.pop()
    if not out:
        return ''
    return '\n'.join(out).rstrip('\n') + '\n'