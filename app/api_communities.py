"""BGP community CRUD API handlers.

Registered on import via ctx.route. Shared runtime state is read through
context at call time so rebinding (tests, restore) stays visible.
"""

# Conditional imports — work both as package and as script entry point
try:
    from . import context as ctx
    from .models import Community
    from .validators import (
    derive_community_values,
    validate_community_name,
    validate_community_ref,
)
except ImportError:
    import context as ctx
    from models import Community
    from validators import (
    derive_community_values,
    validate_community_name,
    validate_community_ref,
)


@ctx.route('/api/communities', method='GET')
def get_communities(request):
    """Get all communities from the database."""
    try:
        session = ctx.get_session()
        communities = ctx.crud.get_communities(session)
        community_list = [{
            'id': comm.id, 'name': comm.name, 'values': comm.values, 'description': comm.description,
            'enabled': comm.enabled,
        } for comm in communities]
        return {'communities': community_list, 'count': len(community_list)}, 200
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500


@ctx.route('/api/communities/count', method='GET')
def get_community_count(request):
    """Get the number of community records without returning their data.

    Used by the dashboard to show collapsed lists: only the count is
    loaded on startup, list contents are fetched lazily on expand.
    """
    try:
        session = ctx.get_session()
        count = ctx.crud.count_communities(session)
        return {'count': count}, 200
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500


@ctx.route('/api/communities', method='POST')
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

        session = ctx.get_session()
        if name == '65534':
            community = ctx.crud.ensure_default_community(session)
            ctx._regenerate_staged(session)
            return {'community': {
                'id': community.id, 'name': community.name, 'values': community.values, 'description': community.description,
                'enabled': community.enabled,
            }}, 200
        existing = session.query(Community).filter(Community.name == name).first()
        if existing is not None:
            ctx._regenerate_staged(session)
            return {'community': {
                'id': existing.id, 'name': existing.name, 'values': existing.values, 'description': existing.description,
                'enabled': existing.enabled,
            }}, 200
        community = ctx.crud.create_community(
            session=session,
            name=name,
            values=values,
            description=data.get('description', ''),
            enabled=data.get('enabled', True),
        )
        ctx._regenerate_staged(session)
        return {'community': {
            'id': community.id, 'name': community.name, 'values': community.values, 'description': community.description,
            'enabled': community.enabled,
        }}, 201
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500


@ctx.route('/api/communities/<int:community_id>', method='PUT')
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

        session = ctx.get_session()
        existing = ctx.crud.get_community_by_id(session, community_id)
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

        community = ctx.crud.update_community(
            session=session, community_id=community_id, **updates
        )
        if community:
            ctx._regenerate_staged(session)
            return {'community': {
                'id': community.id, 'name': community.name, 'values': community.values, 'description': community.description,
                'enabled': community.enabled,
            }}, 200
        return {'error': 'Community not found', 'code': 'community_not_found'}, 404
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500


@ctx.route('/api/communities/<int:community_id>', method='DELETE')
def delete_community(request):
    """Delete a community record."""
    try:
        community_id = int(request.path.split('/')[-1])

        session = ctx.get_session()
        existing = ctx.crud.get_community_by_id(session, community_id)
        if not existing:
            return {'error': 'Community not found', 'code': 'community_not_found'}, 404

        community = ctx.crud.delete_community(
            session=session, community_id=community_id
        )
        if community:
            ctx._regenerate_staged(session)
            return {
                'message': 'Community deleted',
                'community': {'id': community.id, 'name': community.name},
            }, 200
        return {'error': 'Community not found', 'code': 'community_not_found'}, 404
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500
