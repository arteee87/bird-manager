"""Full-snapshot backup of bird-manager state.

An archive contains the SQLite database, the staged and live trees,
``managed-files.json`` and a copy of the main ``bird.conf`` kept for
offline recovery only. The pre-publish rollback snapshot
(``live-bak/``) is deliberately not archived: it is regenerated on
every apply.
"""

import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime

try:
    from .utils import local_now
    from .config_generator import (
        MANAGED_CONFIG_FILENAME,
        main_config_status,
        render_managed_entrypoint,
    )
except ImportError:
    from utils import local_now
    from config_generator import (
        MANAGED_CONFIG_FILENAME,
        main_config_status,
        render_managed_entrypoint,
    )

SNAPSHOT_FILENAME = 'bird-manager-snapshot.tar.gz'
DEFAULT_RETENTION = 5
BACKUP_ID_RE = re.compile(r'^\d{8}-\d{6}-\d{6}$')


def backup_retention():
    raw = (os.getenv('BIRD_BACKUP_RETENTION') or '').strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_RETENTION
    return value if 1 <= value <= 99 else DEFAULT_RETENTION


def snapshot_path(db_path):
    return os.path.join(backup_paths.backup_dir(db_path), SNAPSHOT_FILENAME)


def backup_directory(db_path):
    return os.path.dirname(snapshot_path(db_path)) or '.'


def _backup_items(db_path):
    directory = backup_directory(db_path)
    prefix = SNAPSHOT_FILENAME[:-len('.tar.gz')]
    suffix = '.tar.gz'
    items = []
    try:
        names = os.listdir(directory)
    except OSError:
        return items
    for name in names:
        if name == SNAPSHOT_FILENAME:
            backup_id = 'latest'
        elif name.startswith(prefix + '.') and name.endswith(suffix):
            backup_id = name[len(prefix) + 1:-len(suffix)]
            if not BACKUP_ID_RE.fullmatch(backup_id):
                continue
        else:
            continue
        path = os.path.join(directory, name)
        try:
            stat_result = os.stat(path)
        except OSError:
            continue
        if not os.path.isfile(path):
            continue
        created = datetime.fromtimestamp(stat_result.st_mtime)
        items.append({
            'id': backup_id,
            'created_at': created.isoformat(timespec='seconds'),
            'display_time': created.strftime('%d.%m.%Y %H:%M:%S'),
            'legacy_time': created.strftime('%Y-%m-%d %H:%M:%S'),
            'size': stat_result.st_size,
            'mtime_ns': stat_result.st_mtime_ns,
            'latest': False,
        })
    items.sort(key=lambda item: (item['mtime_ns'], item['id']), reverse=True)
    if items:
        items[0]['latest'] = True
    for item in items:
        item.pop('mtime_ns', None)
    return items


def resolve_backup_path(backup_id, db_path):
    if backup_id == 'latest':
        path = snapshot_path(db_path)
        return path if os.path.isfile(path) else None
    if not backup_id or not BACKUP_ID_RE.fullmatch(backup_id):
        raise ValueError('Некорректный идентификатор бекапа')
    prefix = SNAPSHOT_FILENAME[:-len('.tar.gz')]
    path = os.path.join(backup_directory(db_path), f'{prefix}.{backup_id}.tar.gz')
    return path if os.path.isfile(path) else None


def delete_backup(backup_id, db_path):
    path = resolve_backup_path(backup_id, db_path)
    if path is None:
        return {'status': 'error', 'message': 'Бекап не найден'}
    os.unlink(path)
    return {
        'status': 'success',
        'message': 'Бекап удалён',
        'backup': backup_info(db_path),
    }


def _add_tree(tar, root, arc_prefix):
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            arcname = os.path.join(arc_prefix, os.path.relpath(full, root))
            tar.add(full, arcname=arcname)


def _member_names(members, prefix):
    return sorted({member.name for member in members if member.name.startswith(prefix + '/')})


def _clear_directory(dest):
    if not os.path.lexists(dest):
        os.makedirs(dest, mode=0o750)
        return
    if not os.path.isdir(dest) or os.path.islink(dest):
        raise ValueError(f'backup destination is not a directory: {dest}')
    for entry in os.scandir(dest):
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
        else:
            os.unlink(entry.path)


def _restore_tree(tmp, prefix, dest):
    src = os.path.join(tmp, prefix)
    if not os.path.isdir(src):
        return
    _clear_directory(dest)
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames.sort()
        relative_dir = os.path.relpath(dirpath, src)
        target_dir = dest if relative_dir == '.' else os.path.join(dest, relative_dir)
        os.makedirs(target_dir, mode=0o750, exist_ok=True)
        for name in sorted(filenames):
            source = os.path.join(dirpath, name)
            target = os.path.join(target_dir, name)
            shutil.copy2(source, target)


def backup_info(db_path):
    items = _backup_items(db_path)
    retention = backup_retention()
    used = len(items)
    newest = items[0] if items else None
    return {
        'exists': newest is not None,
        'last_backup': newest['legacy_time'] if newest else None,
        'size': newest['size'] if newest else 0,
        'retention': retention,
        'used': used,
        'free': max(0, retention - used),
        'slots': f'{used}/{retention}',
        'items': items,
    }


class BackupPaths:
    """Env-derived backup locations, resolved once per environment.

    The layout these paths describe is documented in README.md; the rules for
    manifest and main-config resolution are shared with ConfigManager on
    purpose, see docs/api_backend.md.

    Reading these on every call let create_backup() follow a changed
    BIRD_LIVE_DIR while ConfigManager still held the value captured at import,
    and the two then disagreed about which manifest and main config belong to
    the installation. Resolving once removes the per-call divergence; the cache
    key covers every input so a change recomputes instead of going stale.

    ``manifest_path`` and ``backup_dir`` take their paths as arguments instead of
    reading BIRD_LIVE_DIR themselves: both callers know the directories they are
    really operating on, and a manager constructed with a non-default live_path
    must derive its manifest next to *that* tree, not next to a module default.
    ``db_path`` is part of the key for the same reason — a rebound database must
    not inherit the previous one's snapshot directory.
    """

    _ENV_KEYS = ('BIRD_MAIN_CONFIG', 'BIRD_BACKUP_DIR')
    _MANIFEST_KEYS = ('BIRD_MANIFEST_PATH',)

    def __init__(self):
        self._signature = None
        self._resolved = {}
        self._manifest_key = None
        self._manifest_value = None

    def _env(self):
        signature = tuple(os.getenv(key) for key in self._ENV_KEYS)
        if signature != self._signature:
            self._resolved = {
                'main_config_path': os.getenv('BIRD_MAIN_CONFIG') or '/etc/bird/bird.conf',
                'backup_dir_override': (os.getenv('BIRD_BACKUP_DIR') or '').strip(),
            }
            self._signature = signature
        return self._resolved

    def manifest_path(self, live_path):
        key = (tuple(os.getenv(name) for name in self._MANIFEST_KEYS), live_path)
        if key != self._manifest_key:
            override = os.getenv('BIRD_MANIFEST_PATH')
            if override:
                self._manifest_value = override
            elif os.path.abspath(live_path).startswith('/etc/bird'):
                self._manifest_value = '/var/lib/bird-manager/managed-files.json'
            else:
                self._manifest_value = os.path.join(
                    os.path.dirname(live_path) or '.', 'managed-files.json'
                )
            self._manifest_key = key
        return self._manifest_value

    def main_config_path(self):
        return self._env()['main_config_path']

    def backup_dir(self, db_path):
        override = self._env()['backup_dir_override']
        if override:
            return override
        return os.path.join(os.path.dirname(db_path) or '.', 'backup')


backup_paths = BackupPaths()


def _archive_previous_snapshot(target):
    if not os.path.isfile(target):
        return
    stamp = local_now().strftime('%Y%m%d-%H%M%S-%f')
    archived = target.replace('.tar.gz', f'.{stamp}.tar.gz')
    os.replace(target, archived)
    basename = os.path.basename(target)
    prefix = basename[:-len('.tar.gz')] if basename.endswith('.tar.gz') else basename
    pattern = re.compile(r'^' + re.escape(prefix) + r'\..+\.tar\.gz$')
    archives = []
    for name in os.listdir(os.path.dirname(target)):
        if pattern.match(name):
            archives.append(os.path.join(os.path.dirname(target), name))
    archives.sort(key=lambda path: (os.stat(path).st_mtime, path), reverse=True)
    keep = max(0, backup_retention() - 1)
    for path in archives[keep:]:
        try:
            os.unlink(path)
        except OSError:
            pass


def _add_format_metadata(tar):
    payload = json.dumps({
        'format_version': 2,
        'created_at': local_now().isoformat(timespec='seconds'),
    }).encode('utf-8')
    info = tarfile.TarInfo('state/backup.json')
    info.size = len(payload)
    info.mode = 0o640
    tar.addfile(info, io.BytesIO(payload))


def create_backup(live_path, staged_path, db_path):
    target = snapshot_path(db_path)
    os.makedirs(os.path.dirname(target) or '.', mode=0o750, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix='.snapshot-',
        suffix='.tmp',
        dir=os.path.dirname(target),
    )
    os.close(fd)
    try:
        with tarfile.open(tmp_path, 'w:gz') as tar:
            if os.path.isfile(db_path):
                tar.add(db_path, arcname=os.path.join('db', os.path.basename(db_path)))
            _add_tree(tar, staged_path, 'staged')
            _add_tree(tar, live_path, 'live')
            manifest_path = backup_paths.manifest_path(live_path)
            if os.path.isfile(manifest_path):
                tar.add(manifest_path, arcname='state/managed-files.json')
            main_path = backup_paths.main_config_path()
            if os.path.isfile(main_path):
                tar.add(main_path, arcname='main/bird.conf')
            _add_format_metadata(tar)
        os.chmod(tmp_path, 0o640)
        _archive_previous_snapshot(target)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return backup_info(db_path)


def _policy_names(directory):
    try:
        return sorted(
            (
                name for name in os.listdir(directory)
                if re.fullmatch(r'policy_\d+\.conf', name)
            ),
            key=lambda name: int(name.split('_')[1].split('.')[0]),
        )
    except OSError:
        return []


def _ensure_legacy_entrypoint(tmp, live_path):
    extracted_live = os.path.join(tmp, 'live')
    if not os.path.isdir(extracted_live):
        return
    entrypoint = os.path.join(extracted_live, MANAGED_CONFIG_FILENAME)
    if os.path.isfile(entrypoint):
        return
    with open(entrypoint, 'w', encoding='utf-8') as stream:
        stream.write(render_managed_entrypoint(live_path, _policy_names(extracted_live)))


def _restore_manifest(tmp, manifest_path):
    source = os.path.join(tmp, 'state', 'managed-files.json')
    if not os.path.isfile(source):
        return
    os.makedirs(os.path.dirname(manifest_path) or '.', exist_ok=True)
    try:
        with open(source, 'r', encoding='utf-8') as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        data = {}
    files = set(data.get('files') or [])
    files.add(MANAGED_CONFIG_FILENAME)
    temporary = f'{manifest_path}.tmp'
    with open(temporary, 'w', encoding='utf-8') as stream:
        json.dump({'files': sorted(files)}, stream, indent=2)
        stream.write('\n')
    os.chmod(temporary, 0o640)
    os.replace(temporary, manifest_path)


def _validate_candidate_bundle(tmp):
    extracted_live = os.path.join(tmp, 'live')
    if not os.path.isdir(extracted_live):
        return True, None
    entrypoint = os.path.join(tmp, 'candidate-bird-manager.conf')
    with open(entrypoint, 'w', encoding='utf-8') as stream:
        stream.write(render_managed_entrypoint(extracted_live, _policy_names(extracted_live)))
    main_config = os.path.join(tmp, 'candidate-bird.conf')
    with open(main_config, 'w', encoding='utf-8') as stream:
        stream.write(f'include "{entrypoint}";\n')
    bird_bin = os.getenv('BIRD_BIN_PATH', 'bird')
    try:
        process = subprocess.run(
            [bird_bin, '-p', '-c', main_config],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return True, None
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if process.returncode != 0:
        return False, (process.stderr or process.stdout).strip()[:500]
    return True, None


def restore_backup(live_path, staged_path, db_path, backup_id=None):
    if backup_id is None:
        items = _backup_items(db_path)
        if not items:
            return {'status': 'error', 'message': 'Полный бекап не найден — сначала сделайте его'}
        backup_id = items[0]['id']
    path = resolve_backup_path(backup_id, db_path)
    if path is None:
        return {'status': 'error', 'message': 'Выбранный бекап не найден'}

    main_text = None
    main_path = backup_paths.main_config_path()
    try:
        with open(main_path, 'r', encoding='utf-8') as stream:
            main_text = stream.read()
    except OSError:
        pass
    valid, detail = main_config_status(main_text or '', live_path)
    if not valid:
        return {
            'status': 'error',
            'message': f'Миграция /etc/bird/bird.conf требуется перед restore: {detail}',
        }

    with tempfile.TemporaryDirectory(prefix='bm-restore-') as tmp:
        with tarfile.open(path, mode='r:gz') as tar:
            members = tar.getmembers()
            has_any = any(
                member.name.startswith(prefix)
                for member in members
                for prefix in ('db/', 'staged/', 'live/', 'state/', 'main/')
            )
            if not has_any:
                return {'status': 'error', 'message': 'Бекап повреждён или имеет неизвестный формат'}
            tar.extractall(tmp, filter='data')

        _ensure_legacy_entrypoint(tmp, live_path)
        valid, detail = _validate_candidate_bundle(tmp)
        if not valid:
            return {
                'status': 'error',
                'message': f'Бекап не проходит BIRD validation: {detail}',
            }
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        db_names = _member_names(members, 'db')
        if db_names:
            temporary_db = f'{db_path}.restore.tmp'
            shutil.copy2(os.path.join(tmp, db_names[0]), temporary_db)
            os.replace(temporary_db, db_path)

        _restore_tree(tmp, 'staged', staged_path)
        _restore_tree(tmp, 'live', live_path)
        _restore_manifest(tmp, backup_paths.manifest_path(live_path))

    return {'status': 'success', 'message': 'Полный бекап восстановлен'}
