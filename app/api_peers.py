"""BGP peer create/delete/count API handlers.

Registered on import via ctx.route. Shared runtime state is read through
context at call time so rebinding (tests, restore) stays visible.
"""

from sqlalchemy.exc import IntegrityError

# Conditional imports — work both as package and as script entry point
try:
    from . import context as ctx
    from .config_generator import _peer_local_asn
    from .validators import Validator
except ImportError:
    import context as ctx
    from config_generator import _peer_local_asn
    from validators import Validator


@ctx.route('/api/bgp_peers/count', method='GET')
def get_bgp_peer_count(request):
    """Number of BGP peer records, without their contents.

    Mirrors /api/networks/count: the peer panel starts collapsed and shows only
    a counter, so the list itself is fetched lazily on first expand. The path
    cannot collide with /api/bgp_peers/<int:peer_id> — route patterns are
    anchored, and "count" is not a digit.
    """
    try:
        session = ctx.get_session()
        return {'count': ctx.crud.count_bgp_peers(session)}, 200
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500


@ctx.route('/api/bgp_peers', method='POST')
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

        session = ctx.get_session()
        if ctx._asn_already_in_use(session, neighbor_as):
            return {
                'error': 400,
                'message': f'ASN {neighbor_as} уже используется другим пиром (peer id / конфиг)',
                'code': 'asn_already_in_use',
            }, 400
        if ctx._neighbor_already_in_use(session, neighbor):
            return {
                'error': 400,
                'message': f'Neighbor {neighbor} уже используется другим пиром (peer id / конфиг)',
                'code': 'neighbor_already_in_use',
            }, 400

        peer = ctx.crud.create_bgp_peer(
            session=session,
            name=name,
            local_as=local_as,
            neighbor=neighbor,
            neighbor_as=neighbor_as,
            status=data.get('status', 'active'),
            user_id=data.get('user_id', 'system'),
            export_all=bool(data.get('export_all', False)),
        )
        ctx._regenerate_staged(session)
        # Same fields the list endpoint returns, so the form can warn about a
        # missing ASN straight from the POST response instead of waiting for a
        # refresh.
        asn = _peer_local_asn({'local_as': peer.local_as})
        asn_prefixes = ctx._cached_peer_asn_prefix_counts(session)
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
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500


@ctx.route('/api/bgp_peers/<int:peer_id>', method='DELETE')
def delete_bgp_peer(request):
    """Delete a BGP peer record."""
    try:
        peer_id = int(request.path.split('/')[-1])

        session = ctx.get_session()
        existing = ctx.crud.get_bgp_peer_by_id(session, peer_id)
        if not existing:
            return {'error': 'BGP peer not found', 'code': 'bgp_peer_not_found'}, 404

        peer = ctx.crud.delete_bgp_peer(
            session=session, peer_id=peer_id, user_id='system'
        )
        if peer:
            ctx._regenerate_staged(session)
            return {
                'message': 'BGP peer deleted',
                'bgp_peer': {'id': peer.id, 'name': peer.name},
            }, 200
        return {'error': 'BGP peer not found', 'code': 'bgp_peer_not_found'}, 404
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500
