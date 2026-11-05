"""Configuration manager — orchestrates apply, rollback, and status workflows."""

import os
import json
import shutil
import stat
import subprocess
import difflib
import re
import tempfile
from datetime import datetime

try:
    from .models import AuditLog, Network
    from .utils import local_now
    from .syntax_validator import SyntaxValidator
    from .diff_engine import DiffEngine, aligned_rows, diff_contents, measure_rows
    from .config_generator import (
        build_bgp_blocks,
        build_community_blocks,
        build_device_block,
        build_direct_block,
        build_export_config,
        build_redistribute_lines,
        build_static_block,
        strip_managed_blocks,
        strip_ghost_markers,
        normalize_bird_comments,
        _canonicalize_blank_lines,
        _valid_peers,
        GHOST_MARKER,
        _build_full_config,
        _build_split_base_config,
        MANAGED_CONFIG_FILENAME,
        EXPORT_CONFIG_FILENAME,
        BUNDLE_FILENAMES,
        POLICY_FILENAME_RE,
        is_bundle_filename,
        render_managed_entrypoint,
        main_config_status,
    )
except ImportError:
    from models import AuditLog, Network
    from utils import local_now
    from syntax_validator import SyntaxValidator
    from diff_engine import DiffEngine, aligned_rows, diff_contents, measure_rows
    from config_generator import (
        build_bgp_blocks,
        build_community_blocks,
        build_device_block,
        build_direct_block,
        build_export_config,
        build_redistribute_lines,
        build_static_block,
        strip_managed_blocks,
        strip_ghost_markers,
        normalize_bird_comments,
        _canonicalize_blank_lines,
        _valid_peers,
        GHOST_MARKER,
        _build_full_config,
        _build_split_base_config,
        MANAGED_CONFIG_FILENAME,
        EXPORT_CONFIG_FILENAME,
        BUNDLE_FILENAMES,
        POLICY_FILENAME_RE,
        is_bundle_filename,
        render_managed_entrypoint,
        main_config_status,
    )


#: Lines of diff text kept in the audit trail and in the apply response. The
#: exact per-file counts are always preserved; only the excerpt is capped.
DIFF_SUMMARY_MAX_LINES = 40
#: Caps for the read-only per-file diff view. A populated bundle produces a
#: megabyte of diff text for export.conf alone, which must not be pushed into
#: a single response; the change counts stay exact.
FILE_DIFF_MAX_LINES = 400
FILE_DIFF_MAX_BYTES = 64 * 1024
#: Rows returned by the side-by-side view. The panel does not even ask for a
#: file whose row count exceeds ROW_VIEW_MAX_ROWS — it keeps the header
#: counters instead, so the limit here only bounds an explicit request.
FILE_ROWS_MAX_LINES = 2000


try:
    from .backup import backup_paths
except ImportError:  # script mode: modules imported top-level
    from backup import backup_paths


class ConfigManager:
    """Orchestrate the entire configuration deployment workflow."""

    def __init__(self, live_path, staged_path, db_session_factory):
        """Initialize with paths and database session factory.

        Args:
            live_path: Path to the live configuration directory.
            staged_path: Path to the staged configuration directory.
            db_session_factory: Callable that returns a fresh DB session.
        """
        self.live_path = live_path
        self.staged_path = staged_path
        self.db_session_factory = db_session_factory
        self._resolve_derived_paths()

        # Ensure directories exist so apply/rollback never fail on missing dirs.
        os.makedirs(self.live_path, exist_ok=True)
        os.makedirs(self.staged_path, exist_ok=True)
        os.makedirs(self.publish_bak_dir, mode=0o750, exist_ok=True)
        self._migrate_legacy_live_backups()
        try:
            live_mode = stat.S_IMODE(os.stat(self.live_path).st_mode)
            if not live_mode & stat.S_ISGID:
                os.chmod(self.live_path, live_mode | stat.S_ISGID)
        except OSError:
            pass

    def _resolve_derived_paths(self):
        """Recompute the state that follows from live_path/staged_path/env.

        Split out of __init__ so :meth:`retarget` reuses the exact same rules
        instead of a second copy drifting away from them.
        """
        self.split_mode = (
            os.path.isfile(os.path.join(self.staged_path, 'routes.conf'))
            or any(
                n.startswith('routes_') and n.endswith('.conf')
                for n in os.listdir(self.staged_path)
            )
        ) if os.path.isdir(self.staged_path) else False
        # Same resolver the backup code uses, so the two can never disagree
        # about which manifest belongs to this installation.
        self.manifest_path = backup_paths.manifest_path(self.live_path)
        # Rollback snapshots live outside the BIRD include directory so that
        # /etc/bird/live only ever contains files that BIRD actually reads.
        self.publish_bak_dir = os.getenv(
            'BIRD_PUBLISH_BAK_DIR',
            '/var/lib/bird-manager/live-bak'
            if os.path.abspath(self.live_path).startswith('/etc/bird')
            else os.path.join(os.path.dirname(self.live_path), 'live-bak')
        )

    def retarget(self, live_path, staged_path):
        """Point this manager at different directories.

        Pure state rebind: it deliberately skips the filesystem side effects of
        __init__ (makedirs, SGID chmod, legacy backup migration). publish() and
        apply() create what they need themselves, and re-running the migration
        on every retarget would be surprising work for a path change.

        ``_apply_state_path()`` is derived from staged_path, so this also moves
        apply_state.json along with the rest.
        """
        self.live_path = live_path
        self.staged_path = staged_path
        self._resolve_derived_paths()

    @staticmethod
    def _main_config_path():
        return backup_paths.main_config_path()

    def _managed_entrypoint_path(self, directory=None):
        return os.path.join(directory or self.live_path, MANAGED_CONFIG_FILENAME)

    def _policy_filenames(self, directory=None):
        try:
            return sorted(
                (
                    name for name in os.listdir(directory or self.staged_path)
                    if re.fullmatch(r'policy_\d+\.conf', name)
                ),
                key=lambda name: int(name.split('_')[1].split('.')[0]),
            )
        except OSError:
            return []

    def _community_keys(self, directory=None):
        keys = set()
        try:
            for name in os.listdir(directory or self.staged_path):
                m = re.fullmatch(r'routes_(.+)\.conf', name)
                if m:
                    keys.add(m.group(1))
        except OSError:
            pass
        return sorted(keys)

    def _export_peer_keys(self, directory=None):
        keys = set()
        try:
            for name in os.listdir(directory or self.staged_path):
                m = re.fullmatch(r'export_peer_(.+)\.conf', name)
                if m:
                    keys.add(m.group(1))
        except OSError:
            pass
        return sorted(keys)

    def _has_combined_export(self, directory=None):
        try:
            return os.path.isfile(os.path.join(directory or self.staged_path, 'export_combined.conf'))
        except OSError:
            return False

    def _managed_filenames(self):
        names = set()
        names.add('networks.conf')
        names.add('core.conf')
        names.add(MANAGED_CONFIG_FILENAME)
        for directory in (self.staged_path,):
            try:
                for name in os.listdir(directory):
                    if is_bundle_filename(name) and name not in ('networks.conf', 'core.conf', MANAGED_CONFIG_FILENAME):
                        names.add(name)
            except OSError:
                pass
        names.update(self._policy_filenames())
        def order(name):
            if name == 'networks.conf':
                return (0, 0, name)
            if re.fullmatch(r'policy_\d+\.conf', name):
                return (1, int(name.split('_')[1].split('.')[0]), name)
            if name.startswith('export_') and name.endswith('.conf'):
                return (2, 0, name)
            if name.startswith('routes_') and name.endswith('.conf'):
                return (3, 0, name)
            if name == 'core.conf':
                return (4, 0, name)
            if name == MANAGED_CONFIG_FILENAME:
                return (5, 0, name)
            return (6, 0, name)
        return sorted(names, key=order)

    def _publish_bak_path(self, filename):
        return os.path.join(self.publish_bak_dir, f'{filename}.bak')

    def _migrate_legacy_live_backups(self):
        """Move snapshot artifacts left by older releases out of the include directory.

        Older releases kept ``<name>.conf.bak`` and temporary files next to the
        published configuration. The live directory must stay clean, so the
        snapshots are moved into the state directory on startup.
        """
        try:
            entries = os.listdir(self.live_path)
        except OSError:
            return
        for name in entries:
            live_file = os.path.join(self.live_path, name)
            if name.endswith('.tmp') or name.endswith('.rollback.tmp'):
                try:
                    os.unlink(live_file)
                except OSError:
                    pass
                continue
            if not name.endswith('.conf.bak'):
                continue
            target = self._publish_bak_path(name[:-len('.bak')])
            try:
                if not os.path.exists(target):
                    temporary = f'{target}.tmp'
                    shutil.copy2(live_file, temporary)
                    os.replace(temporary, target)
                os.unlink(live_file)
            except OSError:
                pass

    def _condense_bundle_diff(self, filenames):
        """Per-file change counts plus the head of the diff, for the audit trail.

        The counts are the part that matters: they are what warns before a
        bundle publish would drop thousands of prefixes. The text is context
        only, and the full diff of a populated bundle runs to megabytes, which
        would be written into SQLite on every apply. The overview endpoint
        serves the complete diff on demand.
        """
        engine = DiffEngine(self.live_path, self.staged_path)
        counts = []
        excerpt = []
        for name in filenames:
            file_diff = engine.get_diff(name)
            if not file_diff:
                continue
            size = measure_rows(
                self._read_text(os.path.join(self.live_path, name)),
                self._read_text(os.path.join(self.staged_path, name)),
            )
            adds, deletes = size['adds'], size['dels']
            counts.append(f'{name} +{adds} -{deletes}')
            if len(excerpt) < DIFF_SUMMARY_MAX_LINES:
                excerpt.extend(
                    file_diff.splitlines()[:DIFF_SUMMARY_MAX_LINES - len(excerpt)]
                )
        summary = '; '.join(counts)
        if excerpt:
            summary = (summary + '\n' if summary else '') + '\n'.join(excerpt)
        return summary

    def _overview_filenames(self):
        names = set(BUNDLE_FILENAMES)
        for directory in (self.live_path, self.staged_path):
            try:
                names.update(
                    name for name in os.listdir(directory)
                    if is_bundle_filename(name)
                )
            except OSError:
                pass
        return sorted(names)

    def _read_pair(self, name):
        """``(live_text, staged_text, readable)`` for one bundle file.

        A missing file is a normal state — its empty text is the truth, and the
        pairing legitimately reports every line as added. A file that exists but
        does not decode is not: comparing its unreadable content against nothing
        would invent a diff, so ``readable`` is False and the caller must not
        count anything.
        """
        live_file = os.path.join(self.live_path, name)
        staged_file = os.path.join(self.staged_path, name)
        live_text = self._read_text(live_file)
        staged_text = self._read_text(staged_file)
        readable = not (os.path.isfile(live_file) and live_text is None)
        readable = readable and not (os.path.isfile(staged_file) and staged_text is None)
        return live_text, staged_text, readable

    def _file_state(self, name):
        """Counters and text for one bundle file, live vs staged.

        The counters come from :func:`measure_rows`, which walks the same match
        the side-by-side view is built from, so the number in a heading and the
        rows below it can never disagree. The row model itself is not built
        here: the overview needs three integers per file, not tens of thousands
        of dictionaries nobody asked for yet.

        ``readable`` is False when the file exists but does not decode as
        UTF-8. The panel shows that as a warning — reporting ``+0 −0`` for a
        file it could not read would invite an Apply over a broken config.
        """
        current_path = os.path.join(self.live_path, name)
        current_text, candidate_text, readable = self._read_pair(name)
        if readable:
            diff_output = diff_contents(current_text or '', candidate_text or '')
            size = measure_rows(current_text, candidate_text)
        else:
            # Nothing to compare: an unreadable side must not be reported as an
            # empty one, or every line of it would look added.
            diff_output = ''
            size = {'adds': 0, 'dels': 0, 'total_rows': 0}
        exists = os.path.isfile(current_path)
        return {
            'content': current_text,
            'diff': diff_output,
            'adds': size['adds'],
            'dels': size['dels'],
            'rows': size['total_rows'],
            'exists': exists,
            'readable': readable,
            'candidate_exists': candidate_text is not None,
        }

    def get_file_rows_view(self, filename):
        """Aligned live→staged rows of one bundle file, text-capped.

        Returns ``None`` when *filename* is not a bundle member, so a path from
        a request can never reach the filesystem. The counts and ``total_rows``
        are exact; only the rows are capped, and ``truncated`` reports it.

        Both sides have the same number of rows by construction, so the panel
        can put them in two columns of equal height and align them by index.
        """
        if not is_bundle_filename(filename):
            return None
        live_text, staged_text, readable = self._read_pair(filename)
        if readable:
            view = aligned_rows(live_text, staged_text, FILE_ROWS_MAX_LINES)
        else:
            view = {
                'rows': [], 'adds': 0, 'dels': 0, 'total_rows': 0,
                'returned_rows': 0, 'truncated': False,
            }
        view.update({
            'filename': filename,
            'path': os.path.join(self.live_path, filename),
            'readable': readable,
        })
        return view

    def get_file_diff_view(self, filename):
        """Read-only live→staged diff for one generated bundle file.

        Returns ``None`` when *filename* is not a bundle member, so a path from
        a request can never reach the filesystem. The change counts and the
        total line count are exact; only the text is capped, and ``truncated``
        says whether it was.
        """
        if not is_bundle_filename(filename):
            return None
        state = self._file_state(filename)
        lines = (state['diff'] or '').splitlines()
        kept = lines[:FILE_DIFF_MAX_LINES]
        text = '\n'.join(kept)
        while text and len(text.encode('utf-8')) > FILE_DIFF_MAX_BYTES:
            kept.pop()
            text = '\n'.join(kept)
        return {
            'filename': filename,
            'adds': state['adds'],
            'dels': state['dels'],
            'total_lines': len(lines),
            'returned_lines': len(kept),
            'truncated': len(kept) < len(lines),
            'diff': text,
        }

    def _safe_file_state(self, name):
        """``_file_state`` that degrades instead of raising.

        One unreadable file must not cost the operator every other counter, so
        a failure is reported as ``readable: False`` with zero counters. The
        real exception still reaches the journal.
        """
        try:
            return self._file_state(name)
        except Exception as exc:
            print(f'[ERROR] {name}: не удалось прочитать состояние: {exc}', flush=True)
            return {
                'content': None, 'diff': '', 'adds': 0, 'dels': 0, 'rows': 0,
                'exists': os.path.isfile(os.path.join(self.live_path, name)),
                'readable': False, 'candidate_exists': False,
            }

    def get_config_overview(self):
        managed_state = self._safe_file_state(MANAGED_CONFIG_FILENAME)
        core_state = self._safe_file_state('core.conf')
        networks_state = self._safe_file_state('networks.conf')
        summary_names = [
            name for name in self._overview_filenames()
            if name not in {'networks.conf', 'core.conf', MANAGED_CONFIG_FILENAME}
        ]
        summary = []
        for name in summary_names:
            state = self._safe_file_state(name)
            summary.append({
                'filename': name,
                'path': os.path.join(self.live_path, name),
                'adds': state['adds'],
                'dels': state['dels'],
                'rows': state['rows'],
                'exists': state['exists'],
                'readable': state['readable'],
            })
        return {
            'networks': {
                'filename': 'networks.conf',
                'path': os.path.join(self.live_path, 'networks.conf'),
                'editable': True,
                'content': self._read_text(os.path.join(self.staged_path, 'networks.conf')),
                'live_content': self._read_text(os.path.join(self.live_path, 'networks.conf')),
                'diff': networks_state['diff'],
                'adds': networks_state['adds'],
                'dels': networks_state['dels'],
                'rows': networks_state['rows'],
                'readable': networks_state['readable'],
            },
            'managed_conf': {
                'filename': MANAGED_CONFIG_FILENAME,
                'path': self._managed_entrypoint_path(),
                'editable': False,
                'content': managed_state['content'],
                'diff': managed_state['diff'],
                'adds': managed_state['adds'],
                'dels': managed_state['dels'],
                'rows': managed_state['rows'],
                'readable': managed_state['readable'],
                'exists': managed_state['exists'],
                'candidate_exists': managed_state['candidate_exists'],
            },
            'core': {
                'filename': 'core.conf',
                'path': os.path.join(self.live_path, 'core.conf'),
                'editable': False,
                'content': core_state['content'],
                'diff': core_state['diff'],
                'adds': core_state['adds'],
                'dels': core_state['dels'],
                'rows': core_state['rows'],
                'readable': core_state['readable'],
            },
            'summary': summary,
        }

    def _validation_include_block(self, directory=None, networks_path=None):
        """Managed include block used to parse the bundle with ``bird -p``.

        ``networks_path`` replaces only the include of ``networks.conf`` so a
        caller can validate an edited version of that single file against the
        real generated bundle. Every other include (policies, the combined
        export filter, routes, core) comes from ``render_managed_entrypoint``,
        the same source the published entrypoint is built from. The preview
        used to assemble its own include list, which went stale when
        export.conf was added and produced a phantom parse error pointing at
        core.conf while the editor was showing networks.conf.
        """
        directory = directory or self.staged_path
        block = render_managed_entrypoint(directory, self._policy_filenames(directory), self._community_keys(directory), self._export_peer_keys(directory), self._has_combined_export(directory))
        if networks_path:
            lines = block.splitlines()
            for index, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith('include ') and stripped.endswith('/networks.conf";'):
                    lines[index] = f'include "{networks_path}";'
                    break
            block = "\n".join(lines)
        return block.rstrip()

    def _main_entrypoint_state(self):
        main_path = self._main_config_path()
        return main_config_status(self._read_text(main_path) or '', self.live_path)

    def _validate_staged_bundle(self):
        bird_bin = os.getenv('BIRD_BIN_PATH', 'bird')
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.conf', prefix='bird-manager-', delete=False,
            dir=self.staged_path, encoding='utf-8'
        ) as stream:
            stream.write(self._validation_include_block(self.staged_path) + '\n')
            validation_path = stream.name
        try:
            proc = subprocess.run(
                [bird_bin, '-p', '-c', validation_path],
                check=False, capture_output=True, text=True, timeout=15,
            )
            if proc.returncode != 0:
                return False, (proc.stderr or proc.stdout).strip()[:500]
            return True, None
        except FileNotFoundError:
            return True, None
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        finally:
            try:
                os.unlink(validation_path)
            except OSError:
                pass

    def validate_staged_bundle(self):
        """Validate the complete generated BIRD bundle, not only networks.conf."""
        expected_entrypoint = render_managed_entrypoint(
            self.live_path,
            self._policy_filenames(self.live_path),
            self._community_keys(self.staged_path),
            self._export_peer_keys(self.staged_path),
            self._has_combined_export(self.staged_path),
        )
        staged_entrypoint = self._read_text(
            self._managed_entrypoint_path(self.staged_path)
        )
        if staged_entrypoint != expected_entrypoint:
            detail = 'staged bird-manager.conf does not match the generated bundle'
            return {
                'valid': False,
                'errors': [detail],
                'error_lines': [],
                'error_files': [],
                'warnings': [],
                'summary': detail,
            }
        valid, detail = self._validate_staged_bundle()
        if valid:
            return {
                'valid': True,
                'errors': [],
                'error_lines': [],
                'error_files': [],
                'warnings': [],
                'summary': 'All checks passed',
            }
        detail = detail or 'Generated bundle validation failed'
        return {
            'valid': False,
            'errors': [f'BIRD parse error: {detail}'],
            'error_lines': [SyntaxValidator._bird_error_line(detail)],
            'error_files': [SyntaxValidator._bird_error_file(detail)],
            'warnings': [],
            'summary': f'BIRD parse error: {detail}',
        }

    def _publish_order(self, filenames):
        names = set(filenames)
        ordered = ['networks.conf']
        ordered.extend(name for name in self._policy_filenames() if name in names)
        # Split files: export_* before routes_* (matches generator order), then core.
        ordered.extend(sorted(n for n in names if n.startswith('export_') and n.endswith('.conf')))
        ordered.extend(sorted(n for n in names if n.startswith('routes_') and n.endswith('.conf')))
        # Legacy single files (backward compat with old bundles).
        for legacy in (EXPORT_CONFIG_FILENAME, 'routes.conf'):
            if legacy in names:
                ordered.append(legacy)
        ordered.extend(['core.conf', MANAGED_CONFIG_FILENAME])
        # De-duplicate while preserving order.
        seen = set()
        result = []
        for n in ordered:
            if n in names and n not in seen:
                seen.add(n)
                result.append(n)
        return result

    def _restore_publish_snapshot(self, names, existed_before):
        errors = []
        for name in names:
            live_file = os.path.join(self.live_path, name)
            backup_file = self._publish_bak_path(name)
            try:
                if name in existed_before and os.path.isfile(backup_file):
                    temporary = f'{live_file}.rollback.tmp'
                    shutil.copy2(backup_file, temporary)
                    os.chmod(temporary, 0o640)
                    os.replace(temporary, live_file)
                elif name not in existed_before and os.path.lexists(live_file):
                    os.unlink(live_file)
            except OSError as exc:
                errors.append(f'{name}: {exc}')
        return errors

    def publish_staged_generated(self, user_id='database'):
        """Publish the complete staged bundle without changing bird.conf."""
        filenames = self._managed_filenames()
        diff_output = self._condense_bundle_diff(self._overview_filenames())
        missing = [
            name for name in filenames
            if not os.path.isfile(os.path.join(self.staged_path, name))
        ]
        if missing:
            return {'status': 'error', 'message': f'Missing staged generated files: {missing}'}

        main_valid, main_detail = self._main_entrypoint_state()
        if not main_valid:
            message = f'Main BIRD configuration is not migrated: {main_detail}'
            self._write_apply_state(True, main_detail)
            return {'status': 'error', 'message': message, 'reload_detail': main_detail}

        expected_entrypoint = render_managed_entrypoint(
            self.live_path,
            self._policy_filenames(self.live_path),
            self._community_keys(self.staged_path),
            self._export_peer_keys(self.staged_path),
            self._has_combined_export(self.staged_path),
        )
        staged_entrypoint = self._read_text(
            self._managed_entrypoint_path(self.staged_path)
        )
        if staged_entrypoint != expected_entrypoint:
            detail = 'staged bird-manager.conf does not match the generated bundle'
            self._write_apply_state(True, detail)
            return {'status': 'error', 'message': 'Generated bundle validation failed', 'reload_detail': detail}

        valid, detail = self._validate_staged_bundle()
        if not valid:
            self._write_apply_state(True, detail)
            return {'status': 'error', 'message': 'Generated bundle validation failed', 'reload_detail': detail}

        current_names = {
            name for name in os.listdir(self.live_path)
            if re.fullmatch(r'policy_\d+\.conf', name)
            or name in {'networks.conf', 'core.conf', MANAGED_CONFIG_FILENAME}
            or name == 'routes.conf'
            or (name.startswith('routes_') and name.endswith('.conf'))
            or (name.startswith('export_') and name.endswith('.conf'))
        }
        bundle_names = sorted(set(filenames) | current_names)
        existed_before = set(current_names)
        try:
            os.makedirs(self.publish_bak_dir, exist_ok=True)
            for name in bundle_names:
                live_file = os.path.join(self.live_path, name)
                backup_file = self._publish_bak_path(name)
                if os.path.isfile(live_file):
                    backup_temp = f'{backup_file}.tmp'
                    shutil.copy2(live_file, backup_temp)
                    os.replace(backup_temp, backup_file)
                elif os.path.exists(backup_file):
                    os.unlink(backup_file)
            for name in self._publish_order(filenames):
                staged_file = os.path.join(self.staged_path, name)
                live_file = os.path.join(self.live_path, name)
                temporary = f'{live_file}.tmp'
                shutil.copy2(staged_file, temporary)
                os.chmod(temporary, 0o640)
                os.replace(temporary, live_file)
            for name in sorted(current_names - set(filenames)):
                if (re.fullmatch(r'policy_\d+\.conf', name)
                        or (name.startswith('routes_') and name.endswith('.conf'))
                        or (name.startswith('export_') and name.endswith('.conf'))):
                    os.unlink(os.path.join(self.live_path, name))
            os.makedirs(os.path.dirname(self.manifest_path) or '.', exist_ok=True)
            manifest_temp = f'{self.manifest_path}.tmp'
            with open(manifest_temp, 'w', encoding='utf-8') as stream:
                json.dump({'files': filenames}, stream, indent=2)
                stream.write('\n')
            os.chmod(manifest_temp, 0o640)
            os.replace(manifest_temp, self.manifest_path)
        except OSError as exc:
            rollback_errors = self._restore_publish_snapshot(bundle_names, existed_before)
            detail = str(exc)
            if rollback_errors:
                detail = f'{detail}; rollback errors: {"; ".join(rollback_errors)}'
            self._write_apply_state(True, detail)
            return {
                'status': 'error',
                'message': 'Failed to publish generated bundle',
                'reload_detail': detail,
            }

        reload_success, reload_warning, reload_detail = self._attempt_bird_reload()
        self._write_apply_state(reload_warning, reload_detail)
        status = 'success' if reload_success else 'success_with_warning'
        return {
            'status': status,
            'message': 'Generated configuration published.',
            'files': filenames,
            'diff': diff_output,
            'reload_warning': reload_warning,
            'reload_detail': reload_detail,
            'user_id': user_id,
        }

    def apply_config(self, filename, user_id='system'):
        """Apply staged configuration to live directory and reload BIRD.

        In split mode (the packaged layout) this short-circuits into
        :meth:`publish_staged_generated`, which publishes the whole bundle and
        takes its own rollback snapshot. The per-file steps documented below
        only run for the monolithic layout:

          1. Check staged file exists.
          2. Validate syntax of staged config.
          3. If invalid — abort, return errors.
          4. Generate diff between live and staged.
          5. Copy staged → live atomically.
          6. Attempt BIRD reload (best-effort).
          7. Write audit log entry.

        Args:
            filename: Name of the configuration file to apply.
            user_id: User who initiated the apply.

        Returns:
            Dictionary with keys: status, message, diff, audit_id, reload_warning.
        """
        # Step 1: Check staged file exists
        staged_file = os.path.join(self.staged_path, filename)
        if not os.path.exists(staged_file):
            return {
                "status": "file_not_found",
                "message": f"Staged file {filename} not found.",
                "diff": "",
                "audit_id": None,
                "reload_warning": False,
            }

        if self.split_mode:
            old_prefixes = self._route_prefixes(self.live_path)
            result = self.publish_staged_generated(user_id=user_id)
            if result.get('status') in ('success', 'success_with_warning'):
                self.sync_statuses_with_config(
                    self._read_all_routes_text() or ''
                )
                new_prefixes = self._route_prefixes(self.staged_path)
                affected = sorted(old_prefixes ^ new_prefixes)
                result['applied_networks'] = affected
                try:
                    session = self.db_session_factory()
                    audit = AuditLog(
                        action='APPLY_CONFIG',
                        user_id=user_id,
                        timestamp=local_now(),
                        diff_summary=result.get('diff', ''),
                    )
                    session.add(audit)
                    session.commit()
                    result['audit_id'] = audit.id
                    session.close()
                except Exception:
                    result['audit_id'] = None
            return result

        # Step 2-3: Validate syntax — abort if invalid
        validator = SyntaxValidator()
        validator.staged_path = self.staged_path
        validation_result = validator.validate_staged_config(filename)

        if not validation_result["valid"]:
            return {
                "status": "error",
                "message": f"Syntax validation failed: {validation_result['summary']}",
                "errors": validation_result["errors"],
                "error_lines": validation_result.get("error_lines", []),
                "diff": "",
                "audit_id": None,
                "reload_warning": False,
            }

        # Step 4: Generate diff for audit trail
        diff_engine = DiffEngine(self.live_path, self.staged_path)
        diff_output = diff_engine.get_diff(filename)

        # Step 5: Atomic copy staged → live
        live_file = os.path.join(self.live_path, filename)

        # Save rollback snapshot of the current live file if it exists
        backup_file = self._publish_bak_path(filename)
        try:
            os.makedirs(self.publish_bak_dir, exist_ok=True)
            if os.path.exists(live_file):
                backup_temp = f'{backup_file}.tmp'
                shutil.copy2(live_file, backup_temp)
                os.replace(backup_temp, backup_file)
        except IOError:
            pass  # Snapshot is optional

        try:
            # Write to temp file first, then rename for atomicity
            temp_file = os.path.join(self.live_path, f"{filename}.tmp")
            shutil.copy2(staged_file, temp_file)
            os.replace(temp_file, live_file)  # Cross-platform atomic replace
        except OSError as e:
            return {
                "status": "error",
                "message": f"Failed to copy staged to live: {str(e)}",
                "diff": diff_output,
                "audit_id": None,
                "reload_warning": False,
            }


        # Step 6: Attempt BIRD reload — best-effort
        reload_success, reload_warning, reload_detail = self._attempt_bird_reload()
        self._write_apply_state(reload_warning, reload_detail)

        # Step 7: Write audit log entry
        try:
            session = self.db_session_factory()

            audit = AuditLog(
                action='APPLY_CONFIG',
                user_id=user_id,
                timestamp=local_now(),
                diff_summary=diff_output,
            )
            session.add(audit)
            session.commit()
            audit_id = audit.id
        except Exception:
            audit_id = None

        applied = self.sync_statuses_with_config(
            self._read_all_routes_text() or ''
        )

        status = "success" if reload_success else "success_with_warning"
        message = "Configuration applied and BIRD reloaded successfully."
        if reload_warning:
            message = (
                "Configuration applied, but BIRD reload failed. "
                "Check BIRD daemon status manually."
            )
            if reload_detail:
                message = f"Configuration applied, but BIRD reload failed: {reload_detail}"

        return {
            "status": status,
            "message": message,
            "diff": diff_output,
            "audit_id": audit_id,
            "reload_warning": reload_warning,
            "reload_detail": reload_detail,
            "applied_networks": applied,
        }

    def _rollback_split_bundle(self, user_id='system'):
        names = self._overview_filenames()
        backups = [
            self._publish_bak_path(name)
            for name in names
            if os.path.isfile(self._publish_bak_path(name))
        ]
        if not backups:
            return {
                'status': 'backup_not_found',
                'message': 'No full bundle snapshot found. Cannot rollback.',
                'audit_id': None,
                'reload_warning': False,
            }
        try:
            for name in names:
                live_file = os.path.join(self.live_path, name)
                backup_file = self._publish_bak_path(name)
                if os.path.isfile(backup_file):
                    temp_file = f'{live_file}.rollback.tmp'
                    shutil.copy2(backup_file, temp_file)
                    os.chmod(temp_file, 0o640)
                    os.replace(temp_file, live_file)
                elif os.path.isfile(live_file):
                    os.unlink(live_file)
        except OSError as exc:
            return {
                'status': 'error',
                'message': f'Failed to restore full bundle: {exc}',
                'audit_id': None,
                'reload_warning': False,
            }

        reload_success, reload_warning, reload_detail = self._attempt_bird_reload()
        self._write_apply_state(reload_warning, reload_detail)
        audit_id = None
        try:
            session = self.db_session_factory()
            audit = AuditLog(
                action='ROLLBACK_CONFIG',
                user_id=user_id,
                timestamp=local_now(),
                diff_summary='Full staged bundle rolled back',
            )
            session.add(audit)
            session.commit()
            audit_id = audit.id
            session.close()
        except Exception:
            audit_id = None
        status = 'success' if reload_success else 'success_with_warning'
        message = 'Full configuration rolled back successfully.'
        if reload_detail:
            message = f'Full configuration rolled back, but BIRD reload failed: {reload_detail}'
        return {
            'status': status,
            'message': message,
            'audit_id': audit_id,
            'reload_warning': reload_warning,
            'reload_detail': reload_detail,
        }

    def rollback_last_apply(self, filename, user_id='system'):
        """Rollback the last applied configuration for a file.

        Requires a publish snapshot in the state directory
        (``BIRD_PUBLISH_BAK_DIR``).

        Args:
            filename: Name of the configuration file to rollback.
            user_id: User who initiated the rollback.

        Returns:
            Dictionary with keys: status, message, audit_id, reload_warning.
        """
        if self.split_mode:
            return self._rollback_split_bundle(user_id=user_id)

        live_file = os.path.join(self.live_path, filename)
        backup_file = self._publish_bak_path(filename)

        if not os.path.exists(backup_file):
            return {
                "status": "backup_not_found",
                "message": f"No snapshot found for {filename}. Cannot rollback.",
                "audit_id": None,
                "reload_warning": False,
            }

        # Restore from snapshot
        try:
            temporary = f'{live_file}.rollback.tmp'
            shutil.copy2(backup_file, temporary)
            os.chmod(temporary, 0o640)
            os.replace(temporary, live_file)
        except OSError as e:
            return {
                "status": "error",
                "message": f"Failed to restore snapshot for {filename}: {str(e)}",
                "audit_id": None,
                "reload_warning": False,
            }


        # Attempt BIRD reload — best-effort
        reload_success, reload_warning, reload_detail = self._attempt_bird_reload()
        self._write_apply_state(reload_warning, reload_detail)

        # Write audit log entry
        try:
            session = self.db_session_factory()

            audit = AuditLog(
                action='ROLLBACK_CONFIG',
                user_id=user_id,
                timestamp=local_now(),
                diff_summary=f"Rollback of {filename} applied",
            )
            session.add(audit)
            session.commit()
            audit_id = audit.id
        except Exception:
            audit_id = None

        status = "success" if reload_success else "success_with_warning"
        if reload_success:
            message = f"Configuration rolled back to {filename} successfully."
        else:
            message = f"Rollback of {filename} applied, but BIRD reload failed."
            if reload_detail:
                message = f"Rollback of {filename} applied, but BIRD reload failed: {reload_detail}"

        return {
            "status": status,
            "message": message,
            "audit_id": audit_id,
            "reload_warning": reload_warning,
            "reload_detail": reload_detail,
        }

    # ------------------------------------------------------------------
    # Content merge + status sync (variant B: apply from editor content)
    # ------------------------------------------------------------------

    def _merge_managed_blocks(self, base_content, networks, communities, bgp_peers=None):
        """Merge DB-derived blocks into a user-edited config skeleton.

        Managed lines (routes, DB community defines, and the whole generated
        filter/static/direct/bgp blocks) are stripped from *base_content*,
        then fresh DB blocks are inserted under the corresponding section
        markers. An empty skeleton produces a full DB-driven config. Legacy
        ``//`` comment lines in user content are rewritten to ``#`` so BIRD
        accepts the result.

        Returns:
            Merged configuration string (not written to disk).
        """
        if self.split_mode:
            base_content = normalize_bird_comments(base_content)
            return _build_split_base_config(None, None, base_content)

        redistribute_lines = build_redistribute_lines(networks)
        community_blocks = build_community_blocks(communities)
        peer_addrs = [f'{p["neighbor"]}/32' for p in _valid_peers(bgp_peers)]
        export_lines = build_export_config(
            networks, communities, peer_addrs, bgp_peers
        ).splitlines()
        direct_lines = build_direct_block()
        static_lines = build_static_block(redistribute_lines, bgp_peers)
        bgp_lines = build_bgp_blocks(bgp_peers)
        device_lines = build_device_block()

        base_content = normalize_bird_comments(base_content)
        skeleton = strip_managed_blocks(base_content)
        if not (skeleton or '').strip():
            return _build_full_config(
                redistribute_lines, community_blocks, export_lines, bgp_peers=bgp_peers
            )

        markers = {
            '# Communities': community_blocks,
            '# Export policy': export_lines,
            '# Device': device_lines,
            '# Direct routes': direct_lines,
            '# Static routes': static_lines,
            '# BGP sessions': bgp_lines,
        }

        out = []
        present = set()
        for line in skeleton.rstrip('\n').splitlines():
            out.append(line)
            marker = line.strip()
            if marker in markers:
                present.add(marker)
                out.extend(markers[marker])

        tail = []
        for marker, block in markers.items():
            if block and marker not in present:
                tail.extend([marker] + block)

        if tail:
            if out:
                out.append('')
            out.extend(tail)

        return _canonicalize_blank_lines('\n'.join(out))

    def merge_and_stage(self, filename, base_content, networks, communities, bgp_peers=None):
        """Merge DB blocks into *base_content* and write the result to staged."""
        merged = self._merge_managed_blocks(base_content, networks, communities, bgp_peers)
        merged = strip_ghost_markers(merged)
        staged_file = os.path.join(self.staged_path, filename)
        with open(staged_file, 'w', encoding='utf-8') as f:
            f.write(merged)
        return merged

    def preview_diff(self, filename, base_content, networks, communities, bgp_peers=None):
        """Merge without writing; return (merged, unified live→merged diff)."""
        merged = self._merge_managed_blocks(base_content, networks, communities, bgp_peers)
        merged = strip_ghost_markers(merged)
        live_content = self._read_live(filename)
        diff_output = diff_contents(live_content, merged)
        return merged, diff_output

    def build_editor_view(self, filename, content, merged):
        """Build the editor text and the aligned rows for the change pane.

        The model is the same live→staged line pairing the read-only file panes
        use, so editor row N is right pane row N. A live line the edit dropped
        has no counterpart in *merged*, so a ghost row is inserted into the
        editor text for it — without that row every line below it would sit one
        position off. ``strip_ghost_markers`` removes the ghosts before
        anything is written, so they never reach the published config unless
        the operator types onto one of those rows.

        Returns:
            - editor_content: text for the editor, ghost rows included,
            - rows: the aligned model, one entry per editor line.
        """
        model = aligned_rows(self._read_live(filename), merged)
        out_lines = [
            GHOST_MARKER if row['kind'] == 'removed'
            else ('' if row['new'] is None else row['new'])
            for row in model['rows']
        ]
        editor_content = '\n'.join(out_lines)
        if out_lines:
            editor_content += '\n'
        return editor_content, model['rows']

    def _editor_error_rows(self, content, merged, filename, error_lines, error_files):
        """Map validation errors to editor rows of the edited file only.

        The parse runs over the whole bundle, so a reported line number may
        belong to ``core.conf`` or another generated file. Painting that row
        inside the ``networks.conf`` editor would point at a line that does not
        exist there, so such errors are surfaced by message only. An error
        whose file cannot be determined is kept: those come from the regex
        checks on the edited text itself.
        """
        rows_map = self._merged_to_editor_rows(content, merged)
        rows = []
        for index, line in enumerate(error_lines or []):
            if not isinstance(line, int):
                continue
            error_file = error_files[index] if index < len(error_files or []) else None
            if error_file and os.path.basename(str(error_file)) != filename:
                continue
            rows.append(rows_map.get(line, line))
        return rows

    def _merged_to_editor_rows(self, content, merged):
        """Map 1-based merged line numbers to editor_content line numbers.

        ``content`` is the raw editor text and ``merged`` the database-merged
        result. The mapping mirrors the diff used by :meth:`build_editor_view`
        (ghost rows keep equal runs aligned), so an error reported against a
        merged/staged line lands on the correct visible line of the editor.

        Returns:
            Dict ``{merged_line_number: editor_line_number}`` for merged lines
            that are visible in the editor text.
        """
        content_lines = (content or '').splitlines()
        merged_lines = (merged or '').splitlines()
        matcher = difflib.SequenceMatcher(
            None, content_lines, merged_lines, autojunk=False
        )

        mapping = {}
        editor_row = 0
        merged_row = 0
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == 'equal':
                for _ in range(i2 - i1):
                    editor_row += 1
                    merged_row += 1
                    mapping[merged_row] = editor_row
            elif tag == 'delete':
                editor_row += (i2 - i1)
            elif tag == 'insert':
                for _ in range(j1, j2):
                    merged_row += 1
                    editor_row += 1
                    mapping[merged_row] = editor_row
            elif tag == 'replace':
                editor_row += (i2 - i1)
                for _ in range(j1, j2):
                    merged_row += 1
                    editor_row += 1
                    mapping[merged_row] = editor_row
        return mapping

    def _apply_state_path(self):
        """Path of the small JSON carrying the last BIRD reload outcome."""
        parent = os.path.dirname(self.staged_path.rstrip('/\\')) or '.'
        return os.path.join(parent, 'apply_state.json')

    def _write_apply_state(self, reload_warning, reload_detail):
        """Persist the reload outcome next to the staged directory."""
        try:
            path = self._apply_state_path()
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({
                    'reload_warning': bool(reload_warning),
                    'reload_detail': reload_detail or None,
                }, f)
        except OSError:
            pass

    def _read_apply_state(self):
        """Read the last reload outcome; defaults when absent/unreadable."""
        try:
            with open(self._apply_state_path(), 'r', encoding='utf-8') as f:
                data = json.load(f)
            return {
                'reload_warning': bool(data.get('reload_warning')),
                'reload_detail': data.get('reload_detail'),
            }
        except (OSError, ValueError):
            return {'reload_warning': False, 'reload_detail': None}

    def apply_config_from_content(self, filename, content, user_id, networks, communities, bgp_peers=None):
        """Merge content into staged, apply it, and sync network statuses.

        Full workflow: merge_and_stage → apply_config() → on success
        sync_statuses_with_config(). On failure the result is extended with
        'editor_content' (the aligned editor text) and 'error_rows' (merged
        error lines mapped to editor rows) so the UI can highlight them.
        Returns the apply result extended with 'live_content' and
        'applied_networks' (or the error view fields above).
        """
        merged = self.merge_and_stage(filename, content, networks, communities, bgp_peers)
        result = self.apply_config(filename=filename, user_id=user_id)
        applied_networks = result.get('applied_networks')
        if applied_networks is None:
            applied_networks = []
            if result.get('status') in ('success', 'success_with_warning'):
                applied_content = self._read_all_routes_text() or merged
                applied_networks = self.sync_statuses_with_config(applied_content)
        if result.get('status') not in ('success', 'success_with_warning'):
            editor_content, _rows = self.build_editor_view(filename, content, merged)
            result['editor_content'] = editor_content
            result['error_rows'] = self._editor_error_rows(
                content, merged, filename,
                result.get('error_lines', []), result.get('error_files', []),
            )
        result['live_content'] = merged
        result['applied_networks'] = applied_networks
        return result

    def mark_pending_applied(self):
        """Set status='applied' for every network still marked 'pending'."""
        session = self.db_session_factory()
        try:
            count = (
                session.query(Network)
                .filter(Network.status == 'pending')
                .update({'status': 'applied'}, synchronize_session=False)
            )
            session.commit()
            return count
        except Exception:
            session.rollback()
            return 0
        finally:
            try:
                session.close()
            except Exception:
                pass

    def sync_statuses_with_config(self, live_content):
        """Mark pending networks whose CIDR is present in *live_content*.

        Only *pending* records are touched; records already carrying a status
        are left alone. Returns the list of prefixes that became 'applied'.
        """
        session = self.db_session_factory()
        if session is None:
            return []
        affected = []
        try:
            import re
            present_prefixes = set(
                re.findall(r'route\s+(\S+)', live_content or '')
            )
            networks = session.query(Network).all()
            for net in networks:
                if not net.prefix:
                    continue
                present = (
                    net.enabled is not False
                    and net.prefix in present_prefixes
                )
                if present and net.status != 'applied':
                    net.status = 'applied'
                    affected.append(net.prefix)
                elif (not present) and net.status == 'applied':
                    net.status = 'pending'
                    affected.append(net.prefix)
            session.commit()
        except Exception:
            session.rollback()
        finally:
            try:
                session.close()
            except Exception:
                pass
        return affected

    @staticmethod
    def _read_text(path):
        """Text of *path*, or None when missing or not decodable.

        ``UnicodeDecodeError`` is a ``ValueError``, not an ``OSError``: a file
        that is not UTF-8 — a stray binary in the live directory, or a config
        saved in a legacy codepage — used to propagate out of the overview and
        turn the whole panel into a 500 instead of one unreadable file.
        """
        if not os.path.isfile(path):
            return None
        try:
            with open(path, 'r', encoding='utf-8') as stream:
                return stream.read()
        except (OSError, ValueError):
            return None

    def _read_all_routes_text(self):
        """Concatenate text of all routes_*.conf (and legacy routes.conf)."""
        parts = []
        try:
            names = sorted(os.listdir(self.staged_path))
        except OSError:
            names = []
        for name in names:
            if name == 'routes.conf' or (name.startswith('routes_') and name.endswith('.conf')):
                text = self._read_text(os.path.join(self.staged_path, name))
                if text:
                    parts.append(text)
        if not parts:
            text = self._read_text(os.path.join(self.staged_path, 'routes.conf'))
            if text:
                parts.append(text)
        return '\n'.join(parts)

    def _route_prefixes(self, directory):
        """Set of route prefixes found in the routes files of *directory*."""
        import re
        prefixes = set()
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            names = []
        for name in names:
            if name == 'routes.conf' or (name.startswith('routes_') and name.endswith('.conf')):
                text = self._read_text(os.path.join(directory, name))
                if text:
                    prefixes.update(re.findall(r'route\s+(\S+)', text))
        if not prefixes:
            text = self._read_text(os.path.join(directory, 'routes.conf'))
            if text:
                prefixes.update(re.findall(r'route\s+(\S+)', text))
        return prefixes

    def _read_live(self, filename):
        """Content of <live_path>/<filename>, or '' if missing/unreadable."""
        path = os.path.join(self.live_path, filename)
        if not os.path.isfile(path):
            return ''
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        except OSError:
            return ''

    @staticmethod
    def _bird_socket_path():
        """Control socket path the reload targets (env override or default)."""
        socket_path = os.getenv('BIRD_SOCKET')
        if socket_path:
            return socket_path
        return '/run/bird/bird.ctl'

    def _birdc_command(self, command):
        birdc_bin = os.getenv('BIRDC_BIN_PATH', 'birdc')
        bird_socket = os.getenv('BIRD_SOCKET', '')
        result = [birdc_bin]
        if bird_socket:
            result.extend(['-s', bird_socket])
        result.append(command)
        return result

    @staticmethod
    def _format_bird_timestamp(value):
        try:
            return datetime.fromisoformat(value).strftime('%d.%m.%Y %H:%M:%S')
        except (TypeError, ValueError):
            return value or None

    def _bird_status_timestamps(self):
        try:
            process = subprocess.run(
                self._birdc_command('show status'),
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return None, None
        if process.returncode != 0:
            return None, None
        output = process.stdout or ''
        reboot = re.search(r'^Last reboot on (.+)$', output, re.MULTILINE)
        reconfiguration = re.search(
            r'^Last reconfiguration on (.+)$', output, re.MULTILINE
        )
        return (
            self._format_bird_timestamp(reboot.group(1).strip()) if reboot else None,
            self._format_bird_timestamp(reconfiguration.group(1).strip()) if reconfiguration else None,
        )

    def _attempt_bird_reload(self):
        """Attempt best-effort BIRD reload via birdc.

        The birdc binary and control socket are configurable through the
        ``BIRDC_BIN_PATH`` and ``BIRD_SOCKET`` environment variables. When the
        socket is empty, birdc uses the daemon default (host deployment).
        ``configure`` is the BIRD command that re-reads the configuration; the
        daemon keeps the running config if the new one fails to parse.

        Returns:
            Tuple (success, warning, detail). Success is True when the reload
            command completed or was skipped because birdc is unavailable.
            Warning is True only when a reload attempt that was actually
            present failed. Detail carries the daemon error text (or timeout
            message) when the attempt failed, None otherwise.
        """
        try:
            process = subprocess.run(
                self._birdc_command('configure'),
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if process.returncode != 0:
                detail = self._birdc_error_detail(process.stderr or process.stdout)
                return False, True, detail or f"birdc exited with {process.returncode}"
            return True, False, None
        except FileNotFoundError:
            # birdc not installed — reload step skipped, treat as success.
            return True, False, None
        except subprocess.TimeoutExpired as e:
            return False, True, f"birdc timed out: {e} (timeout after 10s)"
        except OSError as e:
            return False, True, str(e)

    @staticmethod
    def _birdc_error_detail(output):
        """Trim birdc output to a short, human-readable error message."""
        if not output:
            return None
        text = output.strip()
        if not text:
            return None
        return text[:300]

    @staticmethod
    def _parse_protocols_output(output):
        """Parse ``birdc show protocols all`` into per-protocol records.

        The output is a column-aligned summary table followed by an indented
        detail block per protocol::

            main_mkt   BGP   ---   start   2026-09-25   Active   Socket: ...
              BGP state:          Active
                Neighbor address: 192.168.0.1
                Neighbor AS:      64999
                Local AS:         65500
                Last error:       Socket: Connection refused
              Channel ipv4
                State:            DOWN
                Routes:           1204 imported, 1180 exported, 1204 preferred

        Column positions cannot be used: BIRD pads the name column to the widest
        protocol name and separates the remaining columns with spaces, so the
        gap between Name and Proto is sometimes a single space. The summary row
        is therefore anchored on the columns that have a fixed shape - the table
        (``master4`` / ``---``) and ``Since``. Protocols are classified by the
        ``Proto`` column, so every BGP session is reported, including ones added
        to bird.conf by hand.

        ``Since`` comes in three shapes and all three occur in the wild:

        * ``2026-09-25`` - date only, for a protocol that started days ago;
        * ``2026-09-26 12:04:11`` - date and time;
        * ``10:08:14.529`` - bare time, which is what BIRD prints for anything
          started on the current day.

        Requiring the date made the whole table unparseable after a same-day
        restart, so a live session was reported as missing from BIRD. A bare
        time cannot be confused with a date, and the optional fractional part
        is part of the time token in both shapes.
        """
        time_token = r'\d{2}:\d{2}:\d{2}(?:\.\d+)?'
        since_token = r'(?:\d{4}-\d{2}-\d{2}(?:\s+' + time_token + r')?|' + time_token + r')'
        summary_pattern = re.compile(
            r'^(?P<name>\S+)\s+'
            r'(?P<proto>[A-Z][A-Za-z0-9_]*)\s+'
            r'(?P<table>master\d+|---)\s+'
            r'(?P<state>\S+)\s+'
            r'(?P<since>' + since_token + r')\s*'
            r'(?P<info>.*)$'
        )
        records = []
        current = None
        channel = None
        for raw_line in (output or '').splitlines():
            line = raw_line.rstrip()
            if not line.strip():
                continue
            if not raw_line[:1].isspace():
                match = summary_pattern.match(line.strip())
                if not match:
                    # Banner ("BIRD 3.2.0 ready.") or the column header row.
                    current = None
                    channel = None
                    continue
                current = {
                    'name': match.group('name'),
                    'proto': match.group('proto'),
                    'table': match.group('table'),
                    'state': match.group('state'),
                    'since': match.group('since'),
                    'info': match.group('info').strip() or None,
                    'bgp_state': None,
                    'neighbor': None,
                    'neighbor_as': None,
                    'local_as': None,
                    'connect_delay': None,
                    'last_error': None,
                    'channel_state': None,
                    'import_state': None,
                    'export_state': None,
                    'imported': None,
                    'exported': None,
                }
                channel = None
                records.append(current)
                continue
            if current is None:
                continue
            stripped = line.strip()
            if stripped.lower().startswith('channel '):
                channel = stripped
                continue
            if ':' not in stripped:
                continue
            key, _, value = stripped.partition(':')
            key = key.strip().lower()
            value = value.strip()
            if channel is None:
                if key == 'bgp state':
                    current['bgp_state'] = value or None
                elif key == 'neighbor address':
                    current['neighbor'] = value or None
                elif key == 'neighbor as':
                    current['neighbor_as'] = value or None
                elif key == 'local as':
                    current['local_as'] = value or None
                elif key == 'connect delay':
                    current['connect_delay'] = value or None
                elif key == 'last error':
                    current['last_error'] = value or None
                continue
            if key == 'state':
                current['channel_state'] = value or None
            elif key == 'import state':
                current['import_state'] = value or None
            elif key == 'export state':
                current['export_state'] = value or None
            elif key == 'routes':
                imported = re.search(r'(\d+)\s+imported', value)
                exported = re.search(r'(\d+)\s+exported', value)
                current['imported'] = int(imported.group(1)) if imported else None
                current['exported'] = int(exported.group(1)) if exported else None
        return records

    def _bird_protocols(self):
        """Live protocol/session snapshot from ``birdc show protocols all``."""
        result = {
            'bird_running': os.path.exists(self._bird_socket_path()),
            'error': None,
            'bgp': [],
            'service': [],
        }
        if not result['bird_running']:
            result['error'] = 'BIRD is not running (control socket is missing)'
            return result
        try:
            process = subprocess.run(
                self._birdc_command('show protocols all'),
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError:
            result['error'] = 'birdc binary not found'
            return result
        except subprocess.TimeoutExpired:
            result['error'] = 'birdc timed out after 10s'
            return result
        except OSError as exc:
            result['error'] = str(exc)
            return result
        if process.returncode != 0:
            result['error'] = self._birdc_error_detail(
                process.stderr or process.stdout
            ) or f'birdc exited with {process.returncode}'
            return result
        for record in self._parse_protocols_output(process.stdout):
            if record['proto'].upper() == 'BGP':
                result['bgp'].append({
                    'name': record['name'],
                    'state': record['state'],
                    'bgp_state': record['bgp_state'],
                    'channel_state': record['channel_state'],
                    'neighbor': record['neighbor'],
                    'neighbor_as': record['neighbor_as'],
                    'local_as': record['local_as'],
                    'since': record['since'],
                    'info': record['info'],
                    'last_error': record['last_error'],
                    'imported': record['imported'],
                    'exported': record['exported'],
                    'in_bird': True,
                })
            else:
                result['service'].append({
                    'name': record['name'],
                    'proto': record['proto'],
                    'state': record['state'],
                    'channel_state': record['channel_state'],
                    'import_state': record['import_state'],
                    'since': record['since'],
                })
        return result

    def get_apply_status(self, filename):
        """Get the last apply metadata for a file.

        Args:
            filename: Name of the configuration file.

        Returns:
            Dictionary with keys: last_applied, last_diff_summary, has_backup,
            reload_warning, reload_detail, bird_running.
        """
        state = self._read_apply_state()
        bird_running = os.path.exists(self._bird_socket_path())
        bird_last_reboot, bird_last_reconfiguration = self._bird_status_timestamps()
        has_backup = os.path.isfile(self._publish_bak_path(filename))
        try:
            session = self.db_session_factory()
            # Find the most recent APPLY_CONFIG or ROLLBACK_CONFIG for this file
            from sqlalchemy import desc

            audit_entries = (
                session.query(AuditLog)
                .filter(
                    AuditLog.action.in_(['APPLY_CONFIG', 'ROLLBACK_CONFIG'])
                )
                .order_by(desc(AuditLog.id))
                .limit(1)
                .all()
            )

            if audit_entries:
                last_entry = audit_entries[0]
                return {
                    "last_applied": str(last_entry.timestamp),
                    "last_diff_summary": last_entry.diff_summary,
                    "action": last_entry.action,
                    "user_id": last_entry.user_id,
                    "has_backup": has_backup,
                    "reload_warning": state["reload_warning"],
                    "reload_detail": state["reload_detail"],
                    "bird_running": bird_running,
                    "bird_last_reboot": bird_last_reboot,
                    "bird_last_reconfiguration": bird_last_reconfiguration,
                }

            return {
                "last_applied": None,
                "last_diff_summary": None,
                "action": None,
                "user_id": None,
                "has_backup": has_backup,
                "reload_warning": state["reload_warning"],
                "reload_detail": state["reload_detail"],
                "bird_running": bird_running,
                "bird_last_reboot": bird_last_reboot,
                "bird_last_reconfiguration": bird_last_reconfiguration,
            }
        except Exception:
            return {
                "last_applied": None,
                "last_diff_summary": None,
                "action": None,
                "user_id": None,
                "reload_warning": state["reload_warning"],
                "reload_detail": state["reload_detail"],
                "bird_running": bird_running,
                "bird_last_reboot": bird_last_reboot,
                "bird_last_reconfiguration": bird_last_reconfiguration,
                "has_backup": has_backup,
                "error": "Failed to query audit log",
            }
