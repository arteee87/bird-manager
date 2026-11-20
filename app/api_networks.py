"""Network CRUD and bulk-import API handlers.

Registered on import via ctx.route. Shared runtime state is read through
context at call time so rebinding (tests, restore) stays visible.
"""

# Conditional imports — work both as package and as script entry point
try:
    from . import context as ctx
except ImportError:
    import context as ctx


@ctx.route('/api/networks', method='GET')
def get_networks(request):
    """Get all networks from the database."""
    try:
        session = ctx.get_session()
        networks = ctx.crud.get_networks(session)
        network_list = [{
            'id': net.id, 'prefix': net.prefix, 'description': net.description,
            'status': net.status, 'community_ids': net.community_ids,
            'enabled': net.enabled,
        } for net in networks]
        return {'networks': network_list, 'count': len(network_list)}, 200
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500


@ctx.route('/api/networks/count', method='GET')
def get_network_count(request):
    """Get the number of network records without returning their data.

    Used by the dashboard to show collapsed lists: only the count is
    loaded on startup, list contents are fetched lazily on expand.
    """
    try:
        session = ctx.get_session()
        count = ctx.crud.count_networks(session)
        return {'count': count}, 200
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500


def _prefix_review(prefix):
    """Build the 409 body for a prefix that is not already canonical.

    Returns ``None`` when the prefix is fine and the caller may proceed. The
    same shape is used by both the bulk parser and the single-record routes so
    the dialog is driven by one contract.
    """
    verdict = ctx.parser.classify_token(prefix)
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


@ctx.route('/api/networks', method='POST')
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

        session = ctx.get_session()
        network = ctx.crud.create_network(
            session=session,
            prefix=data['prefix'],
            description=data.get('description', ''),
            status=data.get('status', 'pending'),
            community_ids=data.get('community_ids'),
            enabled=data.get('enabled', True),
        )
        ctx._regenerate_staged(session)
        return {'network': {
            'id': network.id, 'prefix': network.prefix, 'description': network.description,
            'status': network.status, 'community_ids': network.community_ids,
            'enabled': network.enabled,
        }}, 201
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500


@ctx.route('/api/networks/<int:network_id>', method='PUT')
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

        session = ctx.get_session()
        existing = ctx.crud.get_network_by_id(session, network_id)
        if not existing:
            return {'error': 'Network not found', 'code': 'network_not_found'}, 404

        network = ctx.crud.update_network(session=session, network_id=network_id, **data)
        if network:
            ctx._regenerate_staged(session)
            return {'network': {
                'id': network.id, 'prefix': network.prefix, 'description': network.description,
                'status': network.status, 'community_ids': network.community_ids,
                'enabled': network.enabled,
            }}, 200
        return {'error': 'Network not found', 'code': 'network_not_found'}, 404
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500


@ctx.route('/api/networks/<int:network_id>', method='DELETE')
def delete_network(request):
    """Delete a network record."""
    try:
        network_id = int(request.path.split('/')[-1])

        session = ctx.get_session()
        existing = ctx.crud.get_network_by_id(session, network_id)
        if not existing:
            return {'error': 'Network not found', 'code': 'network_not_found'}, 404

        network = ctx.crud.delete_network(session=session, network_id=network_id)
        if network:
            ctx._regenerate_staged(session)
            return {
                'message': 'Network deleted',
                'network': {'id': network.id, 'prefix': network.prefix},
            }, 200
        return {'error': 'Network not found', 'code': 'network_not_found'}, 404
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500


@ctx.route('/api/networks/bulk', method='POST')
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

        result = ctx.parser.parse_and_map(raw_data, input_format=input_format)
        return {
            'parsed_networks': result['networks'],
            'parsed_communities': result['communities'],
            'invalid': result['invalid'],
        }, 200
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500


@ctx.route('/api/networks/bulk/commit', method='POST')
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

        session = ctx.get_session()
        result = ctx.crud.bulk_commit_networks(session=session, networks=networks, user_id=user_id)
        ctx._regenerate_staged(session)
        return {
            'created': result['created'],
            'skipped_duplicates': result['skipped_duplicates'],
            'skipped_invalid': result.get('skipped_invalid', 0),
            'audit_id': result['audit_id'],
        }, 201
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500
