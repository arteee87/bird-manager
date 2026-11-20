"""BIRD runtime, audit, health and static-page API handlers.

Registered on import via ctx.route. Shared runtime state is read through
context at call time so rebinding (tests, restore) stays visible.
"""

import os

# Conditional imports — work both as package and as script entry point
try:
    from . import context as ctx
except ImportError:
    import context as ctx


@ctx.route('/api/bird/reload', method='POST')
def bird_reload(request):
    """POST /api/bird/reload — reconfigure BIRD through birdc."""
    try:
        data = request.body_data if request.body_data else {}
        success, warning, detail = ctx.config_manager._attempt_bird_reload()
        ctx.config_manager._write_apply_state(warning, detail)
        if success:
            ctx._audit_action('BIRD_RECONFIGURE', data.get('user_id'))
        return {
            'status': 'ok' if success else 'error',
            'bird_running': os.path.exists(ctx.config_manager._bird_socket_path()),
            'error': detail,
        }, 200
    except Exception as e:
        return {'status': 'error', 'error': str(e), 'code': 'server_error'}, 200


@ctx.route('/api/bird/protocols', method='GET')
def get_bird_protocols(request):
    """GET /api/bird/protocols — live snapshot from `birdc show protocols all`.

    Read-only view of the running daemon. Every BGP session found in BIRD is
    reported, including protocols added to bird.conf by hand; peers configured
    in the panel but missing from the BIRD output are appended with
    ``in_bird: false`` so the discrepancy stays visible.
    """
    try:
        snapshot = ctx.config_manager._bird_protocols()
        managed_names = set()
        try:
            session = ctx.get_session()
            managed_names = {p.name for p in ctx.crud.get_bgp_peers(session)}
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
        return {'status': 'error', 'error': ctx._error_message(e), 'code': 'server_error'}, 200


@ctx.route('/api/audit', method='GET')
def get_audit_log(request):
    """Get recent audit log entries."""
    try:
        limit_str = request.args.get('limit', ['50'])[0] if 'limit' in request.args else '50'
        session = ctx.get_session()
        audit_entries = ctx.crud.get_audit_log(session, int(limit_str))

        log_list = [{
            'id': entry.id, 'action': entry.action, 'timestamp': str(entry.timestamp),
            'user_id': entry.user_id, 'diff_summary': entry.diff_summary,
        } for entry in audit_entries]

        return {'audit_log': log_list, 'count': len(log_list)}, 200
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500


@ctx.route('/api/health', method='GET')
def health_check(request):
    """Check API, main include migration, and last reload state."""
    state = ctx.config_manager._read_apply_state()
    main_valid, main_detail = ctx.config_manager._main_entrypoint_state()
    reload_warning = bool(state.get('reload_warning'))
    warning = reload_warning or not main_valid
    detail = state.get('reload_detail') if reload_warning else main_detail
    return {
        'status': 'degraded' if warning else 'healthy',
        'service': 'bird-manager',
        'reload_warning': warning,
        'reload_detail': detail,
    }, 200

