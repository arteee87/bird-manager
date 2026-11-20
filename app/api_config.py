"""Configuration generation, diff, editor and apply API handlers.

Registered on import via ctx.route. Shared runtime state is read through
context at call time so rebinding (tests, restore) stays visible.
"""

import os

# Conditional imports — work both as package and as script entry point
try:
    from . import context as ctx
    from .backup import backup_info
    from .config_generator import _as_community_ids, is_bundle_filename
    from .syntax_validator import SyntaxValidator
except ImportError:
    import context as ctx
    from backup import backup_info
    from config_generator import _as_community_ids, is_bundle_filename
    from syntax_validator import SyntaxValidator


@ctx.route('/api/config/live', method='GET')
def get_config_live(request):
    """GET /api/config/live — return the live config file content."""
    try:
        filename = (request.args.get('filename') or ['networks.conf'])[0]
        if not ctx._is_editable_config(filename):
            return {'error': 'Only networks.conf is editable', 'code': 'editable_only_networks_conf'}, 400
        live_file = os.path.join(ctx._LIVE_PATH, filename)
        if not os.path.exists(live_file):
            return {'content': None, 'filename': filename}, 200
        # Explicit UTF-8, and a read failure reported as such: this endpoint
        # feeds the editor, so an undecodable file must not surface as a 500.
        with open(live_file, 'r', encoding='utf-8') as f:
            content = f.read()
        return {'content': content, 'filename': filename}, 200
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500

@ctx.route('/api/config/overview', method='GET')
def get_config_overview(request):
    """GET /api/config/overview — return the multi-file config panel model."""
    try:
        return ctx.config_manager.get_config_overview(), 200
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500

@ctx.route('/api/config/generate', method='POST')
def generate_config(request):
    """Generate BIRD configuration from database data."""
    try:
        session = ctx.get_session()
        config_path = ctx._regenerate_staged(session)
        if config_path is None:
            return {'error': 'Config generation failed', 'code': 'config_generation_failed'}, 500
        return {'status': 'success', 'config_path': config_path}, 200
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500


@ctx.route('/api/config/diff', method='GET')
def get_config_diff(request):
    """Get diff between live and staged configuration."""
    try:
        filename = request.args.get('filename', ['networks.conf'])[0] if 'filename' in request.args else 'networks.conf'
        if not ctx._is_editable_config(filename):
            return {'error': 'Only networks.conf is editable', 'code': 'editable_only_networks_conf'}, 400
        diff_output = ctx.diff_engine.get_diff(filename)
        return {'diff': diff_output, 'filename': filename}, 200
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500


@ctx.route('/api/config/file-rows', method='GET')
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
        return ctx.config_manager.get_file_rows_view(filename), 200
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500


@ctx.route('/api/config/file-diff', method='GET')
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
        return ctx.config_manager.get_file_diff_view(filename), 200
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500


@ctx.route('/api/config/diff/all', method='GET')
def get_all_config_diffs(request):
    """Get diffs for all files between live and staged."""
    try:
        all_diffs = ctx.diff_engine.get_all_diffs()
        count_diff = ctx.diff_engine.get_file_count_diff()
        return {'diffs': all_diffs, 'summary': count_diff}, 200
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500


@ctx.route('/api/config/syntax-check', method='GET')
def check_syntax(request):
    """Check syntax validation of staged configuration."""
    try:
        filename = request.args.get('filename', ['networks.conf'])[0] if 'filename' in request.args else 'networks.conf'
        if not ctx._is_editable_config(filename):
            return {'error': 'Only networks.conf is editable', 'code': 'editable_only_networks_conf'}, 400
        if ctx.config_manager.split_mode:
            result = ctx.config_manager.validate_staged_bundle()
        else:
            validator = SyntaxValidator()
            validator.staged_path = ctx.diff_engine.staged_path
            result = validator.validate_staged_config(filename)
        return result, 200
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500


@ctx.route('/api/config/staged', method='GET')
def get_staged_config(request):
    """Get the current staged configuration content."""
    try:
        filename = request.args.get('filename', ['networks.conf'])[0] if 'filename' in request.args else 'networks.conf'
        if not ctx._is_editable_config(filename):
            return {'error': 'Only networks.conf is editable', 'code': 'editable_only_networks_conf'}, 400
        content = ctx.diff_engine.get_staged_content(filename)
        return {'content': content, 'filename': filename}, 200
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500


@ctx.route('/api/config/apply', method='POST')
def apply_config(request):
    """POST /api/config/apply — apply config to live and reload BIRD.

    If `content` is present in the body it is merged with database-derived
    blocks (variant B, see ConfigManager.apply_config_from_content);
    otherwise the existing staged file is applied.
    """
    try:
        data = request.body_data if request.body_data else {}
        filename = data.get('filename', 'networks.conf')
        if not ctx._is_editable_config(filename):
            return {'error': 'Only networks.conf is editable', 'code': 'editable_only_networks_conf'}, 400
        user_id = data.get('user_id', 'system')
        user_content = data.get('content', '')
        has_content = 'content' in data and data.get('content') is not None

        if has_content:
            session = ctx.get_session()
            networks = [
                {
                    'prefix': n.prefix, 'description': n.description, 'status': n.status,
                    'enabled': n.enabled,
                    'community_ids': _as_community_ids(n.community_ids),
                }
                for n in ctx.crud.get_networks(session)
            ]
            communities = [
                {'id': c.id, 'name': c.name, 'values': c.values, 'description': c.description, 'enabled': c.enabled}
                for c in ctx.crud.get_communities(session)
            ]
            bgp_peers = ctx._db_bgp_peers(session)
            result = ctx.config_manager.apply_config_from_content(
                filename, user_content, user_id, networks, communities, bgp_peers=bgp_peers
            )
        else:
            result = ctx.config_manager.apply_config(filename=filename, user_id=user_id)
        status_code = 200 if result['status'] in ('success', 'success_with_warning') else (404 if result.get('status') == 'file_not_found' else 400)
        return result, status_code
    except Exception as e:
        return {'error': ctx._error_message(e), 'status': 'error', 'code': 'server_error'}, 500


@ctx.route('/api/config/rollback', method='POST')
def rollback_config(request):
    """Rollback the last applied configuration."""
    try:
        filename = request.args.get('filename', [None])[0] if 'filename' in request.args else None
        if not filename:
            return {'error': 'Missing required parameter: filename', 'code': 'missing_filename'}, 400
        if not ctx._is_editable_config(filename):
            return {'error': 'Only networks.conf is editable', 'code': 'editable_only_networks_conf'}, 400

        user_id = request.args.get('user_id', ['system'])[0] if 'user_id' in request.args else 'system'
        result = ctx.config_manager.rollback_last_apply(filename=filename, user_id=user_id)
        if result['status'] in ('success', 'success_with_warning'):
            status_code = 200
        elif result.get('status') == 'backup_not_found':
            status_code = 404
        else:
            status_code = 400
        return result, status_code
    except Exception as e:
        return {'error': ctx._error_message(e), 'status': 'error', 'code': 'server_error'}, 500


@ctx.route('/api/config/status', method='GET')
def get_config_status(request):
    """Get the last apply metadata for a configuration file."""
    try:
        filename = request.args.get('filename', ['networks.conf'])[0] if 'filename' in request.args else 'networks.conf'
        status_data = ctx.config_manager.get_apply_status(filename)
        # Add 'status' key to match API documentation
        status_data['status'] = 'success' if any(status_data.values()) else 'no_history'
        status_data['backup'] = backup_info(ctx._DB_PATH)
        return status_data, 200
    except Exception as e:
        return {'error': ctx._error_message(e), 'code': 'server_error'}, 500
