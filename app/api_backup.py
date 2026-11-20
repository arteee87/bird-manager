"""Full-backup snapshot/restore/delete API handlers.

Registered on import via ctx.route. Shared runtime state is read through
context at call time so rebinding (tests, restore) stays visible.
"""

# Conditional imports — work both as package and as script entry point
try:
    from . import context as ctx
    from .backup import backup_info, create_backup, delete_backup, restore_backup
    from .models import ensure_schema
except ImportError:
    import context as ctx
    from backup import backup_info, create_backup, delete_backup, restore_backup
    from models import ensure_schema


@ctx.route('/api/backup', method='POST')
def backup_snapshot(request):
    """POST /api/backup — create a rotating full snapshot (@@ db+staged+live)."""
    try:
        data = request.body_data if request.body_data else {}
        info = create_backup(ctx._LIVE_PATH, ctx._STAGED_PATH, ctx._DB_PATH)
        ctx._audit_action('BACKUP', data.get('user_id'))
        return {'status': 'success', 'backup': info}, 200
    except Exception as e:
        return {'error': ctx._error_message(e), 'status': 'error', 'code': 'server_error'}, 500


def _restore_backup_request(request, backup_id=None):
    try:
        data = request.body_data if request.body_data else {}
        ctx.engine.dispose()
        try:
            result = restore_backup(ctx._LIVE_PATH, ctx._STAGED_PATH, ctx._DB_PATH, backup_id=backup_id)
        except ValueError as exc:
            return {'error': str(exc), 'status': 'error', 'code': 'invalid_backup_id'}, 400
        if result['status'] != 'success':
            return result, 400
        ensure_schema(ctx.engine)
        restored_session = ctx.get_session()
        ctx._regenerate_staged(restored_session)
        reload_success, reload_warning, reload_detail = ctx.config_manager._attempt_bird_reload()
        ctx.config_manager._write_apply_state(reload_warning, reload_detail)
        ctx._audit_action('RESTORE_BACKUP', data.get('user_id'))
        return {
            'status': 'success',
            'message': result['message'],
            'code': result.get('code'),
            'reload_warning': reload_warning,
            'reload_detail': reload_detail,
            'backup': backup_info(ctx._DB_PATH),
        }, 200
    except Exception as e:
        return {'error': ctx._error_message(e), 'status': 'error', 'code': 'server_error'}, 500


@ctx.route('/api/backup/restore', method='POST')
def backup_restore(request):
    """POST /api/backup/restore — restore the newest snapshot."""
    return _restore_backup_request(request)


@ctx.route('/api/backup/<string:backup_id>/restore', method='POST')
def backup_restore_selected(request):
    """POST /api/backup/<backup_id>/restore — restore one selected snapshot."""
    backup_id = request.path.split('/')[-2]
    return _restore_backup_request(request, backup_id=backup_id)


@ctx.route('/api/backup/<string:backup_id>', method='DELETE')
def backup_delete(request):
    """DELETE /api/backup/<backup_id> — delete one snapshot."""
    try:
        backup_id = request.path.rsplit('/', 1)[-1]
        result = delete_backup(backup_id, ctx._DB_PATH)
    except ValueError as exc:
        return {'error': str(exc), 'status': 'error', 'code': 'invalid_backup_id'}, 400
    if result['status'] != 'success':
        return result, 404
    data = request.body_data if request.body_data else {}
    ctx._audit_action('DELETE_BACKUP', data.get('user_id'))
    return result, 200
