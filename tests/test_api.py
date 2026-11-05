"""Isolated contract tests for the current split-config implementation."""

import contextlib
import difflib
import gzip
import io
import ipaddress
import json
import os
import pathlib
import re
import shutil
import sqlite3
import time
import subprocess
import threading
import urllib.request
import urllib.error
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.backup import backup_info, create_backup, delete_backup, restore_backup
from app.config_generator import generate_split, render_managed_main_block
from app.config_manager import ConfigManager
from app.crud_operations import CRUDOperations
from app.diff_engine import DiffEngine, aligned_rows, diff_contents, measure_rows
from app.config_generator import GHOST_MARKER
from app.models import Base, BgpPeer, Community, Network, ensure_schema


@pytest.fixture
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    ensure_schema(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


def test_enabled_defaults_and_crud_cascade(db_session):
    community = CRUDOperations.create_community(
        db_session, name='65500:102', values='', description='test', enabled=True
    )
    network = CRUDOperations.create_network(
        db_session,
        prefix='203.0.113.0/24',
        description='test',
        status='pending',
        community_ids=json.dumps([community.id]),
        enabled=True,
    )
    assert community.enabled is True
    assert network.enabled is True
    assert db_session.query(Network).count() == 1
    assert db_session.query(Community).count() == 1

    network.enabled = False
    db_session.commit()
    assert db_session.query(Network).one().enabled is False
    CRUDOperations.delete_community(db_session, community.id)
    assert db_session.query(Community).count() == 0
    assert db_session.query(Network).count() == 0


def test_default_community_migrates_empty_networks_and_merges_duplicates(db_session):
    canonical = CRUDOperations.create_community(
        db_session, name='65534', description='', enabled=False
    )
    duplicate = CRUDOperations.create_community(
        db_session, name='65534', description='duplicate', enabled=True
    )
    empty_network = Network(
        prefix='192.0.2.0/24', community_ids=None, enabled=True
    )
    duplicate_network = Network(
        prefix='198.51.100.0/24', community_ids=json.dumps([duplicate.id]), enabled=True
    )
    db_session.add_all([empty_network, duplicate_network])
    db_session.commit()

    result = CRUDOperations.ensure_default_community(db_session)

    assert result.id == canonical.id
    assert result.description == 'default'
    assert result.enabled is False
    assert db_session.query(Community).filter(Community.name == '65534').count() == 1
    assert json.loads(db_session.get(Network, empty_network.id).community_ids) == [canonical.id]
    assert json.loads(db_session.get(Network, duplicate_network.id).community_ids) == [canonical.id]


def test_bulk_empty_community_uses_canonical_default(db_session):
    result = CRUDOperations.bulk_commit_networks(
        db_session,
        [{'prefix': '203.0.113.0/24', 'community_ids': [], 'enabled': True}],
    )

    assert result['created'] == 1
    community = db_session.query(Community).filter(Community.name == '65534').one()
    network = db_session.query(Network).one()
    assert community.description == 'default'
    assert community.values == '65534:0'
    assert community.enabled is True
    assert json.loads(network.community_ids) == [community.id]


def test_migration_adds_enabled_to_legacy_sqlite(tmp_path):
    path = tmp_path / 'legacy.db'
    connection = sqlite3.connect(path)
    connection.executescript(
        'CREATE TABLE networks (id INTEGER PRIMARY KEY, prefix TEXT, '
        'description TEXT, status TEXT, community_ids INTEGER);'
        'CREATE TABLE communities (id INTEGER PRIMARY KEY, name TEXT, '
        '"values" TEXT, description TEXT);'
        'INSERT INTO networks (prefix) VALUES (\'203.0.113.0/24\');'
        'INSERT INTO communities (name) VALUES (\'65500:102\');'
    )
    connection.commit()
    connection.close()

    engine = create_engine(f'sqlite:///{path}')
    ensure_schema(engine)
    with engine.connect() as migrated:
        network_columns = {
            row[1] for row in migrated.execute(text('PRAGMA table_info(networks)'))
        }
        community_columns = {
            row[1] for row in migrated.execute(text('PRAGMA table_info(communities)'))
        }
        assert 'enabled' in network_columns
        assert 'enabled' in community_columns
        assert migrated.execute(text('SELECT enabled FROM networks')).scalar() == 1
        assert migrated.execute(text('SELECT enabled FROM communities')).scalar() == 1


LEGACY_BGP_PEERS_DDL = (
    'CREATE TABLE bgp_peers (\n'
    '\tid INTEGER NOT NULL,\n'
    '\tname VARCHAR(255) NOT NULL,\n'
    '\tlocal_as VARCHAR(20) NOT NULL,\n'
    '\tneighbor VARCHAR(45) NOT NULL,\n'
    '\tneighbor_as VARCHAR(20) NOT NULL,\n'
    '\tstatus VARCHAR(50),\n'
    '\tPRIMARY KEY (id),\n'
    '\tUNIQUE (name)\n'
    ');'
)


def _legacy_bgp_peers_db(path, rows):
    connection = sqlite3.connect(path)
    connection.executescript(LEGACY_BGP_PEERS_DDL)
    connection.executemany(
        'INSERT INTO bgp_peers (id, name, local_as, neighbor, neighbor_as, status) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        rows,
    )
    connection.commit()
    connection.close()


def _neighbor_indexes(path):
    connection = sqlite3.connect(path)
    try:
        return {
            row[1]
            for row in connection.execute("PRAGMA index_list('bgp_peers')")
            if row[2]
        }
    finally:
        connection.close()


def test_ensure_schema_adds_unique_neighbor_to_legacy_table(tmp_path):
    """Installing over an older release adds UNIQUE(neighbor) in place.

    create_all never alters a table that already exists, so the constraint
    would otherwise only appear on a fresh database and every upgraded install
    would keep accepting duplicates at the database level.
    """
    path = tmp_path / 'legacy_peers.db'
    _legacy_bgp_peers_db(path, [
        (1, 'mkt', '65500', '192.168.0.1', '64999', 'active'),
        (2, 'second', '65500', '192.168.0.2', '65000', 'disabled'),
    ])

    engine = create_engine(f'sqlite:///{path}')
    ensure_schema(engine)

    assert _neighbor_indexes(path) == {
        'sqlite_autoindex_bgp_peers_1', 'sqlite_autoindex_bgp_peers_2',
    }
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            'SELECT id, name, local_as, neighbor, neighbor_as, status '
            'FROM bgp_peers ORDER BY id'
        ).fetchall() == [
            (1, 'mkt', '65500', '192.168.0.1', '64999', 'active'),
            (2, 'second', '65500', '192.168.0.2', '65000', 'disabled'),
        ]
    finally:
        connection.close()


def test_ensure_schema_keeps_duplicated_neighbors(tmp_path, capsys):
    """Duplicates are reported and left alone — no rows are ever dropped."""
    path = tmp_path / 'dup_peers.db'
    _legacy_bgp_peers_db(path, [
        (1, 'first', '65500', '192.168.0.5', '65001', 'active'),
        (2, 'second', '65500', '192.168.0.5', '65002', 'active'),
    ])

    ensure_schema(create_engine(f'sqlite:///{path}'))

    assert '192.168.0.5' in capsys.readouterr().out
    assert len(_neighbor_indexes(path)) == 1
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            'SELECT id, name FROM bgp_peers ORDER BY id'
        ).fetchall() == [(1, 'first'), (2, 'second')]
    finally:
        connection.close()


def test_ensure_schema_is_idempotent_for_bgp_peers(tmp_path, capsys):
    """A second run finds the constraint and does not touch the table."""
    path = tmp_path / 'idem_peers.db'
    _legacy_bgp_peers_db(path, [(1, 'mkt', '65500', '192.168.0.1', '64999', 'active')])
    engine = create_engine(f'sqlite:///{path}')

    ensure_schema(engine)
    capsys.readouterr()
    ensure_schema(engine)

    assert capsys.readouterr().out == ''
    assert _neighbor_indexes(path) == {
        'sqlite_autoindex_bgp_peers_1', 'sqlite_autoindex_bgp_peers_2',
    }


def test_split_bundle_publishes_all_generated_files(tmp_path, monkeypatch):
    live = tmp_path / 'live'
    staged = tmp_path / 'staged'
    live.mkdir()
    staged.mkdir()
    main = tmp_path / 'bird.conf'
    main.write_text(render_managed_main_block(live) + '\n')
    monkeypatch.setenv('BIRD_MAIN_CONFIG', str(main))
    monkeypatch.setenv('BIRD_BIN_PATH', '/usr/sbin/bird')
    monkeypatch.setenv('BIRDC_BIN_PATH', '/bin/true')

    generate_split(
        [{'prefix': '203.0.113.0/24', 'community_ids': [7], 'enabled': True}],
        [{'id': 7, 'name': '65500:102', 'values': '', 'enabled': True}],
        staged_dir=str(staged), managed_include_dir=str(live), router_id='203.0.113.254', local_as='64512',
    )
    main_before = main.read_text()
    manager = ConfigManager(str(live), str(staged), lambda: None)
    result = manager.publish_staged_generated()
    assert result['status'] == 'success'
    assert (live / 'export.conf').exists()
    assert not (live / 'policy_7.conf').exists()
    assert (live / 'routes.conf').exists()
    assert (live / 'core.conf').exists()
    assert (live / 'bird-manager.conf').exists()
    entrypoint = (live / 'bird-manager.conf').read_text()
    assert entrypoint.index('networks.conf') < entrypoint.index('routes_65500_102.conf')
    assert entrypoint.index('routes_65500_102.conf') < entrypoint.index('core.conf')
    # This fixture has no BGP peers, so no peer owns a filter and no channel
    # carries an export line at all; see the BIRD-channel tests.
    assert 'filter export' not in (live / 'export.conf').read_text()
    assert main.read_text() == main_before
    assert DiffEngine(str(live), str(staged))._all_filenames() == {
        'networks.conf', 'routes.conf', 'routes_65500_102.conf',
        'core.conf', 'export.conf',
        'bird-manager.conf',
    }

    overview = manager.get_config_overview()
    assert overview['networks']['editable'] is True
    assert 'bird_conf' not in overview
    assert overview['managed_conf']['path'] == str(live / 'bird-manager.conf')
    assert overview['managed_conf']['content'] == entrypoint
    assert overview['core']['editable'] is False
    assert overview['core']['content']
    assert overview['managed_conf']['adds'] == 0
    assert overview['managed_conf']['dels'] == 0
    assert overview['core']['adds'] == 0
    assert overview['core']['dels'] == 0
    assert {row['filename'] for row in overview['summary']} == {
        'routes.conf', 'export.conf',
        'routes_65500_102.conf',
    }
    assert all(row['adds'] == 0 and row['dels'] == 0 for row in overview['summary'])
    assert all('content' not in row for row in overview['summary'])

    with open(staged / 'export.conf', 'a', encoding='utf-8') as stream:
        stream.write('\n# latest published change\n')
    second_result = manager.publish_staged_generated()
    assert second_result['status'] == 'success'
    second_overview = manager.get_config_overview()
    export_row = next(row for row in second_overview['summary'] if row['filename'] == 'export.conf')
    assert export_row['adds'] == 0
    assert export_row['dels'] == 0


def test_disabled_records_are_not_emitted(tmp_path):
    staged = tmp_path / 'staged'
    generate_split(
        [
            {'prefix': '203.0.113.0/24', 'community_ids': [7], 'enabled': False},
            {'prefix': '198.51.100.0/24', 'community_ids': [7], 'enabled': True},
        ],
        [{'id': 7, 'name': '65500:102', 'values': '', 'enabled': True}],
        staged_dir=str(staged), managed_include_dir=str(tmp_path / 'live'), router_id='203.0.113.254', local_as='64512',
    )
    assert '203.0.113.0/24' not in (staged / 'routes.conf').read_text()
    assert '198.51.100.0/24' in (staged / 'routes.conf').read_text()
    assert '203.0.113.0/24' not in (staged / 'export.conf').read_text()


def test_backup_roundtrip_synthesizes_legacy_entrypoint_and_preserves_main(tmp_path, monkeypatch):
    live = tmp_path / 'live'
    staged = tmp_path / 'staged'
    live.mkdir()
    staged.mkdir()
    db_path = tmp_path / 'app.db'
    db_path.write_text('sqlite fixture')
    manifest = tmp_path / 'managed-files.json'
    manifest.write_text(json.dumps({'files': ['policy_7.conf', 'routes.conf', 'networks.conf']}))
    main = tmp_path / 'bird.conf'
    main.write_text(render_managed_main_block(live) + '\n')
    for directory in (live, staged):
        (directory / 'networks.conf').write_text('base')
        (directory / 'routes.conf').write_text('routes')
        (directory / 'core.conf').write_text('core')
        (directory / 'policy_7.conf').write_text('policy')
    monkeypatch.setenv('BIRD_BACKUP_DIR', str(tmp_path / 'backup'))
    monkeypatch.setenv('BIRD_MANIFEST_PATH', str(manifest))
    monkeypatch.setenv('BIRD_MAIN_CONFIG', str(main))
    monkeypatch.setenv('BIRD_BIN_PATH', str(tmp_path / 'bird-missing'))
    main_before = main.read_text()
    live_inode = live.stat().st_ino
    live_mode = live.stat().st_mode

    create_backup(str(live), str(staged), str(db_path))
    (live / 'policy_7.conf').unlink()
    result = restore_backup(str(live), str(staged), str(db_path))

    assert result['status'] == 'success'
    assert (live / 'policy_7.conf').read_text() == 'policy'
    assert live.stat().st_ino == live_inode
    assert live.stat().st_mode == live_mode
    assert 'policy_7.conf' in (live / 'bird-manager.conf').read_text()
    assert main.read_text() == main_before
    assert 'bird-manager.conf' in json.loads(manifest.read_text())['files']


def test_backup_paths_recompute_when_env_changes(tmp_path, monkeypatch):
    """The resolver must never serve a stale value.

    It caches per environment signature; if the signature did not cover a key,
    an operator (or a test) changing it mid-process would keep getting the old
    location — exactly the drift between backup.py and ConfigManager.
    """
    from app.backup import backup_paths

    monkeypatch.setenv('BIRD_MAIN_CONFIG', str(tmp_path / 'bird.conf'))
    monkeypatch.setenv('BIRD_BACKUP_DIR', str(tmp_path / 'bak-one'))
    monkeypatch.setenv('BIRD_MANIFEST_PATH', str(tmp_path / 'one.json'))
    assert backup_paths.main_config_path() == str(tmp_path / 'bird.conf')
    assert backup_paths.backup_dir('/data/app.db') == str(tmp_path / 'bak-one')
    assert backup_paths.manifest_path('/etc/bird/live') == str(tmp_path / 'one.json')

    monkeypatch.setenv('BIRD_MAIN_CONFIG', str(tmp_path / 'bird2.conf'))
    monkeypatch.setenv('BIRD_BACKUP_DIR', str(tmp_path / 'bak-two'))
    monkeypatch.setenv('BIRD_MANIFEST_PATH', str(tmp_path / 'two.json'))
    assert backup_paths.main_config_path() == str(tmp_path / 'bird2.conf')
    assert backup_paths.backup_dir('/data/app.db') == str(tmp_path / 'bak-two')
    assert backup_paths.manifest_path('/etc/bird/live') == str(tmp_path / 'two.json')


def test_backup_dir_follows_the_database_file(tmp_path, monkeypatch):
    """Without BIRD_BACKUP_DIR the snapshot sits next to the database.

    db_path is part of the cache key, so a rebound database cannot inherit the
    previous one's snapshot directory.
    """
    from app.backup import backup_paths

    monkeypatch.delenv('BIRD_BACKUP_DIR', raising=False)
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    assert backup_paths.backup_dir(str(first / 'app.db')) == str(first / 'backup')
    assert backup_paths.backup_dir(str(second / 'app.db')) == str(second / 'backup')
    # And going back to the first must not return the second's answer.
    assert backup_paths.backup_dir(str(first / 'app.db')) == str(first / 'backup')


def test_manifest_path_follows_the_live_directory_it_is_given(tmp_path, monkeypatch):
    """A manager built on a non-default tree keeps its manifest in that tree.

    The resolver used to read BIRD_LIVE_DIR itself, so such a manager reported
    /var/lib/bird-manager/managed-files.json and would have written there.
    """
    from app.backup import backup_paths

    monkeypatch.delenv('BIRD_MANIFEST_PATH', raising=False)
    plain = ConfigManager(str(tmp_path / 'live'), str(tmp_path / 'staged'), lambda: None)
    assert plain.manifest_path == str(tmp_path / 'managed-files.json')

    monkeypatch.setenv('BIRD_MANIFEST_PATH', str(tmp_path / 'explicit.json'))
    overridden = ConfigManager(str(tmp_path / 'live'), str(tmp_path / 'staged'), lambda: None)
    assert overridden.manifest_path == str(tmp_path / 'explicit.json')


def test_manifest_path_distinguishes_two_live_trees(tmp_path, monkeypatch):
    """live_path is part of the cache key, not just an input read once.

    Two managers on different trees in the same environment must not share one
    answer, and going back to the first must not return the second's.
    """
    from app.backup import backup_paths

    monkeypatch.delenv('BIRD_MANIFEST_PATH', raising=False)
    first = backup_paths.manifest_path(str(tmp_path / 'one' / 'live'))
    second = backup_paths.manifest_path(str(tmp_path / 'two' / 'live'))
    assert first == str(tmp_path / 'one' / 'managed-files.json')
    assert second == str(tmp_path / 'two' / 'managed-files.json')
    assert first != second
    assert backup_paths.manifest_path(str(tmp_path / 'one' / 'live')) == first


def test_config_manager_and_backup_share_one_path_rule(tmp_path, monkeypatch):
    """The duplicate rules must not come back.

    ConfigManager used to re-implement BIRD_MANIFEST_PATH and BIRD_MAIN_CONFIG on
    its own, which is how the two modules came to disagree about the same
    installation.
    """
    from app.backup import backup_paths

    monkeypatch.delenv('BIRD_MANIFEST_PATH', raising=False)
    monkeypatch.setenv('BIRD_MAIN_CONFIG', str(tmp_path / 'bird.conf'))
    manager = ConfigManager(str(tmp_path / 'live'), str(tmp_path / 'staged'), lambda: None)
    assert manager.manifest_path == backup_paths.manifest_path(str(tmp_path / 'live'))
    assert manager._main_config_path() == backup_paths.main_config_path()


def test_backup_retention_falls_back_and_limits_archives(tmp_path, monkeypatch):
    from app.backup import backup_retention

    monkeypatch.setenv('BIRD_BACKUP_RETENTION', '100')
    assert backup_retention() == 5
    monkeypatch.setenv('BIRD_BACKUP_RETENTION', 'invalid')
    assert backup_retention() == 5
    monkeypatch.setenv('BIRD_BACKUP_RETENTION', '2')
    assert backup_retention() == 2
    live = tmp_path / 'live'
    staged = tmp_path / 'staged'
    backup_dir = tmp_path / 'rotating-backup'
    live.mkdir()
    staged.mkdir()
    db_path = tmp_path / 'rotation.db'
    db_path.write_text('fixture')
    monkeypatch.setenv('BIRD_BACKUP_DIR', str(backup_dir))
    for _ in range(3):
        create_backup(str(live), str(staged), str(db_path))
    assert len(list(backup_dir.glob('*.tar.gz'))) == 2


def test_backup_list_slots_delete_and_strict_ids(tmp_path, monkeypatch):
    live = tmp_path / 'live'
    staged = tmp_path / 'staged'
    live.mkdir()
    staged.mkdir()
    db_path = tmp_path / 'app.db'
    db_path.write_text('fixture')
    backup_dir = tmp_path / 'backups'
    monkeypatch.setenv('BIRD_BACKUP_DIR', str(backup_dir))
    monkeypatch.setenv('BIRD_BACKUP_RETENTION', '3')

    for _ in range(3):
        create_backup(str(live), str(staged), str(db_path))
    info = backup_info(str(db_path))

    assert info['used'] == 3
    assert info['free'] == 0
    assert info['slots'] == '3/3'
    assert len(info['items']) == 3
    assert info['items'][0]['id'] == 'latest'
    assert info['items'][0]['latest'] is True
    assert all(item['display_time'] for item in info['items'])

    deleted = delete_backup('latest', str(db_path))
    assert deleted['status'] == 'success'
    assert deleted['backup']['used'] == 2
    assert deleted['backup']['free'] == 1
    assert deleted['backup']['slots'] == '2/3'
    assert not (backup_dir / 'bird-manager-snapshot.tar.gz').exists()

    with pytest.raises(ValueError):
        delete_backup('../etc/bird/bird.conf', str(db_path))
    missing = delete_backup('20990101-000000-000000', str(db_path))
    assert missing['status'] == 'error'


def test_restore_selected_backup_archive(tmp_path, monkeypatch):
    live = tmp_path / 'live'
    staged = tmp_path / 'staged'
    live.mkdir()
    staged.mkdir()
    db_path = tmp_path / 'app.db'
    db_path.write_text('fixture')
    main = tmp_path / 'bird.conf'
    main.write_text(render_managed_main_block(live) + '\n')
    monkeypatch.setenv('BIRD_BACKUP_DIR', str(tmp_path / 'backups'))
    monkeypatch.setenv('BIRD_MANIFEST_PATH', str(tmp_path / 'managed-files.json'))
    monkeypatch.setenv('BIRD_MAIN_CONFIG', str(main))
    monkeypatch.setenv('BIRD_BIN_PATH', str(tmp_path / 'bird-missing'))

    (live / 'networks.conf').write_text('one\n')
    (staged / 'networks.conf').write_text('one\n')
    create_backup(str(live), str(staged), str(db_path))
    (live / 'networks.conf').write_text('two\n')
    (staged / 'networks.conf').write_text('two\n')
    create_backup(str(live), str(staged), str(db_path))

    info = backup_info(str(db_path))
    selected = next(item for item in info['items'] if item['id'] != 'latest')
    (live / 'networks.conf').write_text('broken\n')
    (staged / 'networks.conf').write_text('broken\n')

    result = restore_backup(str(live), str(staged), str(db_path), backup_id=selected['id'])

    assert result['status'] == 'success'
    assert (live / 'networks.conf').read_text() == 'one\n'


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, *_args):
        return


def _configure_app_env(tmp_path, monkeypatch):
    """Point every path/env knob at an isolated temporary tree."""
    live = tmp_path / 'live'
    staged = tmp_path / 'staged'
    live.mkdir()
    staged.mkdir()
    main = tmp_path / 'bird.conf'
    main.write_text(render_managed_main_block(live) + '\n')
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'app.db'))
    monkeypatch.setenv('BIRD_LIVE_DIR', str(live))
    monkeypatch.setenv('BIRD_STAGED_DIR', str(staged))
    monkeypatch.setenv('BIRD_MAIN_CONFIG', str(main))
    monkeypatch.setenv('BIRD_BIN_PATH', '/usr/sbin/bird')
    monkeypatch.setenv('BIRDC_BIN_PATH', '/bin/true')
    monkeypatch.setenv('BIRD_BACKUP_DIR', str(tmp_path / 'backups'))
    monkeypatch.setenv('BIRD_BACKUP_RETENTION', '3')
    monkeypatch.setenv('BIRD_MANIFEST_PATH', str(tmp_path / 'managed-files.json'))
    monkeypatch.setenv('BIRD_MANAGER_ENV_FILE', str(tmp_path / 'missing-runtime.env'))
    for name in (
        'BIRD_TRUSTED_SOURCES', 'BIRD_TRUSTED_PROXIES', 'BIRD_TRUSTED_ORIGINS',
        'BIRD_MAX_BODY_BYTES', 'BIRD_DEBUG_ERRORS', 'APP_HOST', 'USERNAME',
    ):
        monkeypatch.delenv(name, raising=False)
    return live, staged, main


def _bind_test_tree(tmp_path, monkeypatch):
    """Point every path the application caches at import at this test's tree.

    ``routes.config_manager``, ``routes.diff_engine`` and the engine/DB path pair
    are module state frozen at first import, and the database engine plus its
    SQLite file are shared by every test in the process. Without this a test
    reads another test's directories, rows leak into tests that count them, and
    restore_backup() replaces a database file the test is not even using.

    The order is deliberate: rebind first, so the truncation below hits the
    engine this test will use, then reseed, so the default Community exists
    again for anything that expects a fresh install.
    """
    live, staged, main = _configure_app_env(tmp_path, monkeypatch)

    import app.routes as routes
    from app import database as database_module
    from app import models as app_models
    from sqlalchemy import text as sql_text

    routes._bind_paths_from_env()
    routes._bind_database_from_env()
    # A rebound DB_PATH is a brand new file, and database.py cannot create the
    # schema itself (it does not import the models). Import does this at module
    # level; a rebind has to repeat it.
    routes.ensure_schema(routes.engine)

    # Metadata lives in app.models; app.database keeps a Base of its own for
    # older SQLAlchemy versions and it holds none of the tables.
    session = database_module.get_session()
    for table in reversed(app_models.Base.metadata.sorted_tables):
        session.execute(table.delete())
    # Reset the AUTOINCREMENT counters too, so ids do not drift across tests.
    # Nothing asserts a literal id today; this only keeps that cheap later.
    try:
        session.execute(sql_text('DELETE FROM sqlite_sequence'))
    except Exception:
        pass
    session.commit()
    session.close()

    routes._seed_staged()
    return live, staged, main


@pytest.fixture
def http_api(tmp_path, monkeypatch):
    live, staged, main = _bind_test_tree(tmp_path, monkeypatch)

    import app.routes as routes

    server = make_server('127.0.0.1', 0, routes.app, handler_class=_QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f'http://127.0.0.1:{server.server_port}'

    def request(method, path, payload=None):
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload).encode()
            headers['Content-Type'] = 'application/json'
        headers['Content-Length'] = str(len(body or b''))
        req = urllib.request.Request(base_url + path, data=body, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read())

    yield request, routes, live, staged, main
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def test_http_api_crud_reconciles_split_files(http_api, monkeypatch):
    request, _routes, live, staged, main = http_api
    monkeypatch.setenv('USERNAME', 'Integration User')
    assert _routes._audit_user() == 'Integration User'
    monkeypatch.setenv('USERNAME', '   ')
    assert _routes._audit_user() == 'User'
    main_before = main.read_text()
    status, health = request('GET', '/api/health')
    assert status == 200
    assert health['status'] == 'healthy'
    status, runtime_status = request('GET', '/api/config/status?filename=networks.conf')
    assert status == 200
    assert 'bird_last_reboot' in runtime_status
    assert 'bird_last_reconfiguration' in runtime_status

    status, community_response = request(
        'POST', '/api/communities', {'name': '65500:102', 'enabled': True}
    )
    assert status == 201
    assert community_response['community']['enabled'] is False
    community_id = community_response['community']['id']

    status, network_response = request(
        'POST', '/api/networks', {'prefix': '203.0.113.0/24', 'enabled': True}
    )
    assert status == 201
    network_id = network_response['network']['id']
    request(
        'PUT', f'/api/networks/{network_id}',
        {'community_ids': json.dumps([community_id]), 'enabled': True},
    )
    # A peer on AS 65500 owns the only export filter, so its own ASN decides
    # what it receives: export_all defaults off.
    status, peer_response = request('POST', '/api/bgp_peers', {
        'name': 'peer_lan', 'local_as': '65500',
        'neighbor': '192.168.30.1', 'neighbor_as': '64991',
    })
    assert status == 201
    assert peer_response['bgp_peer']['export_all'] is False
    status, peers = request('GET', '/api/bgp_peers')
    assert peers['bgp_peers'][0]['export_count'] == 0
    status, _ = request('PUT', f'/api/communities/{community_id}', {'enabled': True})
    assert status == 200
    # The export filter for this Community lives in export.conf now; the
    # per-Community policy files are gone and nothing includes them.
    assert (staged / 'export.conf').exists()
    assert not list(staged.glob('policy_*.conf'))
    assert not (live / 'export.conf').exists()
    assert 'export_peer_peer_lan.conf' in (staged / 'bird-manager.conf').read_text()
    assert 'export_peer_peer_lan.conf' not in main.read_text()
    assert 'bgp_community.add((65500, 102));' in (staged / 'export.conf').read_text()
    assert 'export filter export_to_peer_lan;' in (staged / 'core.conf').read_text()
    status, peers = request('GET', '/api/bgp_peers')
    assert peers['bgp_peers'][0]['export_count'] == 1

    status, overview = request('GET', '/api/config/overview')
    assert status == 200
    assert overview['networks']['editable'] is True
    assert 'bird_conf' not in overview
    assert overview['managed_conf']['editable'] is False
    # Exact counts, not "> 0": a file that is new in staged counts every one of
    # its lines as added. A "> 0" check passed even while the diff body was
    # glued into one line and only the first line was ever counted.
    assert overview['managed_conf']['adds'] == len(
        (staged / 'bird-manager.conf').read_text().splitlines()
    )
    assert overview['managed_conf']['dels'] == 0
    assert overview['core']['editable'] is False
    export_row = next(
        row for row in overview['summary'] if row['filename'] == 'export.conf'
    )
    assert export_row['adds'] == len(
        (staged / 'export.conf').read_text().splitlines()
    )
    assert export_row['dels'] == 0
    assert all('content' not in row for row in overview['summary'])

    status, staged_response = request('GET', '/api/config/staged?filename=networks.conf')
    assert status == 200
    status, preview = request('POST', '/api/config/preview', {
        'filename': 'networks.conf',
        'content': staged_response['content'],
    })
    assert status == 200
    assert preview['syntax']['valid'] is True, preview['syntax']

    status, syntax = request('GET', '/api/config/syntax-check?filename=networks.conf')
    assert status == 200
    assert syntax['valid'] is True, syntax
    status, applied = request('POST', '/api/config/apply', {
        'filename': 'networks.conf',
        'content': staged_response['content'],
    })
    assert status == 200
    assert applied['status'] == 'success', applied
    assert (live / 'export.conf').exists()
    assert not list(live.glob('policy_*.conf'))
    assert 'export_peer_peer_lan.conf' in (live / 'bird-manager.conf').read_text()
    assert 'bgp_community.add((65500, 102));' in (live / 'export.conf').read_text()
    assert main.read_text() == main_before
    assert [name for name in os.listdir(live) if not name.endswith('.conf')] == []

    status, overview = request('GET', '/api/config/overview')
    assert status == 200
    assert overview['managed_conf']['adds'] == 0
    assert overview['managed_conf']['dels'] == 0
    assert all(row['adds'] == 0 and row['dels'] == 0 for row in overview['summary'])

    status, _ = request('DELETE', f'/api/communities/{community_id}')
    assert status == 200
    assert (live / 'export.conf').exists()
    assert 'export_peer_peer_lan.conf' in (live / 'bird-manager.conf').read_text()
    assert main.read_text() == main_before
    status, applied = request('POST', '/api/config/apply', {
        'filename': 'networks.conf',
    })
    assert status == 200
    assert applied['status'] == 'success', applied
    # Deleting the Community drops its pairs from the peer's filter.
    assert '65500, 102' not in (live / 'export.conf').read_text()
    assert main.read_text() == main_before
    assert [name for name in os.listdir(live) if not name.endswith('.conf')] == []
    snapshot_dir = live.parent / 'live-bak'
    assert (snapshot_dir / 'networks.conf.bak').is_file()
    status, after_apply = request('GET', '/api/config/status?filename=networks.conf')
    assert after_apply['has_backup'] is True

    (live / 'networks.conf').write_text('router id 192.0.2.1;\n')
    (live / 'routes.conf').write_text(
        'protocol static saved_routes { ipv4; route 192.0.2.0/24 via "lo"; }\n'
    )
    (live / 'core.conf').write_text(
        'protocol device {}\nprotocol direct { ipv4; interface "lo"; }\n'
    )
    (staged / 'networks.conf').write_text('router id 192.0.2.1;\n')

    # Two Communities bracketing the snapshot: '64512:1' predates it and must
    # come back, '64512:2' is created afterwards and must not. Both are real
    # community references — the names are only labels for this test.
    status, _ = request('POST', '/api/communities', {'name': '64512:1'})
    assert status == 201
    status, first = request('POST', '/api/backup', {'user_id': 'backup-test'})
    assert status == 200
    assert first['backup']['slots'] == '1/3'
    (live / 'networks.conf').write_text('router id 192.0.2.2;\n')
    status, _ = request('POST', '/api/communities', {'name': '64512:2'})
    assert status == 201
    status, second = request('POST', '/api/backup', {'user_id': 'backup-test'})
    assert status == 200
    assert second['backup']['slots'] == '2/3'

    status, current = request('GET', '/api/config/status?filename=networks.conf')
    assert status == 200
    items = current['backup']['items']
    assert len(items) == 2
    selected = next(item for item in items if item['id'] != 'latest')

    status, restored = request(
        'POST', f"/api/backup/{selected['id']}/restore", {'user_id': 'backup-test'}
    )
    assert status == 200
    assert restored['status'] == 'success'
    assert (live / 'networks.conf').read_text() == 'router id 192.0.2.1;\n'
    # The database half of the restore. Without these assertions the test passed
    # even when restore_backup replaced a database file belonging to another
    # test: it only ever looked at networks.conf and the slot count.
    status, listing = request('GET', '/api/communities')
    assert status == 200
    community_names = [c['name'] for c in listing['communities']]
    assert '64512:1' in community_names, (
        'restore dropped a Community that predates the snapshot'
    )
    assert '64512:2' not in community_names, (
        'restore kept a Community created after the snapshot, so it did not '
        'target the database this test is using'
    )

    status, deleted = request(
        'DELETE', f"/api/backup/{selected['id']}", {'user_id': 'backup-test'}
    )
    assert status == 200
    assert deleted['backup']['slots'] == '1/3'

    # No teardown needed: the fixture empties the shared engine's tables before
    # the next test.


def test_bgp_peer_post_reports_export_scope(http_api):
    """The POST response must carry the same scope fields the list returns.

    The add form warns straight from the POST response, so a missing
    ``asn_found`` there silently swallowed the "peer will get nothing" warning
    at exactly the moment the operator could still act on it.
    """
    request, _routes, _live, _staged, _main = http_api
    status, community_response = request('POST', '/api/communities', {'name': '65500:7'})
    assert status == 201
    community_id = community_response['community']['id']
    status, network_response = request('POST', '/api/networks', {'prefix': '203.0.113.0/24'})
    assert status == 201
    request(
        'PUT', f"/api/networks/{network_response['network']['id']}",
        {'community_ids': json.dumps([community_id]), 'enabled': True},
    )
    status, _ = request('PUT', f'/api/communities/{community_id}', {'enabled': True})
    assert status == 200

    status, matched = request('POST', '/api/bgp_peers', {
        'name': 'matching', 'local_as': '65500',
        'neighbor': '192.168.40.1', 'neighbor_as': '64992',
    })
    assert status == 201
    assert matched['bgp_peer']['export_all'] is False
    assert matched['bgp_peer']['asn_found'] is True
    assert matched['bgp_peer']['export_count'] == 1

    status, ghost = request('POST', '/api/bgp_peers', {
        'name': 'ghost', 'local_as': '64997',
        'neighbor': '192.168.40.2', 'neighbor_as': '64993',
    })
    assert status == 201
    assert ghost['bgp_peer']['asn_found'] is False
    assert ghost['bgp_peer']['export_count'] == 0

    status, listed = request('GET', '/api/bgp_peers')
    assert status == 200
    by_name = {peer['name']: peer for peer in listed['bgp_peers']}
    assert by_name['matching']['asn_found'] is True
    assert by_name['ghost']['asn_found'] is False

    # export_all switches the peer to the combined filter, so the missing ASN
    # stops being a problem.
    status, everything = request('POST', '/api/bgp_peers', {
        'name': 'everything', 'local_as': '64996', 'export_all': True,
        'neighbor': '192.168.40.3', 'neighbor_as': '64994',
    })
    assert status == 201
    assert everything['bgp_peer']['export_all'] is True

    # The three peers above, the network and the Community stay in the
    # database: the fixture empties the shared engine's tables before the next
    # test, so no test has to unwind its own writes.


def _assert_tree_is_own(routes, database_module, tmp_path):
    """Every application path must resolve inside *tmp_path*.

    routes.config_manager/diff_engine and the engine/DB pair are frozen at import.
    When they drift the failure is silent rather than loud: restore_backup()
    os.replace()s the file at _DB_PATH, so a restore aimed at a foreign database
    destroys that database while restoring nothing into the one in use, and the
    test still passes because it only looks at networks.conf and the slot count.
    """
    assert pathlib.Path(routes._LIVE_PATH).is_relative_to(tmp_path)
    assert pathlib.Path(routes._STAGED_PATH).is_relative_to(tmp_path)
    assert pathlib.Path(routes._DB_PATH).is_relative_to(tmp_path)
    assert pathlib.Path(routes.config_manager.live_path).is_relative_to(tmp_path)
    assert pathlib.Path(routes.config_manager.staged_path).is_relative_to(tmp_path)
    assert pathlib.Path(routes.config_manager.publish_bak_dir).is_relative_to(tmp_path)
    assert pathlib.Path(routes.diff_engine.live_path).is_relative_to(tmp_path)
    assert pathlib.Path(routes.diff_engine.staged_path).is_relative_to(tmp_path)
    # apply_state.json is derived from staged_path, so it follows.
    assert pathlib.Path(routes.config_manager._apply_state_path()).is_relative_to(tmp_path)
    # The engine and the path the backup endpoints use must be the same file:
    # restore disposes the engine and replaces the path.
    assert database_module._DB_PATH == routes._DB_PATH
    assert pathlib.Path(routes.engine.url.database) == pathlib.Path(routes._DB_PATH)


def test_http_api_tree_is_bound_to_its_own_dirs(http_api, tmp_path):
    from app import database as database_module
    import app.routes as routes

    _assert_tree_is_own(routes, database_module, tmp_path)


def test_access_api_tree_is_bound_to_its_own_dirs(access_api, tmp_path):
    from app import database as database_module

    _assert_tree_is_own(access_api, database_module, tmp_path)


def test_each_test_starts_with_an_empty_database(http_api):
    """Rows must not leak between tests through the shared engine.

    Without the fixture truncation a row created by one test is still there for
    the next one, and the peer pre-insert checks (_asn_already_in_use /
    _neighbor_already_in_use) then reject neighbours this test never created.
    """
    request = http_api[0]
    status, payload = request('GET', '/api/networks')
    assert status == 200
    assert payload['count'] == 0
    status, payload = request('GET', '/api/bgp_peers')
    assert status == 200
    assert payload['count'] == 0


def test_timestamps_use_machine_local_time(http_api):
    from datetime import datetime, timezone

    from app.utils import local_now

    request, _routes, _live, _staged, _main = http_api
    wall_clock = datetime.now(timezone.utc).astimezone().replace(tzinfo=None)
    assert abs((local_now() - wall_clock).total_seconds()) < 5

    status, created = request('POST', '/api/backup', {'user_id': 'User'})
    assert status == 200
    assert abs(
        (
            datetime.fromisoformat(created['backup']['items'][0]['created_at'])
            - wall_clock
        ).total_seconds()
    ) < 60

    session = _routes.get_session()
    try:
        entry = (
            session.query(_routes.AuditLog)
            .order_by(_routes.AuditLog.id.desc())
            .first()
        )
        assert entry is not None
        assert entry.user_id == 'User'
        assert abs((entry.timestamp - wall_clock).total_seconds()) < 60
    finally:
        session.close()


def test_dashboard_uses_readonly_managed_entrypoint():
    with open('templates/dashboard.html', 'r', encoding='utf-8') as stream:
        dashboard = stream.read()
    with open('static/js/app.js', 'r', encoding='utf-8') as stream:
        script = stream.read()

    assert '/etc/bird/bird.conf' not in dashboard
    assert '/etc/bird/live/bird-manager.conf' in dashboard
    assert 'id="manager-section-count"' in dashboard
    # The managed entrypoint is shown as a live|staged line comparison, so the
    # pane holds the server's row model rather than a file and a raw diff.
    assert 'id="manager-conf-rows" class="row-view row-view-scroll"' in dashboard
    assert 'data.managed_conf' in script
    assert 'data.bird_conf' not in script


def test_dashboard_uses_three_classic_left_aligned_tabs():
    with open('templates/dashboard.html', 'r', encoding='utf-8') as stream:
        dashboard = stream.read()
    with open('static/css/app.css', 'r', encoding='utf-8') as stream:
        css = stream.read()
    with open('static/js/app.js', 'r', encoding='utf-8') as stream:
        script = stream.read()
    with open('app/routes.py', 'r', encoding='utf-8') as stream:
        routes_source = stream.read()

    assert dashboard.index('class="app-header"') < dashboard.index('class="tabs"')
    assert dashboard.index('class="tabs"') < dashboard.index('class="tab-strip"') < dashboard.index('class="tab-panels"')
    assert 'tab-shell' not in dashboard
    assert 'tab-content' not in dashboard
    assert 'role="tablist"' in dashboard
    assert 'id="tab-button-networks"' in dashboard
    assert 'id="tab-button-bird"' in dashboard
    assert 'id="tab-button-system"' in dashboard
    assert 'data-tab="networks" role="tab" aria-selected="true"' in dashboard
    assert 'data-tab="bird" role="tab" aria-selected="false"' in dashboard
    assert 'data-tab="system" role="tab" aria-selected="false"' in dashboard
    assert 'data-tab-panel="bird" hidden' in dashboard
    assert 'data-tab-panel="system" hidden' in dashboard
    assert 'dashboard-col' not in dashboard

    networks = dashboard[dashboard.index('data-tab-panel="networks"'):dashboard.index('data-tab-panel="bird"')]
    bird = dashboard[dashboard.index('data-tab-panel="bird"'):dashboard.index('data-tab-panel="system"')]
    system = dashboard[dashboard.index('data-tab-panel="system"'):]
    assert 'id="panel-networks"' in networks and 'id="panel-bulk"' in networks
    assert 'id="panel-config-editor"' not in networks and 'id="panel-audit"' not in networks
    assert 'id="panel-config-editor"' in bird
    # The add-peer form moved to the Networks tab: BGP peers are database-backed
    # and belong beside the other DB-managed lists, next to the peer list.
    assert 'id="panel-bgp"' not in bird
    assert 'id="panel-bgp"' in networks and 'id="panel-peers"' in networks
    assert 'id="panel-config-status"' not in bird and 'id="panel-audit"' not in bird
    assert 'id="panel-config-status"' in system and 'id="panel-backup"' in system
    assert 'id="panel-audit"' in system
    assert system.index('id="panel-config-status"') < system.index('id="panel-backup"') < system.index('id="panel-audit"')
    assert 'id="backup-body"' in system

    assert 'justify-content: flex-start;' in css
    assert css.count('width: 75%;') >= 1
    assert 'background: #475569;' in css
    assert 'background: #ffffff;' in css
    assert '.tabs::after' not in css
    assert '.tabs {' in css
    assert '.tab-strip {' in css
    assert '.tab-panels {' in css
    assert 'overflow: hidden;' in css
    assert 'border: 1px solid #cbd5e1;' in css
    assert 'border-radius: 8px;' in css
    assert 'width: calc(100% + 2px);' in css
    assert 'margin: -1px -1px 0;' in css
    assert 'background: transparent;' in css
    assert 'border-bottom-color: transparent;' in css
    assert 'border-bottom: 1px solid #cbd5e1;' not in css
    assert 'border-bottom: 1px solid #ffffff;' not in css
    assert 'margin-bottom: -1px;' not in css
    assert 'border-top: 1px solid #cbd5e1;' not in css
    assert '#334155' not in css
    assert 'html, body { margin: 0; padding: 0; height: 100%; }' in css
    assert 'height: 100dvh;' in css
    assert 'overflow: hidden;' in css
    assert 'flex: 1 1 auto;' in css
    assert 'margin: 18px auto;' in css
    assert 'min-height: calc(100vh - 120px);' not in css
    assert 'flex: 1 1 0;' in css
    assert 'min-height: 280px;' not in css
    assert 'max-height: 40vh;' in css
    assert 'overscroll-behavior: contain;' in css
    assert '.panel-backup,' in css
    assert '.backup-list {' in css
    assert '.backup-row {' in css
    assert "const TAB_NAMES = ['networks', 'bird', 'system'];" in script
    assert 'function activateTab(name, updateHash)' in script
    assert 'function wireTabs()' in script
    assert 'id="user-id"' not in dashboard
    assert 'Пользователь:' not in dashboard
    assert 'name="bird-manager-user"' in dashboard
    assert 'content="{{USERNAME_HTML}}"' in dashboard
    assert '<script>window.' not in dashboard
    assert '.user-field' not in css
    assert '.app-header .btn-refresh {' in css
    assert 'margin-left: auto;' in css
    assert "document.getElementById('user-id')" not in script
    assert 'window.BIRD_MANAGER_USER' not in script
    assert "document.querySelector('meta[name=\"bird-manager-user\"]')" in script
    assert "os.getenv('USERNAME')" in routes_source
    assert "or 'User'" in routes_source
    assert "'{{USERNAME_HTML}}'" in routes_source
    assert 'html.escape(_audit_user(), quote=True)' in routes_source
    assert "return value || 'User';" in script
    assert '<strong>BIRD reload:</strong> ok' not in script
    assert '<strong>BIRD restart:</strong>' in script
    assert '<strong>BIRD reconfiguration:</strong>' in script
    assert "section.closest('#tab-panel-bird')" in script
    assert 'function renderBackupStatus(data)' in script
    assert 'function deleteBackupSnapshot(backupId, displayTime)' in script
    assert "setPanelMessage('backup-msg'" in script
    assert "textContent = t('m095')" in script
    assert "textContent = t('m078')" in script

    env_comment = "Identifier for user's actions in journal and audit. Does not affect permissions/access."
    for filename in ('.env', 'scripts/build-deb.sh', 'packaging/debian/postinst'):
        with open(filename, 'r', encoding='utf-8') as stream:
            content = stream.read()
        assert env_comment in content
        assert 'USERNAME=User' in content
    assert "backup.slots" in script
    assert "textContent = t('m095')" in script
    assert "textContent = t('m078')" in script


def _removal_fixture(tmp_path):
    root = tmp_path / 'removal'
    live = root / 'live'
    state = root / 'state'
    etc = root / 'etc'
    app = root / 'app'
    for directory in (live, state, etc, app):
        directory.mkdir(parents=True)
    (state / 'app.db').write_text('database', encoding='utf-8')
    (state / '.system-user-created').write_text('', encoding='utf-8')
    (etc / '.env').write_text('BIRD_LIVE_DIR=/unused\n', encoding='utf-8')
    (app / 'routes.py').write_text('', encoding='utf-8')
    (live / 'networks.conf').write_text('router id 192.0.2.1;\n', encoding='utf-8')
    (live / 'policy_1.conf').write_text('# policy 1\n', encoding='utf-8')
    (live / 'routes.conf').write_text('protocol static test_routes { ipv4; }\n', encoding='utf-8')
    (live / 'core.conf').write_text('protocol device {}\n', encoding='utf-8')
    (live / 'operator-note.txt').write_text('preserve me\n', encoding='utf-8')
    (live / 'bird-manager.conf').write_text(
        '# BEGIN bird-manager managed includes\n'
        f'include "{live / "networks.conf"}";\n'
        f'include "{live / "policy_1.conf"}";\n'
        f'include "{live / "routes.conf"}";\n'
        f'include "{live / "core.conf"}";\n'
        '# END bird-manager managed includes\n',
        encoding='utf-8',
    )
    main = root / 'bird.conf'
    main.write_text(
        '# operator header\n'
        '# BEGIN bird-manager managed includes\n'
        f'include "{live / "bird-manager.conf"}";\n'
        '# END bird-manager managed includes\n'
        '# operator footer\n',
        encoding='utf-8',
    )
    bird = root / 'bird'
    bird.write_text('#!/bin/sh\nexit "${FAKE_BIRD_EXIT:-0}"\n', encoding='utf-8')
    bird.chmod(0o755)
    birdc = root / 'birdc'
    birdc.write_text(
        '#!/bin/sh\n'
        'printf "%s\\n" "$*" >> "$FAKE_BIRDC_ARGS"\n'
        'case "$*" in\n'
        '  *show\\ status*) exit 0 ;;\n'
        '  *configure*) exit "${FAKE_BIRDC_CONFIGURE_EXIT:-0}" ;;\n'
        'esac\n'
        'exit 1\n',
        encoding='utf-8',
    )
    birdc.chmod(0o755)
    args_file = root / 'birdc.args'
    postrm = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        'packaging',
        'debian',
        'postrm',
    )

    def run(action, answer=None, bird_exit=0, configure_exit=0):
        env = os.environ.copy()
        env.update({
            'BIRD_MANAGER_REMOVE_ALL_DATA': 'true' if answer else 'false',
            'BIRD_MANAGER_STATE_DIR': str(state),
            'BIRD_MANAGER_ETC_DIR': str(etc),
            'BIRD_MANAGER_APP_DIR': str(app),
            'BIRD_LIVE_DIR': str(live),
            'BIRD_MAIN_CONFIG': str(main),
            'BIRD_BIN_PATH': str(bird),
            'BIRDC_BIN_PATH': str(birdc),
            'BIRD_SOCKET': '',
            'BIRD_MANAGER_SKIP_USER_REMOVAL': '1',
            'FAKE_BIRD_EXIT': str(bird_exit),
            'FAKE_BIRDC_ARGS': str(args_file),
            'FAKE_BIRDC_CONFIGURE_EXIT': str(configure_exit),
        })
        if answer is None:
            env.pop('BIRD_MANAGER_REMOVE_ALL_DATA', None)
        return subprocess.run(
            [postrm, action],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

    return {
        'run': run,
        'root': root,
        'live': live,
        'state': state,
        'etc': etc,
        'app': app,
        'main': main,
        'args': args_file,
    }


def test_remove_declined_preserves_all_data(tmp_path):
    fixture = _removal_fixture(tmp_path)
    result = fixture['run']('remove', answer=False)

    assert result.returncode == 0
    assert fixture['state'].is_dir()
    assert fixture['etc'].is_dir()
    assert (fixture['live'] / 'bird-manager.conf').exists()
    assert '# BEGIN bird-manager managed includes' in fixture['main'].read_text()


def test_remove_confirmed_inlines_main_and_removes_owned_files(tmp_path):
    fixture = _removal_fixture(tmp_path)
    result = fixture['run']('remove', answer=True)

    assert result.returncode == 0, result.stderr
    main_text = fixture['main'].read_text()
    assert 'bird-manager.conf' not in main_text
    assert 'BEGIN bird-manager managed includes' not in main_text
    assert '# operator header' in main_text
    assert '# operator footer' in main_text
    assert 'router id 192.0.2.1;' in main_text
    assert 'protocol static test_routes' in main_text
    assert not fixture['state'].exists()
    assert not fixture['etc'].exists()
    assert not fixture['app'].exists()
    assert not (fixture['live'] / 'networks.conf').exists()
    assert not (fixture['live'] / 'policy_1.conf').exists()
    assert (fixture['live'] / 'operator-note.txt').read_text() == 'preserve me\n'
    assert fixture['args'].read_text().splitlines() == ['show status', 'configure']


def test_purge_always_removes_all_owned_data(tmp_path):
    fixture = _removal_fixture(tmp_path)
    result = fixture['run']('purge')

    assert result.returncode == 0, result.stderr
    assert 'bird-manager.conf' not in fixture['main'].read_text()
    assert not fixture['state'].exists()
    assert not fixture['etc'].exists()
    assert not fixture['app'].exists()
    assert (fixture['live'] / 'operator-note.txt').exists()


def test_remove_validation_failure_preserves_data_and_main(tmp_path):
    fixture = _removal_fixture(tmp_path)
    original = fixture['main'].read_text()
    result = fixture['run']('remove', answer=True, bird_exit=1)

    assert result.returncode == 0
    assert 'cleanup was not completed' in result.stderr
    assert fixture['main'].read_text() == original
    assert fixture['state'].is_dir()
    assert (fixture['live'] / 'bird-manager.conf').exists()


def test_remove_reconfigure_failure_rolls_back_main_and_preserves_data(tmp_path):
    fixture = _removal_fixture(tmp_path)
    original = fixture['main'].read_text()
    result = fixture['run']('remove', answer=True, configure_exit=1)

    assert result.returncode == 0
    assert 'cleanup was not completed' in result.stderr
    assert fixture['main'].read_text() == original
    assert fixture['state'].is_dir()
    assert (fixture['live'] / 'bird-manager.conf').exists()


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


def _wsgi_call(routes, method='GET', path='/api/health', body=b'', environ=None, query=''):
    """Invoke the WSGI app directly so REMOTE_ADDR/Origin/XFF can be forged."""
    payload = body or b''
    stream = io.BytesIO(payload)
    env = {
        'REQUEST_METHOD': method,
        'PATH_INFO': path,
        'QUERY_STRING': query,
        'CONTENT_LENGTH': str(len(payload)),
        'REMOTE_ADDR': '127.0.0.1',
        'SERVER_NAME': 'testserver',
        'SERVER_PORT': '80',
        'SERVER_PROTOCOL': 'HTTP/1.1',
        'wsgi.version': (1, 0),
        'wsgi.url_scheme': 'http',
        'wsgi.input': stream,
        'wsgi.errors': io.StringIO(),
        'wsgi.multithread': False,
        'wsgi.multiprocess': False,
        'wsgi.run_once': False,
    }
    env.update(environ or {})
    captured = {}

    def start_response(status, headers):
        captured['status'] = status
        captured['headers'] = headers

    chunks = routes.app(env, start_response)
    raw = b''.join(chunks)
    return captured['status'], dict(captured['headers']), raw, stream, captured


@pytest.fixture
def access_api(tmp_path, monkeypatch):
    _bind_test_tree(tmp_path, monkeypatch)
    import app.routes as routes
    return routes


def test_default_allowlist_permits_loopback_and_blocks_other_sources(access_api):
    assert _wsgi_call(access_api)[0].startswith('200')
    status, _headers, raw, _stream, _cap = _wsgi_call(
        access_api, environ={'REMOTE_ADDR': '10.11.12.13'}
    )
    assert status.startswith('403')
    assert b'BIRD_TRUSTED_SOURCES' in raw


def test_auto_token_expands_to_primary_network_without_widening(access_api, monkeypatch):
    monkeypatch.setattr(access_api, 'detect_router_id', lambda: '10.1.2.3')
    monkeypatch.setattr(access_api, '_primary_ipv4_prefixlen', lambda address: 24)
    assert access_api._parse_networks('auto') == (ipaddress.ip_network('10.1.2.0/24'),)

    monkeypatch.setattr(access_api, '_primary_ipv4_prefixlen', lambda address: None)
    assert access_api._parse_networks('auto') == (ipaddress.ip_network('10.1.2.3/32'),)

    monkeypatch.setattr(access_api, 'detect_router_id', lambda: 'not-an-ip')
    assert access_api._parse_networks('auto') == ()


def test_allowlist_accepts_explicit_entries_and_skips_garbage(access_api):
    networks = access_api._parse_networks('192.168.0.0/21, 10.0.0.5, nonsense, ::1/128')
    assert networks == (
        ipaddress.ip_network('192.168.0.0/21'),
        ipaddress.ip_network('10.0.0.5/32'),
        ipaddress.ip_network('::1/128'),
    )


def test_origin_allowlist_gates_mutating_methods_only(access_api, monkeypatch):
    monkeypatch.setenv('BIRD_TRUSTED_ORIGINS', 'http://allowed.example')
    payload = json.dumps({'filename': 'networks.conf', 'content': '# x\n'}).encode()
    headers = {'Content-Type': 'application/json'}

    status, _h, raw, _s, _c = _wsgi_call(
        access_api, 'POST', '/api/config/preview', payload, headers)
    assert status.startswith('403')
    assert b'BIRD_TRUSTED_ORIGINS' in raw

    status, _h, _raw, _s, _c = _wsgi_call(
        access_api, 'POST', '/api/config/preview', payload,
        {**headers, 'HTTP_ORIGIN': 'http://evil.example'})
    assert status.startswith('403')

    status, _h, _raw, _s, _c = _wsgi_call(
        access_api, 'POST', '/api/config/preview', payload,
        {**headers, 'HTTP_ORIGIN': 'http://allowed.example'})
    assert not status.startswith('403')

    status, _h, _raw, _s, _c = _wsgi_call(
        access_api, 'GET', '/api/health', environ={'HTTP_ORIGIN': 'http://evil.example'})
    assert status.startswith('200')


def test_forwarded_header_is_ignored_unless_peer_is_trusted_proxy(access_api, monkeypatch):
    forwarded = {'HTTP_X_FORWARDED_FOR': '127.0.0.1'}

    status, _h, _raw, _s, _c = _wsgi_call(
        access_api, environ={'REMOTE_ADDR': '10.11.12.13', **forwarded})
    assert status.startswith('403')

    monkeypatch.setenv('BIRD_TRUSTED_PROXIES', '10.11.12.13')
    status, _h, _raw, _s, _c = _wsgi_call(
        access_api, environ={'REMOTE_ADDR': '10.11.12.13', **forwarded})
    assert status.startswith('200')


def test_forwarded_chain_skips_trusted_hops(access_api, monkeypatch):
    # XFF grows to the left: <client>, <proxy1>, ... The client is the first
    # entry from the right that is not itself a configured proxy.
    monkeypatch.setenv('BIRD_TRUSTED_SOURCES', '127.0.0.1/32')
    monkeypatch.setenv('BIRD_TRUSTED_PROXIES', '10.11.12.13,10.0.0.9')
    peer = {'REMOTE_ADDR': '10.11.12.13'}

    status, _h, _raw, _s, _c = _wsgi_call(
        access_api, environ={**peer, 'HTTP_X_FORWARDED_FOR': '127.0.0.1, 10.0.0.9'})
    assert status.startswith('200')

    status, _h, _raw, _s, _c = _wsgi_call(
        access_api, environ={**peer, 'HTTP_X_FORWARDED_FOR': '203.0.113.7, 10.0.0.9'})
    assert status.startswith('403')


def test_oversized_body_is_rejected_before_it_is_read(access_api, monkeypatch):
    monkeypatch.setenv('BIRD_MAX_BODY_BYTES', '128')
    payload = json.dumps({'content': 'x' * 512}).encode()
    status, _h, raw, stream, _c = _wsgi_call(
        access_api, 'POST', '/api/config/preview', payload,
        {'CONTENT_LENGTH': str(len(payload))})
    assert status.startswith('413')
    assert b'BIRD_MAX_BODY_BYTES' in raw
    assert stream.tell() == 0, 'rejected request body must not be read'

    small = json.dumps({'filename': 'networks.conf', 'content': '# x\n'}).encode()
    status, _h, _raw, _s, _c = _wsgi_call(
        access_api, 'POST', '/api/config/preview', small,
        {'Content-Type': 'application/json', 'CONTENT_LENGTH': str(len(small))})
    assert not status.startswith('413')


def test_every_response_carries_security_headers(access_api):
    _status, headers, _raw, _s, _c = _wsgi_call(access_api)
    assert headers['X-Content-Type-Options'] == 'nosniff'
    assert headers['X-Frame-Options'] == 'DENY'
    assert headers['Referrer-Policy'] == 'no-referrer'
    assert headers['Cache-Control'] == 'no-store'
    csp = headers['Content-Security-Policy']
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "'unsafe-inline'" not in csp


def test_server_errors_do_not_leak_internals_unless_debugging(access_api, monkeypatch):
    def _explode(request):
        raise RuntimeError('secret path /srv/private/token')

    access_api.ROUTES.append({
        'pattern': '/api/_p0_explode',
        'method': 'POST',
        'handler': _explode,
    })
    try:
        status, _h, raw, _s, _c = _wsgi_call(access_api, 'POST', '/api/_p0_explode')
        assert status.startswith('500')
        assert b'secret path' not in raw
        assert b'/srv/private' not in raw
        assert b'Traceback' not in raw
        assert json.loads(raw)['error'] == 'Внутренняя ошибка сервера'

        monkeypatch.setenv('BIRD_DEBUG_ERRORS', '1')
        status, _h, raw, _s, _c = _wsgi_call(access_api, 'POST', '/api/_p0_explode')
        assert status.startswith('500')
        assert b'secret path' in raw
    finally:
        access_api.ROUTES[:] = [
            entry for entry in access_api.ROUTES
            if entry['pattern'] != '/api/_p0_explode'
        ]


def test_bind_address_comes_from_env_and_cli_default(access_api, monkeypatch):
    monkeypatch.delenv('APP_HOST', raising=False)
    assert access_api._listen_address() == '0.0.0.0'
    monkeypatch.setenv('APP_HOST', '127.0.0.1')
    assert access_api._listen_address() == '127.0.0.1'
    monkeypatch.setenv('APP_HOST', '   ')
    assert access_api._listen_address() == '0.0.0.0'


def test_startup_log_reports_effective_policy(access_api, capsys):
    access_api._log_access_settings(access_api._access_settings())
    output = capsys.readouterr().out
    assert '[ACCESS] trusted sources:' in output
    assert '[ACCESS] max request body:' in output


def test_dashboard_renders_username_meta_without_inline_script(access_api, monkeypatch):
    monkeypatch.setenv('USERNAME', 'Ops "Team"')
    response = access_api.serve_dashboard(request=None)
    body = response.body.decode('utf-8')
    assert 'name="bird-manager-user" content="Ops &quot;Team&quot;"' in body
    assert '{{USERNAME_HTML}}' not in body
    assert '<script>window.' not in body
    assert 'id="toast-stack"' in body


# ---------------------------------------------------------------------------
# Error visibility
# ---------------------------------------------------------------------------


def test_frontend_surfaces_every_failure():
    with open('static/js/app.js', 'r', encoding='utf-8') as stream:
        script = stream.read()
    with open('static/css/app.css', 'r', encoding='utf-8') as stream:
        css = stream.read()
    with open('templates/dashboard.html', 'r', encoding='utf-8') as stream:
        dashboard = stream.read()

    # The offline banner is cleared as soon as the server answers again.
    api_fetch = script[script.index('async function apiFetch'):script.index('function setOfflineBanner')]
    assert 'setOfflineBanner(false);' in api_fetch
    assert 'err.status = res.status;' in api_fetch

    # safeFetch always reports: no call site may pass a no-op handler.
    assert ', null)' not in script
    assert 'const handler = onError || toastError(endpointLabel(path));' in script
    assert 'function reportError(context, error)' in script
    assert 'function toastError(context)' in script
    assert 'function endpointLabel(path)' in script
    assert 'HTTP ' in script

    # Toast layer exists in markup, styles and logic.
    assert 'id="toast-stack"' in dashboard
    assert 'role="status"' in dashboard
    assert 'aria-live="polite"' in dashboard
    assert 'function showToast(kind, text, timeoutMs)' in script
    assert '.toast-stack {' in css
    assert '.toast-error' in css
    assert '.toast-hide' in css

    # Nothing fails silently: global handlers for uncaught errors.
    assert "window.addEventListener('unhandledrejection'" in script
    assert "window.addEventListener('error'" in script

    # Every mutating call site keeps its result check.
    for path in ('/api/networks', '/api/communities', '/api/bgp_peers', '/api/backup'):
        assert path in script


def test_packaging_documents_access_control_keys():
    expected = (
        'BIRD_TRUSTED_SOURCES=',
        'BIRD_TRUSTED_PROXIES=',
        'BIRD_TRUSTED_ORIGINS=',
        'BIRD_MAX_BODY_BYTES=',
        'BIRD_DEBUG_ERRORS=',
        'APP_HOST=',
    )
    for filename in ('.env', 'scripts/build-deb.sh', 'packaging/debian/postinst'):
        with open(filename, 'r', encoding='utf-8') as stream:
            content = stream.read()
        for key in expected:
            assert key in content, f'{key} missing from {filename}'

    for filename in ('README.md', 'packaging/bird-manager.8', 'packaging/README.Debian'):
        with open(filename, 'r', encoding='utf-8') as stream:
            content = stream.read()
        assert 'BIRD_TRUSTED_SOURCES' in content, filename
        assert 'BIRD_TRUSTED_ORIGINS' in content, filename


# ---------------------------------------------------------------------------
def test_bgp_peers_router_id_is_in_the_list_payload():
    """The RouterOS template needs BIRD's own address.

    Parsed client-side it would have to come from networks.conf, which the peers
    panel never loads; and re-detecting on the client could answer with a
    different address than the one the generator wrote into the bundle.
    """
    with open('app/routes.py', 'r', encoding='utf-8') as stream:
        source = stream.read()

    assert '_resolve_router_id' in source
    router_id_line = "router_id = _resolve_router_id(_bird_settings()['router_id'])"
    assert router_id_line in source
    assert "'router_id': router_id," in source
    # Resolved once per request, not once per peer.
    assert source.count(router_id_line) == 1


def test_ros_config_template_fills_every_placeholder():
    """The generated RouterOS text must be paste-ready.

    The template arrived wrapped with a terminal colour escape, stray semicolons
    and backslash line continuations; a split ".port=\\n179" would be rejected
    by the router, so the substitution is checked on all of that.
    """
    with open('static/js/app.js', 'r', encoding='utf-8') as stream:
        script = stream.read()

    start = script.index('function rosConfigFor(')
    end = script.index('function openRosConfig(')
    body = script[start:end]

    assert 'function rosConfigFor(peer, routerId)' in body
    for placeholder in ('<neighbor as from peer>', '<bgp neighbor ip>',
                        "<bird manager's router id>", '<local as from peer>',
                        '<bgp peer name>'):
        assert placeholder not in script, placeholder
    # Every value lands where the peer says it does.
    assert "'birdmanager_' + peer.name" in body
    assert body.count('peer.neighbor_as') == 3      # instance, template, connection
    assert "local.address=' + peer.neighbor + '" in body
    assert "remote.address=' + routerId + '/32'" in body
    assert "' .as=' + peer.local_as + ' .port=179'" in body
    # Paste-ready: the port stays one value, no terminal codes, no padding.
    assert "' .port=179'" in body
    assert '.port=\n' not in body
    assert '[35;120;46M' not in script and '\\x1b' not in script
    assert ';;;;' not in body


def test_ros_button_column_is_declared_and_pinned():
    """Header, row and CSS must agree or every column shifts sideways."""
    with open('static/js/app.js', 'r', encoding='utf-8') as stream:
        script = stream.read()
    with open('static/css/app.css', 'r', encoding='utf-8') as stream:
        css = stream.read()

    cols = script[script.index('const DB_PEER_COLUMNS'):]
    cols = cols[:cols.index('];')]
    # Declared first, so it sits between the peer name and Статус.
    assert cols.index("'peer-col-cfg'") < cols.index("'peer-col-status'")
    assert 'peer-col-cfg' in script[script.index('function buildPeerRow'):]
    assert '.peer-col-cfg' in css
    # Pinned, and the row minimum grew by the column plus one gap.
    assert '.peer-col-cfg   { flex: 0 0 48px; }' in css
    assert css.count('.peer-head, .peer-row { min-width: 660px; }') == 1
    assert 'min-width: 600px' not in css
    assert "rosBtn.textContent = 'ROS';" in script
    assert "rosBtn.addEventListener('click', () => openRosConfig(peer));" in script
    # The dialog has to be wired, otherwise the buttons do nothing.
    assert 'wireRosDialog();' in script


def test_ros_dialog_markup_and_copy_fallback_exist():
    """Clipboard API needs a secure context; plain-http LAN access has none."""
    with open('static/js/app.js', 'r', encoding='utf-8') as stream:
        script = stream.read()
    with open('templates/dashboard.html', 'r', encoding='utf-8') as stream:
        dashboard = stream.read()
    with open('static/css/app.css', 'r', encoding='utf-8') as stream:
        css = stream.read()

    for marker in ('id="ros-config"', 'id="ros-title"', 'id="ros-warning"',
                   'id="ros-body"', 'id="ros-copy"', 'id="ros-close"'):
        assert marker in dashboard, marker
    assert 'function wireRosDialog()' in script
    assert 'navigator.clipboard.writeText' in script
    assert "document.execCommand('copy')" in script
    # Manual selection is the last resort, so copying is always possible.
    assert 'range.selectNodeContents(body)' in script
    # Strict CSP: no inline handlers anywhere in the new code.
    assert 'addEventListener' in script
    for line in dashboard.splitlines():
        assert 'onclick=' not in line, line
    assert '.ros-body' in css and '.ros-dialog::backdrop' in css


def test_bgp_peer_form_export_scope_box_stays_next_to_its_label():
    """The scope checkbox must sit on the label line, not float across the form.

    Regression: the box carried the generic ``field`` class, so ``.field input``
    stretched it to 100% width and it rendered far to the right of its own
    label. The label line also declared ``display: block`` while relying on
    flex-only properties, which left the hint on the same line as the box.
    """
    with open('static/js/app.js', 'r', encoding='utf-8') as stream:
        script = stream.read()
    with open('static/css/app.css', 'r', encoding='utf-8') as stream:
        css = stream.read()

    # The generic ``field`` class must stay off the scope element: it is what
    # stretches a control to 100% width.
    assert "scopeCol.className = 'bgp-scope-field';" in script
    assert "scopeCol.className = 'field" not in script

    assert "scopeText.textContent = t('m136');" in script
    assert "scopeHint.textContent = t('m104');" in script
    assert 'Снимите галочку' not in script

    # Off unless the operator ticks it, and the box is created before the label
    # text so the reading order stays "box, then what it does".
    assert 'scopeInput.checked' not in script
    assert script.index('scopeCol.appendChild(scopeInput)') < script.index(
        'scopeCol.appendChild(scopeText)'
    )

    scope_rule = css[css.index('.bgp-scope-field {'):css.index('.peer-col-status')]
    assert 'display: flex;' in scope_rule
    assert 'align-items: center;' in scope_rule
    assert 'flex-wrap: wrap;' in scope_rule
    assert '.bgp-scope-field input' in scope_rule
    assert 'width: auto;' in scope_rule
    assert 'display: block;' not in scope_rule


# BIRD peers block
# ---------------------------------------------------------------------------

PEER_PROTOCOLS_FIXTURE = """BIRD 3.2.0 ready.
Name       Proto      Table      State  Since         Info
direct1    Direct     ---        up     2026-09-25    
  Channel ipv4
    State:          UP
    Routes:         1 imported, 0 exported, 1 preferred

main_mkt   BGP        ---        start  2026-09-25    Active        Socket: Connection refused
  BGP state:          Active
    Neighbor address: 192.168.0.1
    Neighbor AS:      64999
    Local AS:         65500
    Last error:       Socket: Connection refused
  Channel ipv4
    State:          DOWN

peer_lan   BGP        master4    up     2026-09-26 12:04:11
  BGP state:          Established
    Neighbor address: 10.0.0.9
    Neighbor AS:      65001
    Local AS:         65500
  Channel ipv4
    State:          UP
    Routes:         1204 imported, 1180 exported, 1204 preferred
"""


def test_bird_protocols_endpoint_reports_live_snapshot(access_api, monkeypatch):
    import app.routes as routes

    snapshot = {
        'bird_running': True,
        'error': None,
        'bgp': [{
            'name': 'main_mkt', 'state': 'start', 'bgp_state': 'Active',
            'channel_state': 'DOWN', 'neighbor': '192.168.0.1',
            'neighbor_as': '64999', 'local_as': '65500',
            'since': '2026-09-25', 'info': 'Active',
            'last_error': 'Socket: Connection refused',
            'imported': None, 'exported': None, 'in_bird': True,
        }],
        'service': [{
            'name': 'direct1', 'proto': 'Direct', 'state': 'up',
            'channel_state': 'UP', 'import_state': 'UP',
            'since': '2026-09-25',
        }],
    }
    monkeypatch.setattr(routes.config_manager, '_bird_protocols', lambda: snapshot)

    session = routes.get_session()
    try:
        session.add(routes.BgpPeer(
            name='main_mkt', local_as='65500', neighbor='192.168.0.1',
            neighbor_as='64999', status='active',
        ))
        session.add(routes.BgpPeer(
            name='not_in_bird', local_as='65500', neighbor='10.9.9.9',
            neighbor_as='65002', status='active',
        ))
        session.commit()
    finally:
        session.close()

    status, headers, raw, _stream, _cap = _wsgi_call(access_api, 'GET', '/api/bird/protocols')
    assert status.startswith('200')
    payload = json.loads(raw)
    assert payload['status'] == 'ok'
    assert payload['bird_running'] is True
    assert payload['error'] is None
    assert [p['name'] for p in payload['service']] == ['direct1']

    peers = {peer['name']: peer for peer in payload['peers']}
    assert set(peers) == {'main_mkt', 'not_in_bird'}
    assert peers['main_mkt']['managed'] is True
    assert peers['main_mkt']['last_error'] == 'Socket: Connection refused'
    assert peers['main_mkt']['imported'] is None
    # A peer configured in the panel but absent from BIRD stays visible.
    assert peers['not_in_bird']['in_bird'] is False
    assert peers['not_in_bird']['managed'] is True
    assert peers['not_in_bird']['neighbor'] is None


def test_bird_protocols_endpoint_reports_daemon_error(access_api, monkeypatch):
    import app.routes as routes

    monkeypatch.setattr(routes.config_manager, '_bird_protocols', lambda: {
        'bird_running': False,
        'error': 'BIRD is not running (control socket is missing)',
        'bgp': [],
        'service': [],
    })
    status, _headers, raw, _stream, _cap = _wsgi_call(
        access_api, 'GET', '/api/bird/protocols')
    assert status.startswith('200')
    payload = json.loads(raw)
    assert payload['status'] == 'error'
    assert payload['bird_running'] is False
    assert payload['peers'] == []
    assert 'not running' in payload['error']


# Since prints the bare time for protocols started today and a date otherwise.
# Captured from birdc 3.2.0 after a restart on the same day.
PEER_PROTOCOLS_SAME_DAY_FIXTURE = """BIRD 3.2.0 ready.
Name       Proto      Table      State  Since         Info
managed_static_routes Static     master4    up     10:08:14.529
  Created:            10:08:14.529
  Channel ipv4
    State:          UP
    Import state:   UP
    Export state:   DOWN
    Routes:         7818 imported, 0 exported, 7818 preferred

main_mkt   BGP        ---        up     10:10:52.010  Established
  BGP state:          Established
    Neighbor address: 192.168.0.1
    Neighbor AS:      64999
    Local AS:         65500
    Channel ipv4
      State:          UP
      Routes:         0 imported, 7802 exported, 0 preferred
"""

def test_parser_accepts_birdc_since_without_a_date():
    """Regression: every live peer showed «нет в BIRD» after a same-day restart.

    BIRD prints a bare ``HH:MM:SS.mmm`` in the Since column for protocols that
    started today, and a date for older ones. The summary regex required the
    date, so after a restart no row matched: the snapshot came back empty while
    the daemon was running fine, and the endpoint appended the panel peers with
    ``in_bird: false`` — a peer that was established and exporting prefixes all
    along.
    """
    from app.config_manager import ConfigManager

    records = ConfigManager._parse_protocols_output(PEER_PROTOCOLS_SAME_DAY_FIXTURE)
    by_name = {r['name']: r for r in records}
    assert set(by_name) == {'managed_static_routes', 'main_mkt'}

    bgp = by_name['main_mkt']
    assert bgp['proto'] == 'BGP'
    assert bgp['state'] == 'up'
    assert bgp['since'] == '10:10:52.010'
    assert bgp['info'] == 'Established'
    assert bgp['bgp_state'] == 'Established'
    assert bgp['neighbor'] == '192.168.0.1'
    assert bgp['neighbor_as'] == '64999'
    assert bgp['local_as'] == '65500'
    assert bgp['channel_state'] == 'UP'
    assert bgp['exported'] == 7802

    service = by_name['managed_static_routes']
    assert service['proto'] == 'Static'
    assert service['since'] == '10:08:14.529'
    assert service['import_state'] == 'UP'

    # The date forms must keep working: they are what older hosts still print.
    old = ConfigManager._parse_protocols_output(PEER_PROTOCOLS_FIXTURE)
    old_bgp = {r['name']: r for r in old if r['proto'] == 'BGP'}
    assert set(old_bgp) == {'main_mkt', 'peer_lan'}
    assert old_bgp['main_mkt']['since'] == '2026-09-25'
    assert old_bgp['peer_lan']['since'] == '2026-09-26 12:04:11'


def test_parser_fixture_matches_real_birdc_shape():
    from app.config_manager import ConfigManager

    records = ConfigManager._parse_protocols_output(PEER_PROTOCOLS_FIXTURE)
    bgp = {r['name']: r for r in records if r['proto'] == 'BGP'}
    assert set(bgp) == {'main_mkt', 'peer_lan'}
    assert bgp['peer_lan']['imported'] == 1204
    assert bgp['peer_lan']['exported'] == 1180
    assert bgp['main_mkt']['last_error'] == 'Socket: Connection refused'


def test_bird_peers_panel_sits_above_config_status():
    with open('templates/dashboard.html', 'r', encoding='utf-8') as stream:
        dashboard = stream.read()
    system = dashboard[dashboard.index('data-tab-panel="system"'):]
    assert 'id="panel-bgp-sessions"' in system
    assert system.index('id="panel-bgp-sessions"') < system.index('id="panel-config-status"')
    assert 'id="bgp-sessions-body"' in system
    assert '>BGP-сессии</button>' in system
    # The new panels belong to the System tab only.
    assert dashboard.count('id="panel-bgp-sessions"') == 1


def test_peer_table_columns_and_cells():
    with open('static/js/app.js', 'r', encoding='utf-8') as stream:
        script = stream.read()

    columns = script[script.index('const PEER_COLUMNS'):script.index('];', script.index('const PEER_COLUMNS'))]
    for label in ('Managed', 'Peer', 'Neighbor', 'Neighbor AS', 'Local AS',
                  "t('m159')", "t('m161')", 'Since', 'Last error'):
        assert label in columns, label
    assert columns.index("'Managed'") < columns.index("'Peer'")
    assert columns.index("'Since'") < columns.index("'Last error'")

    # rx/tx counters: two lines inside one cell, rendered as spans.
    assert 'function peerCounterLine(prefix, value)' in script
    assert "peerCounterLine('rx', peer.imported)" in script
    assert "peerCounterLine('tx', peer.exported)" in script
    assert "prefix + ' —'" in script, 'unknown counters must render as a dash'
    # Managed is plain yes/no.
    assert "managed.textContent = peer.managed ? 'yes' : 'no';" in script
    # Last error is always the ninth cell, even when empty.
    assert "error.className = 'peer-error';" in script
    assert "error.textContent = peer.last_error || '';" in script
    assert 'row.append(managed, name, neighbor, neighborAs, localAs,' in script
    assert 'stateCell, prefixes, since, error)' in script
    assert 'cell.colSpan = PEER_COLUMNS.length;' in script
    # No nested table cells: counters are spans, not <td>.
    assert "document.createElement('td')" in script
    assert 'peerCounterLine' in script and 'peerCounterCell' not in script
    # Service protocols are rendered as simple up/down rows.
    assert "className = 'peer-service-row'" in script


def test_peer_state_cell_renders_two_badges_with_source_tooltips():
    with open('static/js/app.js', 'r', encoding='utf-8') as stream:
        script = stream.read()

    assert 'function peerStateCell(peer)' in script
    cell = script[script.index('function peerStateCell(peer)'):]
    cell = cell[:cell.index('\n}\n')]

    # Peer missing from BIRD: single amber badge with an explanatory tooltip.
    assert "missing.className = 'badge badge-other';" in cell
    assert "missing.textContent = t('m156');" in cell
    assert "missing.title = t('m105');" in cell

    # First line: channel state, pill design, tooltip names the birdc field.
    assert "channelBadge.className = 'badge ' + (channelUp ? 'badge-applied' : 'badge-pending');" in cell
    assert "'" + "t('m117')" + "'" in cell or "t('m117')" in cell
    assert "t('m114')" in cell
    assert "channelBadge.title = (peer.channel_state" in cell

    # Second line: BGP state with a fallback to the summary State column.
    assert "const detail = peer.bgp_state || peer.state || '';" in cell
    assert "'BGP state: '" in cell
    assert "detailBadge.title = (peer.bgp_state" in cell
    assert "const established = detailKey === 'established' || detailKey === 'up';" in cell
    assert "detailBadge.className = 'badge ' + (established ? 'badge-applied' : 'badge-other');" in cell

    # The second line is dropped when it repeats the channel state.
    assert "if (!detail || detail.toLowerCase() === String(channel).toLowerCase()) return cell;" in cell
    # Both lines are badges; the muted peer-fsm style is gone from this cell.
    assert "detailBadge.className = 'badge '" in cell
    assert 'peer-fsm' not in cell
    # Still two separate pill lines in the same cell.
    assert 'cell.appendChild(document.createElement(\'br\'));' in cell


def test_peers_poll_rules():
    with open('static/js/app.js', 'r', encoding='utf-8') as stream:
        script = stream.read()

    assert 'const PEERS_POLL_MS = 30000;' in script
    assert 'const PeersPoll = {' in script
    poll = script[script.index('const PeersPoll = {'):]
    poll = poll[:poll.index('\n};')]
    assert 'setInterval' in poll
    assert 'async tick()' in poll
    assert 'if (!peersPanelVisible()) {' in poll
    assert 'loadBirdPeers({ quiet: true });' in poll

    # Visibility rules: expanded panel, System tab, visible page.
    visible = script[script.index('function peersPanelVisible()'):]
    visible = visible[:visible.index('\n}')]
    assert "document.getElementById('panel-bgp-sessions')" in visible
    assert "classList.contains('expanded')" in visible
    assert "currentTab !== 'system'" in visible
    assert 'document.hidden' in visible

    # Manual refresh resets the timer, leaving the tab stops it.
    assert 'PeersPoll.reset();' in script
    assert "if (name !== 'system') PeersPoll.stop();" in script
    assert "if (isExpanded) PeersPoll.stop(); else PeersPoll.start();" in script
    # The block loads with the System tab and on a full dashboard refresh.
    assert script.count('await loadBirdPeers();') == 2
    # Auto-poll failures stay inline; manual ones still toast.
    assert 'opts.quiet' in script
    assert "setPeersMessage(t('m130'), 'error')" in script


def test_peer_block_layout_css():
    with open('static/css/app.css', 'r', encoding='utf-8') as stream:
        css = stream.read()

    assert '.panel-bgp-sessions,' in css
    peers_rule = css[css.index('.panel-add,'):css.index('.panel-hierarchy:not(.expanded)')]
    assert '.panel-bgp-sessions,' in peers_rule
    assert 'flex: 0 0 auto;' in peers_rule
    assert 'max-height: 40vh;' in peers_rule
    assert '.panel-bgp-sessions:not(.expanded)' in css
    assert '.panel-bgp-sessions .panel-body' in css
    assert '.peer-table-wrap { overflow-x: auto; }' in css
    assert '.peer-error {' in css
    error_rule = css[css.index('.peer-table td.peer-error {'):]
    error_rule = error_rule[:error_rule.index('}')]
    assert 'color: #dc2626;' in error_rule
    assert 'white-space: normal;' in error_rule
    assert 'overflow-wrap: anywhere;' in error_rule


# ---------------------------------------------------------------------------
# Preview syntax validation against the real bundle
# ---------------------------------------------------------------------------


def _bundle_with_peer(request, routes, suffix):
    """Regenerate a complete split bundle for the ConfigManager under test.

    ``routes`` is the imported module, so the bundle lands in the staged tree
    the preview really reads — the WSGI fixture keeps one module (and therefore
    one ConfigManager) for the whole module, not one per test. Protocol name
    and neighbor ASN are unique per test because the database is shared too.
    """
    name, asn, neighbor = f'peer_{suffix}', f'6495{suffix}', f'192.168.0.5{suffix}'
    status, payload = request('POST', '/api/bgp_peers', {
        'name': name, 'local_as': '65500',
        'neighbor': neighbor, 'neighbor_as': asn,
    })
    assert status == 201, payload

    manager = routes.config_manager
    generate_split(
        [{'prefix': '203.0.113.0/24', 'community_ids': [7], 'enabled': True}],
        [{'id': 7, 'name': '65500:7', 'values': '', 'enabled': True}],
        staged_dir=manager.staged_path, managed_include_dir=manager.live_path,
        router_id='203.0.113.254', local_as='65500',
        bgp_peers=[{
            'name': name, 'local_as': '65500',
            'neighbor': neighbor, 'neighbor_as': asn,
        }],
    )
    staged = pathlib.Path(manager.staged_path)
    assert (staged / 'core.conf').is_file()
    assert (staged / 'export.conf').is_file()
    return (staged / 'networks.conf').read_text()


def _preview(request, content):
    return request('POST', '/api/config/preview', {
        'filename': 'networks.conf', 'content': content,
    })


def test_preview_reports_no_error_for_generated_bundle(http_api):
    """Regression: the preview used its own include list and went stale.

    Without export.conf in the preview bundle, core.conf referenced an
    undefined export filter, and the error line from core.conf was painted as
    row 18 inside the 5-line networks.conf editor.
    """
    request, routes, _live, _staged, _main = http_api
    content = _bundle_with_peer(request, routes, '1')

    status, preview = _preview(request, content)
    assert status == 200
    assert preview['syntax']['valid'] is True, preview['syntax']
    assert preview['syntax']['errors'] == []
    assert preview['syntax_error_rows'] == []


def test_preview_error_in_another_file_gives_no_editor_row(http_api):
    request, routes, _live, _staged, _main = http_api
    content = _bundle_with_peer(request, routes, '2')

    core = pathlib.Path(routes.config_manager.staged_path) / 'core.conf'
    original = core.read_text()
    core.write_text(original + '\nthis is not bird syntax;\n')
    try:
        status, preview = _preview(request, content)
        assert status == 200
        syntax = preview['syntax']
        assert syntax['valid'] is False
        assert 'core.conf' in syntax['errors'][0]
        # The offending line lives in core.conf, so the networks.conf editor
        # must not get a red bar for a row that does not exist there.
        assert preview['syntax_error_rows'] == []
    finally:
        core.write_text(original)


def test_preview_error_in_edited_text_still_marks_a_row(http_api):
    request, routes, _live, _staged, _main = http_api
    content = _bundle_with_peer(request, routes, '3')

    status, preview = _preview(
        request, content + '\nnetwork bogus_prefix_here;\n'
    )
    assert status == 200
    assert preview['syntax']['valid'] is False
    assert preview['syntax_error_rows'], 'a real editor error must be highlighted'
    assert all(isinstance(row, int) and row >= 1 for row in preview['syntax_error_rows'])


# ---------------------------------------------------------------------------
# One peer per neighbor address
# ---------------------------------------------------------------------------


def _post_peer(routes, name, neighbor, neighbor_as, local_as='65500'):
    """POST /api/bgp_peers through the WSGI app so 4xx bodies stay readable."""
    body = json.dumps({
        'name': name, 'local_as': local_as,
        'neighbor': neighbor, 'neighbor_as': neighbor_as,
    }).encode()
    status, _headers, raw, _stream, _cap = _wsgi_call(
        routes, 'POST', '/api/bgp_peers', body=body,
    )
    return status, json.loads(raw)


def _peer_neighbors(routes):
    """Neighbor addresses currently stored in the shared test database."""
    session = routes.get_session()
    try:
        return [peer.neighbor for peer in routes.crud.get_bgp_peers(session)]
    finally:
        session.close()


def test_second_peer_on_the_same_address_is_rejected(access_api):
    """A second protocol towards one address can never establish.

    BIRD hands an incoming session to the first matching listen request and the
    remote router keys its neighbor by address, so a different neighbor ASN on
    the same IP just produces two sessions that cannot both come up. The panel
    therefore refuses the second one instead of publishing a config that looks
    valid and silently fails at runtime.
    """
    assert _post_peer(access_api, 'dup_ok', '192.168.0.61', '64961')[0].startswith('201')

    status, payload = _post_peer(access_api, 'dup_no', '192.168.0.61', '64962')
    assert status.startswith('400')
    assert '192.168.0.61' in payload['message']

    peers = _peer_neighbors(access_api)
    assert peers.count('192.168.0.61') == 1
    assert '192.168.0.62' not in peers


def test_same_asn_on_another_address_is_still_rejected(access_api):
    """The pre-existing ASN guard must keep working next to the new check."""
    assert _post_peer(access_api, 'asn_ok', '192.168.0.62', '64963')[0].startswith('201')

    status, payload = _post_peer(access_api, 'asn_no', '192.168.0.63', '64963')
    assert status.startswith('400')
    assert '64963' in payload['message']
    assert '192.168.0.63' not in _peer_neighbors(access_api)


def test_distinct_address_and_asn_are_accepted(access_api):
    assert _post_peer(access_api, 'both_ok_a', '192.168.0.64', '64964')[0].startswith('201')
    assert _post_peer(access_api, 'both_ok_b', '192.168.0.65', '64965')[0].startswith('201')
    assert '192.168.0.64' in _peer_neighbors(access_api)
    assert '192.168.0.65' in _peer_neighbors(access_api)


def test_generated_core_conf_blocks_are_scanned(access_api, tmp_path, monkeypatch):
    """core.conf is where the generated peers live since the split bundle.

    Both guards used to read networks.conf only, so a peer present in the
    generated config did not block a second one. The module-level paths are
    frozen at import time, so the fixture has to repoint them.
    """
    live = tmp_path / 'scan_live'
    staged = tmp_path / 'scan_staged'
    live.mkdir()
    staged.mkdir()
    (live / 'core.conf').write_text(
        'protocol bgp mkt_up {\n'
        '    local as 65500;\n'
        '    neighbor 192.168.0.71 as 64971;\n'
        '}\n'
    )
    monkeypatch.setattr(access_api, '_LIVE_PATH', str(live))
    monkeypatch.setattr(access_api, '_STAGED_PATH', str(staged))

    status, payload = _post_peer(access_api, 'scan_ip', '192.168.0.71', '64972')
    assert status.startswith('400')
    assert '192.168.0.71' in payload['message']

    status, payload = _post_peer(access_api, 'scan_as', '192.168.0.72', '64971')
    assert status.startswith('400')
    assert '64971' in payload['message']

    assert '192.168.0.71' not in _peer_neighbors(access_api)
    assert '192.168.0.72' not in _peer_neighbors(access_api)


def test_legacy_networks_conf_is_scanned_too(access_api, tmp_path, monkeypatch):
    """Databases that never moved to the split layout keep peers in networks.conf."""
    live = tmp_path / 'legacy_live'
    staged = tmp_path / 'legacy_staged'
    live.mkdir()
    staged.mkdir()
    (live / 'networks.conf').write_text(
        'protocol bgp legacy {\n    neighbor 192.168.0.73 as 64973;\n}\n'
    )
    monkeypatch.setattr(access_api, '_LIVE_PATH', str(live))
    monkeypatch.setattr(access_api, '_STAGED_PATH', str(staged))

    status, _payload = _post_peer(access_api, 'legacy_ip', '192.168.0.73', '64974')
    assert status.startswith('400')
    assert '192.168.0.73' not in _peer_neighbors(access_api)


def test_bgp_peer_neighbor_column_is_unique(db_session):
    """The database enforces the rule too, not only the API pre-check."""
    db_session.add(BgpPeer(
        name='uniq_a', local_as='65500', neighbor='192.168.0.81',
        neighbor_as='64981', status='active',
    ))
    db_session.commit()
    db_session.add(BgpPeer(
        name='uniq_b', local_as='65500', neighbor='192.168.0.81',
        neighbor_as='64982', status='active',
    ))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_validation_include_block_swaps_only_networks_conf(tmp_path, monkeypatch):
    from app.config_manager import ConfigManager
    from app.config_generator import (
        generate_split, render_managed_main_block,
    )

    staged = tmp_path / 'staged'
    live = tmp_path / 'live'
    live.mkdir()
    (tmp_path / 'bird.conf').write_text(render_managed_main_block(live) + '\n')
    monkeypatch.setenv('BIRD_MAIN_CONFIG', str(tmp_path / 'bird.conf'))
    monkeypatch.setenv('BIRD_BIN_PATH', '/usr/sbin/bird')
    generate_split(
        [{'prefix': '203.0.113.0/24', 'community_ids': [7], 'enabled': True}],
        [{'id': 7, 'name': '65500:7', 'values': '', 'enabled': True}],
        staged_dir=str(staged), managed_include_dir=str(live),
        router_id='203.0.113.254', local_as='65500',
    )
    manager = ConfigManager(str(live), str(staged), lambda: None)

    default_block = manager._validation_include_block()
    assert default_block.count('networks.conf') == 1
    assert 'routes_65500_7.conf' in default_block

    preview_file = '/tmp/preview-networks.conf'
    block = manager._validation_include_block(networks_path=preview_file)
    includes = [
        line.strip() for line in block.splitlines() if line.strip().startswith('include ')
    ]
    assert includes[0] == f'include "{preview_file}";'
    # Only networks.conf was replaced; the rest of the bundle stayed in place.
    assert f'include "{staged / "routes_65500_7.conf"}";' in includes
    assert f'include "{staged / "core.conf"}";' in includes
    assert len(includes) == len(
        [l for l in default_block.splitlines() if l.strip().startswith('include ')]
    )


def test_bird_error_file_parsing():
    from app.syntax_validator import SyntaxValidator

    b3 = 'bird: 2026-09-27 02:35:39.127 [0001] <FATAL> ' \
         '/var/lib/bird-manager/staged/core.conf:18:23 syntax error, unexpected CF_SYM_UNDEFINED'
    b2 = '/etc/bird/core.conf, line 42: syntax error'
    assert SyntaxValidator._bird_error_file(b3) == '/var/lib/bird-manager/staged/core.conf'
    assert SyntaxValidator._bird_error_line(b3) == 18
    assert SyntaxValidator._bird_error_file(b2) == '/etc/bird/core.conf'
    assert SyntaxValidator._bird_error_line(b2) == 42
    assert SyntaxValidator._bird_error_file('Network not found') is None
    assert SyntaxValidator._bird_error_file(None) is None
    assert SyntaxValidator._bird_error_line('Network not found') is None


def test_preview_shows_error_text_not_only_a_count():
    with open('static/js/app.js', 'r', encoding='utf-8') as stream:
        script = stream.read()

    block = script[script.index('function applyPreviewSyntax'):]
    block = block[:block.index('\n}')]
    assert "msg.textContent = syntax.errors.join('; ');" in block
    assert "'Ошибок: ' + syntax.errors.length" not in block


def test_config_block_titles_have_no_numbering():
    """Block titles in «Конфиг bird» are plain labels, not a numbered list."""
    with open('templates/dashboard.html', 'r', encoding='utf-8') as stream:
        dashboard = stream.read()

    editor = dashboard[dashboard.index('id="config-sections"'):]
    editor = editor[:editor.index('data-section="export"')] if 'data-section="export"' in editor else editor
    for title in (
        'staged/networks.conf — редактируемый, live | staged',
        '/etc/bird/live/bird-manager.conf — live | staged',
        '/etc/bird/live/core.conf — live | staged',
        'Остальные файлы — live | staged',
    ):
        assert f'>{title}</span>' in editor, title
    # The 1.1/1.2/1.3/1.4 prefixes are gone, and "до Apply" left the first title.
    for stale in (
        '1.1 ', '1.2 ', '1.3 ', '1.4 ',
        'редактируемый до Apply',
    ):
        assert stale not in editor, stale


# ---------------------------------------------------------------------------
# Diff counters and per-file diff view
# ---------------------------------------------------------------------------


def test_unified_diff_keeps_every_line_on_its_own_line():
    """Regression: Python 3.14 stopped appending newlines to diff lines.

    ``difflib.unified_diff`` yields the hunk body without a terminator, and the
    lines used to be joined as-is, so every ``+``/``-`` marker ended up mid-line
    and the change counters read zero for every file in the panel.
    """
    old = '\n'.join(f'route 10.0.{i}.0/24 via "x";' for i in range(20))
    new = '\n'.join(
        f'route 10.0.{i}.0/24 via "x";' if i not in (0, 10) else '# dropped'
        for i in range(20)
    )

    output = diff_contents(old, new)
    expected = ''.join(
        line if line.endswith('\n') else line + '\n'
        for line in difflib.unified_diff(
            old.splitlines(), new.splitlines(), fromfile='live', tofile='staged', n=3,
        )
    )

    assert output == expected
    lines = output.splitlines()
    assert lines[0] == '--- live'
    assert lines[1] == '+++ staged'
    assert sum(1 for line in lines if line.startswith('@@')) == 2
    added = [line for line in lines if line.startswith('+') and not line.startswith('+++')]
    removed = [line for line in lines if line.startswith('-') and not line.startswith('---')]
    assert added == ['+# dropped', '+# dropped']
    assert removed == ['-route 10.0.0.0/24 via "x";', '-route 10.0.10.0/24 via "x";']
    assert measure_rows(old, new) == {'adds': 2, 'dels': 2, 'total_rows': 20}


def test_file_diff_view_reports_exact_counts_and_caps_text(tmp_path):
    live = tmp_path / 'live'
    staged = tmp_path / 'staged'
    live.mkdir()
    staged.mkdir()
    (live / 'routes.conf').write_text(
        '\n'.join(f'route 10.{i}.0.0/16 via "x";' for i in range(2000))
    )
    (staged / 'routes.conf').write_text('# empty\n')

    manager = ConfigManager(str(live), str(staged), lambda: None)
    view = manager.get_file_diff_view('routes.conf')

    assert view['filename'] == 'routes.conf'
    # Counts stay exact even though the text is cut.
    assert view['dels'] == 2000
    assert view['adds'] == 1
    assert view['truncated'] is True
    assert view['returned_lines'] == view['total_lines'] - (view['total_lines'] - 400)
    assert view['returned_lines'] == 400
    assert len(view['diff'].encode('utf-8')) <= 64 * 1024
    assert view['diff'].startswith('--- live')
    assert '\n-' in view['diff']


def test_file_diff_view_is_untouched_for_a_small_file(tmp_path):
    live = tmp_path / 'live'
    staged = tmp_path / 'staged'
    live.mkdir()
    staged.mkdir()
    (live / 'core.conf').write_text('a\nb\nc\n')
    (staged / 'core.conf').write_text('a\nb\nd\n')

    view = ConfigManager(str(live), str(staged), lambda: None).get_file_diff_view('core.conf')

    assert view['truncated'] is False
    assert view['returned_lines'] == view['total_lines']
    assert (view['adds'], view['dels']) == (1, 1)


def test_file_diff_view_rejects_a_path_outside_the_bundle(tmp_path):
    manager = ConfigManager(str(tmp_path / 'live'), str(tmp_path / 'staged'), lambda: None)
    for name in ('../bird.conf', '/etc/passwd', 'networks.conf.bak', '', 'policy_x.conf'):
        assert manager.get_file_diff_view(name) is None, name


def test_file_diff_endpoint_is_read_only_and_validates_the_name(access_api):
    live = pathlib.Path(access_api._LIVE_PATH)
    staged = pathlib.Path(access_api._STAGED_PATH)
    live.mkdir(parents=True, exist_ok=True)
    staged.mkdir(parents=True, exist_ok=True)
    (live / 'networks.conf').write_text('router id 10.0.0.1;\n')
    (staged / 'networks.conf').write_text('router id 10.0.0.1;\n')

    status, _headers, raw, _stream, _cap = _wsgi_call(
        access_api, 'GET', '/api/config/file-diff', query='filename=networks.conf'
    )
    assert status.startswith('200')
    body = json.loads(raw)
    assert body['filename'] == 'networks.conf'
    assert body['diff'] == ''  # nothing staged yet, nothing to show
    assert body['truncated'] is False

    for name in ('../bird.conf', 'managed-files.json', 'policy_1.conf.bak'):
        status, _headers, raw, _stream, _cap = _wsgi_call(
            access_api, 'GET', '/api/config/file-diff', query=f'filename={name}'
        )
        assert status.startswith('400'), name
        assert json.loads(raw)['error'] == 'Unknown bundle file'

    status, _headers, _raw, _stream, _cap = _wsgi_call(
        access_api, 'GET', '/api/config/file-diff'
    )
    assert status.startswith('400')


def test_publish_audit_keeps_counts_but_not_the_whole_bundle_diff(tmp_path, monkeypatch):
    """The audit row must not grow by a megabyte on every apply.

    A populated bundle diffs to hundreds of kilobytes of text; the journal only
    needs the per-file counts, because that is the warning the operator reads
    before a publish drops every prefix.
    """
    live = tmp_path / 'live'
    staged = tmp_path / 'staged'
    live.mkdir()
    staged.mkdir()
    main = tmp_path / 'bird.conf'
    main.write_text(render_managed_main_block(live) + '\n')
    monkeypatch.setenv('BIRD_MAIN_CONFIG', str(main))
    monkeypatch.setenv('BIRD_BIN_PATH', '/usr/sbin/bird')
    monkeypatch.setenv('BIRDC_BIN_PATH', '/bin/true')

    generate_split(
        [{'prefix': '203.0.113.0/24', 'community_ids': [7], 'enabled': True}],
        [{'id': 7, 'name': '65500:102', 'values': '', 'enabled': True}],
        staged_dir=str(staged), managed_include_dir=str(live), router_id='203.0.113.254',
        local_as='65500',
    )
    # A live bundle that still carries a full table.
    manager = ConfigManager(str(live), str(staged), lambda: None)
    manager.publish_staged_generated()
    for name in ('routes.conf', 'export.conf'):
        (live / name).write_text(
            '\n'.join(f'route 10.{i // 256}.{i % 256}.0/24 via "x";' for i in range(4000))
        )

    result = manager.publish_staged_generated()
    published = result['diff']

    assert 'routes.conf +' in published
    assert 'export.conf +' in published
    assert len(published) < 8 * 1024
    counts_line = published.splitlines()[0]
    assert 'routes.conf +' in counts_line and 'export.conf +' in counts_line
    assert len(published.splitlines()) <= 41


# ---------------------------------------------------------------------------
# Side-by-side line model
# ---------------------------------------------------------------------------


def test_aligned_rows_pairs_both_sides_line_by_line():
    """Every row carries both sides, so the columns align by index.

    A unified diff cannot be aligned with the file it describes: it carries
    three header lines and its own line count. Pairing the texts directly is
    what makes the two columns the same height by construction.
    """
    model = aligned_rows(
        'router id 1.1.1.1;\nfilter f\n{\n}\nlog stderr all;\n',
        'router id 2.2.2.2;\nfilter f\n{\n}\n',
    )

    assert [row['kind'] for row in model['rows']] == [
        'changed', 'context', 'context', 'context', 'removed',
    ]
    assert model['rows'][0] == {
        'old': 'router id 1.1.1.1;', 'new': 'router id 2.2.2.2;', 'kind': 'changed',
    }
    # A line missing on one side is None, not an empty string: an empty line is
    # a real line and must not be confused with an absent one.
    assert model['rows'][4]['new'] is None
    assert model['adds'] == 1
    assert model['dels'] == 2
    assert model['total_rows'] == 5


def test_aligned_rows_covers_longer_side_of_a_replacement():
    model = aligned_rows('a\nb\nc\n', 'a\nB\nC\nD\n')
    # difflib pairs b,c with B,C and appends D, so the spill-over shows up as a
    # changed pair followed by an added row — no line is dropped either way.
    assert [row['kind'] for row in model['rows']] == [
        'context', 'changed', 'changed', 'added',
    ]
    # A modified line counts once on each side, like a unified diff does.
    assert model['adds'] == 3 and model['dels'] == 2


def test_aligned_rows_agree_with_unified_diff_counts():
    """The header counters and the row model must not drift apart."""
    cases = [
        ('a\nb\nc\n', 'a\nb\nc\n'),
        ('a\nb\nc\nd\ne\n', 'a\nB\nc\ne\n'),
        ('a\nb\n', 'a\nb\nc\nd\n'),
        ('a\nb\nc\nd\n', 'a\nd\n'),
    ]
    for old, new in cases:
        model = aligned_rows(old, new)
        # Count the unified diff independently of the model, skipping the two
        # file markers by position: a removed line whose content starts with
        # ``--`` must not be mistaken for a header.
        markers = ('--- live', '+++ staged')
        adds = dels = 0
        for index, line in enumerate(diff_contents(old, new).splitlines()):
            if index < 2 and line == markers[index]:
                continue
            if line.startswith('@@'):
                continue
            if line.startswith('+'):
                adds += 1
            elif line.startswith('-'):
                dels += 1
        assert (adds, dels) == (model['adds'], model['dels']), (old, new)


def test_aligned_rows_max_rows_caps_text_but_not_counters():
    source = '\n'.join(f'route 10.{i}.0.0/16;' for i in range(50))
    capped = aligned_rows(source, '', 10)
    assert len(capped['rows']) == 10
    assert capped['returned_rows'] == 10
    assert capped['truncated'] is True
    assert capped['total_rows'] == 50
    assert capped['adds'] == 0 and capped['dels'] == 50
    full = aligned_rows(source, '')
    assert full['truncated'] is False
    assert len(full['rows']) == 50
    assert (full['adds'], full['dels'], full['total_rows']) == (
        capped['adds'], capped['dels'], capped['total_rows'],
    )


def test_measure_rows_agrees_with_the_row_model():
    """Counters and rows come from one walk, so they cannot drift apart."""
    cases = [
        ('', ''),
        ('a\nb\nc\n', 'a\nb\nc\n'),
        ('a\nb\nc\nd\ne\n', 'a\nB\nc\ne\n'),
        ('a\nb\n', 'a\nb\nc\nd\n'),
        ('a\nb\nc\nd\n', 'a\nd\n'),
        # Content that looks like a diff header must still be counted as content.
        ('-- not a header\na\n', 'a\n'),
        ('+++ plus\na\n', 'a\n'),
    ]
    for old_text, new_text in cases:
        model = aligned_rows(old_text, new_text)
        assert measure_rows(old_text, new_text) == {
            'adds': model['adds'], 'dels': model['dels'],
            'total_rows': model['total_rows'],
        }, (old_text, new_text)


def test_overview_on_a_large_file_stays_fast(tmp_path, monkeypatch):
    """Regression: a 31k-line bundle file made the overview take 37 s.

    The row model disabled difflib's autojunk heuristic, which degenerates on
    the many near-identical lines of a generated policy or export file, and the
    overview built the whole model for every file just to read three integers.
    Only a wall-clock bound catches that class of regression, so the budget is
    deliberately loose: the real cost is a few hundred milliseconds.
    """
    live = tmp_path / 'live'
    staged = tmp_path / 'staged'
    live.mkdir()
    staged.mkdir()
    # Shaped like the real generated filter: per prefix four lines, three of
    # which repeat thousands of times. That repetition is what makes difflib's
    # autojunk heuristic matter, and a fixture of unique lines would not have
    # caught the regression at all.
    def prefix_block(index):
        return (
            f'        if net = 10.{index // 256}.{index % 256}.0/24 then {{',
            '            bgp_community.add((65500, 101));',
            '            accept;',
            '        }',
        )

    big = '\n'.join(
        line for i in range(10400) for line in prefix_block(i)
    )
    assert big.count('bgp_community.add') > 10000
    for directory in (live, staged):
        (directory / 'export.conf').write_text(big + '\n')
    (live / 'core.conf').write_text('a\n')
    (staged / 'core.conf').write_text('b\n')

    manager = ConfigManager(str(live), str(staged), lambda: None)
    started = time.perf_counter()
    overview = manager.get_config_overview()
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0, f'overview took {elapsed:.1f}s on a 31k-line file'
    row = next(r for r in overview['summary'] if r['filename'] == 'export.conf')
    assert row['rows'] == 41600
    assert row['adds'] == 0 and row['dels'] == 0
    assert overview['core']['dels'] == 1


def test_overview_survives_a_file_that_is_not_utf8(tmp_path, monkeypatch):
    """One unreadable file must not turn the whole overview into a 500.

    A stray binary in the live directory used to raise UnicodeDecodeError out of
    the per-file loop, which cost the operator every counter in the panel.
    """
    live = tmp_path / 'live'
    staged = tmp_path / 'staged'
    live.mkdir()
    staged.mkdir()
    (live / 'core.conf').write_bytes(b'\xff\xfe\x00binary\xac junk')
    (staged / 'core.conf').write_text('a\n')
    (live / 'routes.conf').write_text('route 10.0.0.0/8;\n')
    (staged / 'routes.conf').write_text('route 10.0.0.0/8;\n')

    manager = ConfigManager(str(live), str(staged), lambda: None)
    overview = manager.get_config_overview()

    assert overview['core']['readable'] is False
    assert overview['core']['adds'] == 0 and overview['core']['dels'] == 0
    assert overview['networks']['readable'] is True
    routes = next(r for r in overview['summary'] if r['filename'] == 'routes.conf')
    assert routes['readable'] is True
    assert routes['adds'] == 0


def test_file_rows_reports_an_unreadable_file(tmp_path):
    live = tmp_path / 'live'
    staged = tmp_path / 'staged'
    live.mkdir()
    staged.mkdir()
    (live / 'core.conf').write_bytes(b'\xac not utf-8')
    (staged / 'core.conf').write_text('a\n')

    view = ConfigManager(str(live), str(staged), lambda: None).get_file_rows_view('core.conf')

    assert view['readable'] is False
    assert view['rows'] == []


def test_overview_reports_row_count_per_file(tmp_path, monkeypatch):
    live = tmp_path / 'live'
    staged = tmp_path / 'staged'
    live.mkdir()
    staged.mkdir()
    main = tmp_path / 'bird.conf'
    main.write_text(render_managed_main_block(live) + '\n')
    monkeypatch.setenv('BIRD_MAIN_CONFIG', str(main))
    monkeypatch.setenv('BIRD_BIN_PATH', '/usr/sbin/bird')
    # publish_staged_generated() reloads BIRD through birdc. The config paths
    # above are redirected to tmp_path, but birdc talks to the real daemon on
    # its control socket, so the test would reconfigure the running bird — and
    # overwrite the "Last reconfiguration" timestamp the panel reports. /bin/true
    # accepts the command without contacting anything.
    monkeypatch.setenv('BIRDC_BIN_PATH', '/bin/true')
    generate_split(
        [{'prefix': '203.0.113.0/24', 'community_ids': [7], 'enabled': True}],
        [{'id': 7, 'name': '65500:102', 'values': '', 'enabled': True}],
        staged_dir=str(staged), managed_include_dir=str(live),
        router_id='203.0.113.254', local_as='65500',
    )
    manager = ConfigManager(str(live), str(staged), lambda: None)
    manager.publish_staged_generated()
    identical = 'a\nb\nc\nd\n'
    (live / 'core.conf').write_text(identical)
    (staged / 'core.conf').write_text(identical)

    overview = manager.get_config_overview()

    # The panel needs the row count from the overview to decide whether a file
    # is small enough to open; it must not have to fetch the rows first.
    assert overview['core']['rows'] == 4
    assert overview['core']['adds'] == 0
    assert overview['core']['dels'] == 0
    assert overview['networks']['rows'] == len(
        (staged / 'networks.conf').read_text().splitlines()
    )
    for row in overview['summary']:
        assert row['rows'] >= 0


def test_file_rows_endpoint_serves_aligned_rows(access_api):
    live = pathlib.Path(access_api._LIVE_PATH)
    staged = pathlib.Path(access_api._STAGED_PATH)
    live.mkdir(parents=True, exist_ok=True)
    staged.mkdir(parents=True, exist_ok=True)
    (live / 'core.conf').write_text('one\ntwo\nthree\n')
    (staged / 'core.conf').write_text('one\nTWO\nthree\n')

    status, _headers, raw, _stream, _cap = _wsgi_call(
        access_api, 'GET', '/api/config/file-rows', query='filename=core.conf'
    )
    assert status.startswith('200')
    body = json.loads(raw)
    assert body['filename'] == 'core.conf'
    assert [row['kind'] for row in body['rows']] == ['context', 'changed', 'context']
    assert body['adds'] == 1 and body['dels'] == 1
    assert body['total_rows'] == 3
    assert body['truncated'] is False

    for name in ('../bird.conf', 'managed-files.json', 'policy_x.conf', 'anything'):
        status, _headers, raw, _stream, _cap = _wsgi_call(
            access_api, 'GET', '/api/config/file-rows', query=f'filename={name}'
        )
        assert status.startswith('400'), name
        assert json.loads(raw)['error'] == 'Unknown bundle file'

    status, _headers, _raw, _stream, _cap = _wsgi_call(
        access_api, 'GET', '/api/config/file-rows'
    )
    assert status.startswith('400')


def test_preview_returns_aligned_rows_in_split_mode(access_api):
    """Regression: the split layout returned an empty change list.

    build_editor_view short-circuited to ``content, []`` outside the monolithic
    layout, so editing a line in networks.conf showed nothing at all in the
    right pane. The live→merged model is what the pane renders.
    """
    live = pathlib.Path(access_api._LIVE_PATH)
    staged = pathlib.Path(access_api._STAGED_PATH)
    live.mkdir(parents=True, exist_ok=True)
    staged.mkdir(parents=True, exist_ok=True)
    original = '# head\nrouter id 192.0.2.1;\n# tail\n'
    (live / 'networks.conf').write_text(original)
    (staged / 'networks.conf').write_text(original)
    manager = access_api.config_manager
    manager.split_mode = True

    edited = original.replace('192.0.2.1', '198.51.100.7')
    status, _headers, raw, _stream, _cap = _wsgi_call(
        access_api, 'POST', '/api/config/preview',
        body=json.dumps({'filename': 'networks.conf', 'content': edited}).encode(),
    )
    assert status.startswith('200')
    body = json.loads(raw)
    assert [row['kind'] for row in body['rows']] == [
        'context', 'changed', 'context',
    ]
    assert body['rows'][1]['old'] == 'router id 192.0.2.1;'
    assert body['rows'][1]['new'] == 'router id 198.51.100.7;'


def test_editor_keeps_alignment_by_adding_a_ghost_row(access_api):
    """A deleted line leaves a ghost row, so lower rows keep their numbers."""
    live = pathlib.Path(access_api._LIVE_PATH)
    staged = pathlib.Path(access_api._STAGED_PATH)
    live.mkdir(parents=True, exist_ok=True)
    staged.mkdir(parents=True, exist_ok=True)
    original = 'one\ntwo\nthree\n'
    (live / 'networks.conf').write_text(original)
    (staged / 'networks.conf').write_text(original)
    manager = access_api.config_manager
    manager.split_mode = True

    editor_content, rows = manager.build_editor_view(
        'networks.conf', 'one\nthree\n', 'one\nthree\n'
    )

    assert [row['kind'] for row in rows] == ['context', 'removed', 'context']
    assert rows[1]['old'] == 'two' and rows[1]['new'] is None
    # The dropped line occupies a row in the editor text, so "three" stays on
    # row 3 on both sides instead of sliding up to row 2.
    editor_lines = editor_content.rstrip('\n').split('\n')
    assert editor_lines[1] == GHOST_MARKER
    assert editor_lines[2] == 'three'
    assert len(editor_lines) == len(rows) == 3


def test_row_view_markup_and_lazy_sections():
    """Two columns of fixed-pitch rows, and no section open on load."""
    dashboard = pathlib.Path('templates/dashboard.html').read_text(encoding='utf-8')
    css = pathlib.Path('static/css/app.css').read_text(encoding='utf-8')
    script = pathlib.Path('static/js/app.js').read_text(encoding='utf-8')

    assert 'class="row-view row-view-scroll"' in dashboard
    assert 'id="editor-rows"' in dashboard
    assert 'id="core-rows"' in dashboard
    assert 'id="manager-conf-rows"' in dashboard
    assert 'class="row-view row-view-single"' in dashboard
    # Every file section starts collapsed: the header carries the counters only.
    # The BIRD tab now ends at the editor panel, since the add-peer form moved.
    sections = dashboard[dashboard.index('id="config-sections"'):dashboard.index('data-tab-panel="system"')]
    assert 'config-section expanded' not in sections
    assert 'aria-expanded="true"' not in sections
    for selector in (
        '.row-view', '.row-line', '.row-cell', '.row-old', '.row-new',
        '.row-added', '.row-removed', '.row-changed', '.row-note',
    ):
        assert selector in css, selector
    # The overlay that could not follow the textarea is gone.
    assert '.diff-overlay' not in css
    assert '.diff-row' not in css
    assert 'renderDiffOverlay' not in script
    for name in ('renderRowView', 'rowCell', 'openFileSection', 'countsFromRows'):
        assert 'function ' + name in script, name
    assert 'const ROW_VIEW_MAX_ROWS = 5000;' in script
    # Rows come from the server model; the client never recomputes an alignment.
    assert 'data.rows' in script
    assert "/api/config/file-rows?filename=" in script
    assert "'/api/config/file-diff?filename=" not in script


def strip_js_comments(source):
    """Comments explain the refresh path, so the assertions below must look at
    code only — otherwise a comment naming the old guard would satisfy them."""
    without_blocks = re.sub(r'/\*.*?\*/', ' ', source, flags=re.S)
    return re.sub(r'//[^\n]*', ' ', without_blocks)


def test_networks_tab_splits_lists_and_owns_the_bgp_peer_form():
    """The DB-backed lists sit side by side; the add-peer form moved here.

    Both lists start collapsed and load on first expand, so a hidden tab costs
    nothing. The add-peer form came from the BIRD tab unchanged — it is a form
    over the same table the peer list reads, and belongs beside the other
    database-managed panels.
    """
    dashboard = pathlib.Path('templates/dashboard.html').read_text(encoding='utf-8')
    css = pathlib.Path('static/css/app.css').read_text(encoding='utf-8')
    script = pathlib.Path('static/js/app.js').read_text(encoding='utf-8')

    networks = dashboard[dashboard.index('data-tab-panel="networks"'):dashboard.index('data-tab-panel="bird"')]

    # The row holds both lists, each taking half the width.
    assert 'class="tab-row"' in networks
    row = networks[networks.index('class="tab-row"'):networks.index('id="panel-bulk"')]
    assert row.index('id="panel-networks"') < row.index('id="panel-peers"')
    assert 'class="panel panel-peers" id="panel-peers"' in row
    layout = css[css.index('.tab-row {'):css.index('.tab-row {') + 200]
    assert 'display: flex' in layout and 'gap: 16px' in layout
    both = css[css.index('.tab-row > .panel {'):css.index('.tab-row > .panel {') + 80]
    assert 'flex: 1 1 50%' in both, 'each panel gets half the row width'
    # Measured floor, not zero: a zero basis let the panel shrink under the
    # width its editing row needs.
    assert 'min-width: 490px' in both, 'panel must hold its editor width'

    # Both panels collapse to their header instead of sharing the height.
    for cls in ('.panel-hierarchy,', '.panel-peers,'):
        assert cls in css
    collapsed = css[css.index('.panel-hierarchy:not(.expanded),'):]
    collapsed = collapsed[:collapsed.index('{')]
    assert '.panel-peers:not(.expanded)' in collapsed

    # Collapsed by default, with a counter in the header and a hidden body.
    for panel, count, body in (
        ('panel-peers', 'bgp-peers-count', 'bgp-peers-body'),
        ('panel-networks', 'networks-count', 'hierarchy-body'),
    ):
        assert f'id="{panel}"' in networks
        assert f'id="{count}"' in networks, count
        # class comes before id in the markup, so match the attributes loosely.
        assert f'class="panel-body hidden" id="{body}"' in networks, body

    # The form sits below the full-width network form, on the same tab.
    assert networks.index('id="panel-bulk"') < networks.index('id="panel-bgp"')

    # Same lazy contract as the network tree: expand loads, collapse drops rows.
    for name in ('togglePeersPanel', 'loadBgpPeers', 'renderBgpPeerList', 'buildPeerRow'):
        assert 'function ' + name in script, name
    toggle = script[script.index('function togglePeersPanel'):]
    toggle = toggle[:toggle.index('function renderBgpPeerList')]
    assert 'if (bgpPeersLoaded) renderBgpPeerList(bgpPeersCache); else loadBgpPeers();' in toggle
    assert "body.innerHTML = '';" in toggle, 'collapse must drop the rows'
    assert 'loadBgpPeers();' in script[script.index('async function refreshDashboard'):]

    # The list is pure DB data; the live birdc snapshot stays on Система.
    panel = script[script.index('async function loadBgpPeers'):]
    panel = panel[:panel.index('function togglePeersPanel')]
    assert "'/api/bgp_peers'" in panel
    assert '/api/bird/protocols' not in panel
    # Delete goes through the existing endpoint and refreshes the visible state.
    del_call = script[script.index('function buildPeerRow'):]
    del_call = del_call[:del_call.index('const PEER_COLUMNS')]
    assert "method: 'DELETE'" in del_call
    assert "'/api/bgp_peers/' + peer.id" in del_call
    assert 'await refreshDashboard();' in del_call


def test_peer_panel_is_not_double_bound():
    """Regression: the peer toggle had two handlers, so it never opened.

    initCollapsiblePanels binds every .panel-toggle except the network tree,
    whose own handler does the lazy load. The peer panel was added later and
    needed the same exemption: with both handlers bound, one expanded the panel
    and the other collapsed it again in the same click. The list was fetched
    into a hidden body — the request succeeded and nothing appeared.
    """
    script = pathlib.Path('static/js/app.js').read_text(encoding='utf-8')

    guard = script[script.index('function initCollapsiblePanels'):]
    guard = guard[:guard.index('btn.dataset.toggleBound')]
    assert "btn.closest('#panel-networks') || btn.closest('#panel-peers')" in guard, guard

    # Each of the two list panels must have exactly one toggle handler.
    for panel, handler in (('panel-networks', 'toggleHierarchyPanel'),
                           ('panel-peers', 'togglePeersPanel')):
        assert f"document.querySelector('#{panel} .panel-toggle')" in script, panel
        assert f'{handler}();' in script, handler
    assert "document.querySelector('#panel-peers .panel-toggle')" in script
    # The generic binder must exempt both list panels, in a single condition:
    # two separate early returns would let the second one drift.
    assert guard.count('btn.closest(') == 2, guard
    assert 'return;' in guard[guard.index("btn.closest('#panel-networks')"):][:120]


def test_tab_row_panels_keep_half_the_width_when_collapsed():
    """Regression: collapsed panels shrank to their content.

    .panel-hierarchy:not(.expanded) / .panel-peers:not(.expanded) set
    flex: 0 0 auto. Inside .tab-row that made a collapsed panel as narrow as
    its header, and expanding one gave all the free width to it, pushing the
    other to the far edge. A 50% flex-basis fixes the width independently of
    the expanded state.

    The rule has to follow the :not(.expanded) block: :not() adds no
    specificity of its own, so both selectors weigh the same and source order
    alone decides which one wins.
    """
    css = pathlib.Path('static/css/app.css').read_text(encoding='utf-8')

    collapse_at = css.index('.panel-hierarchy:not(.expanded),')
    collapse_end = css.index('}', css.index('.panel-config-status:not(.expanded)'))
    row_rule_at = css.index('.tab-row > .panel {')

    assert row_rule_at > collapse_end, (
        '.tab-row > .panel must come after the :not(.expanded) rules, '
        'otherwise they win on source order and shrink the panel')

    rule = css[row_rule_at:row_rule_at + 80]
    assert 'flex: 1 1 50%' in rule, rule

    # The basis must be width-based, not grow-based.
    assert 'flex: 1 1 0' not in rule
    assert '.tab-row { display: flex; gap: 16px;' in css
    # Exactly one definition of the row-child rule: an earlier, weaker copy
    # would be dead code and would hide the real reason for the fix.
    assert css.count('.tab-row > .panel') == 1


def test_network_panels_hold_the_measured_editor_width():
    """The tab must not squeeze the network tree under its editing row.

    Measured in Chromium at 2560x1440: the editing row needs 490px (prefix 151 +
    description 140 + status select 37 + enabled checkbox 42 + Сохранить 71 +
    Удалить 81, plus gaps and padding). At that width the editor wraps onto
    four lines and clips nothing, so 490 is a readability floor rather than a
    fit-or-scroll one.

    The floor has to sit on the row itself, not only on the panel: the row is a
    flex item and shrinks together with the panel, so a panel-only min-width
    does not stop the editor from being squeezed.
    """
    css = pathlib.Path('static/css/app.css').read_text(encoding='utf-8')

    rule = css[css.index('.tab-row > .panel {'):css.index('.tab-row > .panel {') + 90]
    assert 'min-width: 490px' in rule, rule
    assert 'flex: 1 1 50%' in rule, 'the floor must not reintroduce content sizing'

    row = css[css.index('.panel-hierarchy .network-row {'):]
    row = row[:row.index('}')]
    assert 'min-width: 490px' in row, row
    assert 'flex-wrap: wrap' in row, 'the editor wraps instead of overflowing'

    # The tab itself: two panels + the row gap + the outer padding, so the
    # percentage rule cannot squeeze past the floor. 2*490 + 16 + 34 = 1030.
    tabs = css[css.index('.tabs {'):css.index('}', css.index('.tabs {'))]
    assert 'min-width: 1030px' in tabs, tabs
    assert 'width: 75%' in tabs, 'the percentage rule stays as the upper bound'


def test_peer_header_and_rows_line_up():
    """Regression: every value column sat left of its header by 79px.

    Measured in Chromium: all four columns were shifted by exactly the width of
    the «Удалить» button (79px), while their widths matched the header's. The
    cause was .peer-act: with flex: 0 0 auto the empty span in the header
    measured 0 while the button in a row measured 79px, so the row's basis sum
    was 79px larger and .peer-name (flex: 1 1 auto) absorbed the difference,
    shifting everything after it. Pinning the track in both places fixes it;
    flex: 1 1 0 on .peer-name does not.
    """
    css = pathlib.Path('static/css/app.css').read_text(encoding='utf-8')

    act = css[css.index('.peer-act {'):css.index('.peer-act {') + 60]
    assert 'flex: 0 0 79px' in act, act
    assert 'flex: 0 0 auto' not in act, 'auto let the empty header span collapse to 0'

    # Header and rows share the floor, so neither can be narrower than the set.
    # Declared once, next to the column widths it belongs with.
    assert '.peer-act { flex: 0 0 79px; }' in css, 'the pinned track must exist'
    # Declared exactly once: a duplicated floor in a trailing block would be
    # dead weight that hides which rule actually applies.
    assert css.count('.peer-head, .peer-row { min-width: 660px; }') == 1
    assert css.count('.peer-act { flex: 0 0 79px; }') == 1


def test_bgp_peers_render_one_row_per_peer():
    """One line per peer, with column headers, reusing the shared status badge.

    A single line without headers is unreadable as soon as there are two peers:
    a bare "64999" next to "65500" says nothing on its own.
    """
    css = pathlib.Path('static/css/app.css').read_text(encoding='utf-8')
    script = pathlib.Path('static/js/app.js').read_text(encoding='utf-8')

    for name in ('buildPeerHead', 'buildPeerRow', 'DB_PEER_COLUMNS'):
        assert name in script, name
    # Header and rows must share the column classes to stay aligned.
    head = script[script.index('function buildPeerHead'):script.index('function buildPeerRow')]
    row = script[script.index('function buildPeerRow'):script.index('const PEER_COLUMNS')]
    assert 'DB_PEER_COLUMNS.forEach' in head
    cols = script[script.index('const DB_PEER_COLUMNS'):]
    cols = cols[:cols.index('];')]
    for cls in ('peer-col-status', 'peer-col-as', 'peer-col-nb', 'peer-col-nbas'):
        assert cls in cols, cls          # declared once, shared by header and rows
        assert 'col.cls' in head, cls
        assert cls in row, cls           # rendered by the row
        assert '.' + cls in css, cls
    for title in ('Peer id', 'm086', 'Local AS', 'Neighbor', 'Neighbor AS'):
        assert title in cols or title in head, title
    # Status reuses the shared badge helper rather than its own markup, so a peer
    # looks like any other status in the app. The badge classes it emits come
    # from STATUS_BADGES, which must therefore still exist.
    assert 'statusBadge(peer.status)' in row
    assert "insertAdjacentHTML('beforeend', statusBadge" in row
    assert 'const STATUS_BADGES' in script
    assert 'badge-pending' in script and 'badge-applied' in script
    assert 'STATUS_BADGES[status]' in script
    # The old stacked card must be gone from both files.
    assert 'peer-card' not in css and 'peer-card' not in script
    assert 'peer-row' in css and 'peer-row' in script

    # Columns are fixed width; only the peer id grows.
    name_rule = css[css.index('.peer-name {'):css.index('.peer-name {') + 80]
    assert 'flex: 1 1 auto' in name_rule, name_rule


def test_bgp_peer_count_matches_the_list_length():
    """The header counter must agree with what the list would render."""
    import app.routes as routes
    import app.crud_operations as crud

    assert any(e['pattern'] == '/api/bgp_peers/count' and e['method'] == 'GET'
               for e in routes.ROUTES)
    # The count endpoint cannot be shadowed by the peer_id route.
    import re as _re

    def resolves(path):
        for entry in routes.ROUTES:
            parts = _re.findall(r'<int:(\w+)>|<str:(\w+)>|([^<]*)', entry['pattern'])
            rx = ''.join(r'\d+' if a or b else _re.escape(c) for a, b, c in parts)
            if _re.match('^' + rx + '$', path):
                return entry['pattern']
        return None

    assert resolves('/api/bgp_peers/count') == '/api/bgp_peers/count'
    assert resolves('/api/bgp_peers/7') == '/api/bgp_peers/<int:peer_id>'


def test_entering_the_bird_tab_recomputes_the_diff():
    """Entering the BIRD tab must recompute, not replay what is on screen.

    ensureTabLoaded is a one-shot guard on loadedTabs, so routing the tab
    through it made every entry after the first a no-op: the counters kept up
    (the change handlers call the overview) while an open pane kept the rows it
    was rendered with. The row cache is keyed on filename plus counters, which
    cannot see content that changed behind unchanged +1 -1, so the recompute
    clears it instead of trusting the key.
    """
    script = strip_js_comments(pathlib.Path('static/js/app.js').read_text(encoding='utf-8'))

    for name in ('refreshBirdTab', 'clearFileRowsCache', 'collectExpandedConfigSections',
                 'reopenSummaryViews', 'refreshOpenConfigPanes',
                 'openSummaryFileView', 'closeSummaryFileView'):
        assert 'function ' + name in script, name

    # activateTab must not fall through to the one-shot loader for this tab.
    activate = script[script.index('async function activateTab'):]
    activate = activate[:activate.index('function wireTabs')]
    assert 'await refreshBirdTab();' in activate
    # Only the bird branch is asserted here; the system branch also bypasses the
    # one-shot loader for its own reason, covered separately.
    bird = activate[activate.index("if (name === 'bird')"):]
    bird = bird[:bird.index('await ensureTabLoaded')]
    assert 'ensureTabLoaded' not in bird
    # The manual refresh button means the same thing and must reuse the path.
    assert "await refreshBirdTab();" in script[script.index('async function refreshDashboard'):]

    # A recompute always reaches the server, and parallel entries share one pass.
    refresh = script[script.index('function refreshBirdTab'):]
    refresh = refresh[:refresh.index('async function loadTabContent')]
    assert 'clearFileRowsCache();' in refresh
    assert 'loadConfigOverview()' in refresh
    assert 'reopenSummaryViews()' in refresh
    assert 'refreshOpenConfigPanes()' in refresh
    assert 'birdRefreshInFlight' in refresh


def test_a_hash_change_activates_the_tab_once():
    """Both hashchange and popstate arrive, and handleRoute is bound to both.

    Measured on this Chromium: three hash changes produced three hashchange and
    three popstate events — one each, neither doubled on its own — so the
    binding alone ran the tab twice per change. Сети and BIRD hid it behind
    their own guards, but the System tab reloads on entry by design and fetched
    everything twice.
    """
    script = strip_js_comments(pathlib.Path('static/js/app.js').read_text(encoding='utf-8'))

    route = script[script.index('let lastHandledLocation'):]
    route = route[:route.index("document.addEventListener('DOMContentLoaded'")]

    # The guard keys on the whole location, not the hash alone, so it survives
    # routing that grows a path component.
    assert 'window.location.pathname' in route
    assert 'window.location.search' in route
    assert 'window.location.hash' in route
    assert 'if (location === lastHandledLocation) return undefined;' in route
    assert route.index('lastHandledLocation = location;') < route.index(
        'return activateTab(tabFromHash(), false);'
    )
    # ...and it must be initialised, or the first event would be swallowed.
    assert 'let lastHandledLocation = null;' in route

    # Both listeners stay: dropping popstate would break history traversal the
    # day routing grows a path.
    assert "window.addEventListener('popstate', handleRoute);" in script
    assert "window.addEventListener('hashchange', handleRoute);" in script

    # The guard stays out of activateTab, otherwise clicking the tab that is
    # already open would stop refreshing it.
    activate = script[script.index('async function activateTab'):]
    activate = activate[:activate.index('function wireTabs')]
    assert 'lastHandledLocation' not in activate, 'the guard belongs in handleRoute'
    # A "already on this tab" early return would look harmless but silently stop
    # re-clicking the open tab from refreshing it.
    assert 'name === currentTab' not in activate
    assert activate.index('currentTab = name;') < activate.index('ensureTabLoaded')
    # ...and the direct callers still bypass it.
    assert "button.addEventListener('click', () => activateTab(button.dataset.tab, true));" in script
    assert 'activateTab(buttons[next].dataset.tab, true);' in script


def test_entering_the_system_tab_reloads_and_restarts_the_poll():
    """Returning to Система must fetch again, not replay the first visit.

    ensureTabLoaded is a one-shot guard, so every entry after the first returned
    immediately: the session table, the configuration status, the backup list and
    the audit log kept whatever they were first rendered with. The auto-poll made
    it worse — PeersPoll.stop() runs when leaving the tab and only loadBirdPeers()
    restarts it, so the 30s timer stayed dead too.

    Bypassing that guard is what makes the double activation observable here:
    every hash change runs the tab twice (see
    test_a_hash_change_activates_the_tab_once), and nothing folds the second run
    in any more.
    """
    script = strip_js_comments(pathlib.Path('static/js/app.js').read_text(encoding='utf-8'))

    activate = script[script.index('async function activateTab'):]
    activate = activate[:activate.index('function wireTabs')]
    system = activate[activate.index("if (name === 'system')"):]
    system = system[:system.index('await ensureTabLoaded')]
    assert 'ensureTabLoaded' not in system, 'the one-shot guard would swallow the entry'
    assert 'await loadTabContent(name);' in system
    assert 'loadedTabs.add(name);' in system

    # The reload path is what brings the poll back.
    load_peers = script[script.index('async function loadBirdPeers'):]
    load_peers = load_peers[:load_peers.index('function renderConfigStatus')]
    assert 'PeersPoll.reset();' in load_peers
    # ...and reset only starts a timer when the panel is actually visible.
    poll = script[script.index('const PeersPoll = {'):]
    poll = poll[:poll.index('async tick')]
    assert 'if (!peersPanelVisible()) return;' in poll

    # The manual refresh keeps the same meaning for this tab.
    manual = script[script.index('async function refreshDashboard'):]
    assert 'await loadBirdPeers();' in manual
    assert 'await loadConfigStatus();' in manual
    assert 'await loadAudit();' in manual


def test_secondary_panels_start_collapsed():
    """Five panels open folded; only the live BIRD sessions block opens.

    All three aspects matter, not just the missing ``expanded`` class: the toggle
    handler derives state from ``hidden`` on the body
    (``isExpanded = !body.classList.contains('hidden')``), so a section left
    without the class but with a visible body would collapse on the first click
    instead of expanding.
    """
    dashboard = pathlib.Path('templates/dashboard.html').read_text(encoding='utf-8')

    collapsed = {
        'panel-bulk': 'bulk-body',
        'panel-bgp': 'bgp-body',
        'panel-config-status': 'config-status-body',
        'panel-backup': 'backup-body',
        'panel-audit': 'audit-body',
    }
    for panel_id, body_id in collapsed.items():
        section = re.search(
            r'<section class="panel[^"]*" id="%s">.*?</section>' % panel_id,
            dashboard, re.S,
        )
        assert section, panel_id
        block = section.group(0)
        opening = block.split('>', 1)[0]
        assert 'expanded' not in opening, panel_id + ' still carries expanded'
        assert 'hidden' not in opening, panel_id + ' section must not be display:none'
        assert 'aria-expanded="false"' in block, panel_id
        body_tag = re.search(r'<div class="panel-body[^"]*" id="%s"' % body_id, block)
        assert body_tag, body_id
        assert 'hidden' in body_tag.group(0), body_id + ' body is not hidden'

    # The one panel that must stay open: live state the operator watches.
    bird = re.search(
        r'<section class="panel[^"]*" id="panel-bgp-sessions"[^>]*>.*?</section>',
        dashboard, re.S,
    )
    assert bird, 'panel-bgp-sessions must still be in the markup'
    block = bird.group(0)
    opening = block.split('>', 1)[0]
    assert 'expanded' in opening, 'the live sessions panel must stay open'
    assert 'aria-expanded="true"' in block
    assert 'hidden' not in opening, 'a hidden section cannot be opened at all'


def test_open_bird_peers_panel_takes_the_free_height():
    """The live sessions block must grow like the two lists on the Сети tab.

    It sits in the group capped at ``max-height: 40vh`` with flex: 0 0 auto, so
    opening it grew nothing: the free height stayed unclaimed and the panel below
    it kept its place. Opening a second panel alongside must then take its share
    and pull this panel's bottom edge up, which is what the zeroed flex basis and
    the lifted ceiling together produce.
    """
    css = pathlib.Path('static/css/app.css').read_text(encoding='utf-8')

    rule = css[css.index('.panel-bgp-sessions.expanded {'):]
    rule = rule[:rule.index('}')]
    assert 'flex: 1 1 0;' in rule, rule
    assert 'min-height: 0;' in rule, rule
    assert 'max-height: none;' in rule, 'the 40vh ceiling must be lifted when open'

    # ...and the cap still applies while it is folded.
    capped = css[css.index('.panel-add,\n.panel-backup,'):]
    capped = capped[:capped.index('}')]
    assert 'max-height: 40vh;' in capped, 'a folded panel keeps the ceiling'

    # The body has to scroll rather than push the stack past the viewport.
    body_rule = css[css.index('.panel-add .panel-body,'):]
    body_rule = body_rule[:body_rule.index('}')]
    assert '.panel-bgp-sessions .panel-body,' in body_rule
    assert 'flex: 1;' in body_rule and 'min-height: 0;' in body_rule
    assert 'overflow-y: auto;' in body_rule
    # The stack itself is the constraint: without its own min-height:0 the panels
    # would push the column past the viewport instead of scrolling inside.
    stack = css[css.index('.tab-stack {'):]
    stack = stack[:stack.index('}')]
    assert 'min-height: 0;' in stack and 'overflow: hidden;' in stack


def test_recompute_keeps_the_editors_own_text_and_rebuilds_open_panes():
    """The recompute must redraw what is open and leave the textarea alone.

    No syncLeft on the preview: the textarea holds the operator's unsaved text,
    and updateDiffPreview is also what re-establishes the syntax error state
    from the response, so nothing has to be restored afterwards.
    """
    script = strip_js_comments(pathlib.Path('static/js/app.js').read_text(encoding='utf-8'))
    panes = script[script.index('async function refreshOpenConfigPanes'):]
    panes = panes[:panes.index('let birdRefreshInFlight')]

    assert "if (name === 'networks')" in panes
    assert 'await updateDiffPreview();' in panes
    assert 'syncLeft' not in panes, 'recompute must not rewrite the textarea'
    assert "section.dataset.loaded !== 'true'" in panes, 'collapsed editor is not loaded'
    for view in ('manager-conf-rows', 'core-rows'):
        assert view in script[script.index('const SECTION_VIEWS'):][:200], view
    # Open-file state survives the summary list being rebuilt from scratch.
    assert 'const summaryOpenViews = new Set();' in script
    assert 'item.dataset.viewId = viewId;' in script
    assert '.config-summary-row[data-view-id="' in script
    # A file that went unreadable, oversized or missing is dropped, not reopened.
    assert 'if (!selectable.has(viewId)) summaryOpenViews.delete(viewId);' in script


def test_build_script_resolves_versions_without_arguments():
    """The build script must work with no arguments at all.

    Handing `--version X.Y.Z` to every release was the friction that led to the
    version being edited twice (VERSION by hand and the flag) and the two
    disagreeing. The script now owns the arithmetic, validates it, and refuses
    nonsense instead of shipping a broken version number.
    """
    script = pathlib.Path('scripts/build-deb.sh').read_text(encoding='utf-8')

    # The bump levels and the arithmetic behind them.
    assert 'bump_component()' in script
    assert 'patch) echo "$major.$minor.$((patch + 1))" ;;' in script
    assert 'minor) echo "$major.$((minor + 1)).0" ;;' in script
    assert 'major) echo "$((major + 1)).0.0" ;;' in script

    # The flags must be parsed, not merely mentioned in the header comment.
    assert '--bump)' in script, 'the --bump case arm is what accepts the flag'
    assert '--version)' in script
    assert 'BUMP="$2"; shift 2 ;;' in script, 'the parsed level must be stored'

    # A bare run must still pick a version: the auto path has to call the
    # patch bump, otherwise it would silently rebuild the same number.
    assert 'bump_component \"$CURRENT\" patch' in script
    # ...and honour a hand-edited VERSION instead of overwriting it.
    assert 'VERSION changed since the last build' in script

    # Guard rails: a typo must fail loudly, not silently ship.
    assert "--bump accepts patch, minor or major" in script
    assert '--bump and --version are mutually exclusive' in script
    assert "is not numeric (expected X.Y.Z)" in script
    assert 'has more than three components' in script
    # The rejection must sit in the validator itself, not only in a message.
    assert 'case \"$raw\" in' in script
    assert '*[!0-9.]*) err' in script
    assert 'needs a value' in script
    assert 'normalise_version()' in script, 'a short VERSION must normalise, not break'

    # A build must not touch the running system unless asked.
    assert 'a build never touches the running system on its own' in script

    # The old helper is gone; keeping a half-replaced pair would be a trap.
    assert 'bump_version()' not in script


def test_build_script_bump_levels_are_correct():
    """The version arithmetic itself, exercised through the real script."""
    import subprocess

    script = pathlib.Path('scripts/build-deb.sh')
    # Extract just the two helpers so the cases can run without building.
    body = script.read_text(encoding='utf-8')
    start = body.index('normalise_version()')
    end = body.index('# ---- resolve version')
    # The real err() calls exit, which is what stops normalise_version from
    # echoing a garbage value after reporting the problem; a `return` stub would
    # let it print on and hide the bug.
    helpers = 'err() { echo "$*" >&2; exit 1; }\n' + body[start:end]

    cases = [
        ('1.2.3', 'patch', '1.2.4'),
        ('1.2.3', 'minor', '1.3.0'),
        ('1.2.3', 'major', '2.0.0'),
        ('1.9.9', 'patch', '1.9.10'),
        ('2.0.0', 'minor', '2.1.0'),
        ('1.7', 'minor', '1.8.0'),
    ]
    for version, level, expected in cases:
        out = subprocess.run(
            ['bash', '-c', helpers + f'\necho "$(bump_component {version} {level})"'],
            capture_output=True, text=True,
        )
        assert out.stdout.strip() == expected, (version, level, out.stdout, out.stderr)

    for bad in ('abc', '2.3.4.5', '1.2.x'):
        out = subprocess.run(
            ['bash', '-c', helpers + f'\nnormalise_version {bad}'],
            capture_output=True, text=True,
        )
        assert out.returncode != 0 or out.stdout.strip() == '', (bad, out.stdout)


def test_every_row_spans_the_full_width_of_the_pane():
    """Regression: rows were auto-placed side by side and overlapped.

    ``.row-line`` is itself a grid item of ``.row-view``, which is a two-column
    grid. Without an explicit span, auto-placement puts row 1 in column 1 and
    row 2 in column 2, so the two land beside each other and their cells draw on
    top of each other — two different config lines rendered at the same spot.
    The editor was unaffected only because its pane has a single column.
    """
    css = pathlib.Path('static/css/app.css').read_text(encoding='utf-8')
    rule = css[css.index('.row-line {'):css.index('.row-line {') + 200]
    assert 'grid-column: 1 / -1;' in rule, rule

    # A subgrid row borrowed the parent's lines, which is what made the overlap
    # visible rather than merely a narrow layout.
    assert 'subgrid' not in css
    # The single-column editor keeps its own column count outside @supports.
    assert '.row-view-single .row-line { grid-template-columns: 1fr; }' in css


def test_readable_row_panes_are_height_bounded():
    """An expanded file must not become a 40000px block.

    The cap was lost when the readonly panes moved to the row view, and the
    summary limit allows 2000 rows — unbounded, that is 40000px of rows. The
    editor is exempt on purpose: its height tracks the line count so the columns
    stay aligned with the textarea through the shared scrollbar.
    """
    dashboard = pathlib.Path('templates/dashboard.html').read_text(encoding='utf-8')
    css = pathlib.Path('static/css/app.css').read_text(encoding='utf-8')
    script = pathlib.Path('static/js/app.js').read_text(encoding='utf-8')

    bounded = css[css.index('.row-view-scroll {'):css.index('.row-view-scroll {') + 160]
    assert 'max-height' in bounded
    assert 'overflow: auto' in bounded
    for view in ('manager-conf-rows', 'core-rows'):
        assert f'id="{view}" class="row-view row-view-scroll"' in dashboard, view
    assert "'row-view row-view-scroll config-summary-view'" in script
    # The editor must keep the unbounded pane.
    assert 'id="editor-rows" class="row-view row-view-single"' in dashboard
    assert 'row-view-scroll' not in dashboard[dashboard.index('id="editor-rows"'):][:80]

    # The summary grid needs an explicit track: as an implicit `auto` column its
    # width came from the pane's own content instead of the section.
    summary = css[css.index('.config-summary-body {'):css.index('.config-summary-body {') + 120]
    assert 'grid-template-columns: minmax(0, 1fr)' in summary


def test_large_json_response_is_gzipped(access_api):
    """A full prefix list is 963 KB of repetitive JSON; gzip takes it to 66 KB.

    Only the transfer changes: the access log keeps reporting the uncompressed
    size, and a body under the threshold is sent as-is.
    """
    live = pathlib.Path(access_api._LIVE_PATH)
    live.mkdir(parents=True, exist_ok=True)
    big = '\n'.join(f'# line {i} ' + 'x' * 40 for i in range(600))
    (live / 'networks.conf').write_text(big + '\n')

    status, headers, raw, _stream, _cap = _wsgi_call(
        access_api, 'GET', '/api/config/live', query='filename=networks.conf',
    )
    assert status.startswith('200')
    assert 'Content-Encoding' not in headers, 'a client without gzip gets plain bytes'
    assert json.loads(raw)['content'] == big + '\n'

    status, headers, raw, _stream, _cap = _wsgi_call(
        access_api, 'GET', '/api/config/live', query='filename=networks.conf',
        environ={'HTTP_ACCEPT_ENCODING': 'gzip, deflate'},
    )
    assert status.startswith('200')
    assert headers.get('Content-Encoding') == 'gzip'
    assert headers.get('Vary') == 'Accept-Encoding'
    assert len(raw) < len(big) // 4, 'a repetitive body must actually shrink'
    assert json.loads(gzip.decompress(raw))['content'] == big + '\n'


def test_small_response_is_not_compressed(access_api):
    status, headers, raw, _stream, _cap = _wsgi_call(
        access_api, 'GET', '/api/health', environ={'HTTP_ACCEPT_ENCODING': 'gzip'},
    )
    assert status.startswith('200')
    assert 'Content-Encoding' not in headers
    # The value depends on whether the fixture's main config is migrated, so
    # only the shape is asserted here.
    assert 'status' in json.loads(raw)


# ---------------------------------------------------------------------------
# Documentation consistency
# ---------------------------------------------------------------------------


def _api_doc():
    with open('docs/api_backend.md', 'r', encoding='utf-8') as stream:
        return stream.read()


def test_api_doc_lists_every_registered_route():
    """Every @route in app/routes.py must appear in the API documentation."""
    import re

    with open('app/routes.py', 'r', encoding='utf-8') as stream:
        source = stream.read()

    def normalise(path):
        return re.sub(r'<(?:int:|string:|name:)(\w+)>', r'<\1>', path)

    routes = {
        (method, normalise(path))
        for path, method in re.findall(
            r"@route\('([^']+)', method='([A-Z]+)'\)", source
        )
    }
    doc = _api_doc()
    missing = [
        f'{method} {path}' for method, path in sorted(routes)
        if path != '/' and f'`{path}' not in doc
    ]
    assert not missing, f'undocumented routes: {missing}'


def test_api_doc_include_order_matches_generator():
    """The documented include block must match render_managed_entrypoint."""
    import re

    from app.config_generator import render_managed_entrypoint

    # The generator no longer emits per-Community policy files, so it passes an
    # empty policy list; the doc block must match that exactly.
    block = render_managed_entrypoint('/etc/bird/live', [])
    code_order = [
        line.split('/')[-1].rstrip('";')
        for line in block.splitlines() if line.startswith('include ')
    ]

    doc_order = re.findall(r'include "/etc/bird/live/([^"]+)"', _api_doc())
    assert code_order == ['networks.conf', 'export.conf', 'routes.conf', 'core.conf']
    # The legacy (monolithic) include order must be present in the docs.
    assert ['networks.conf', 'export.conf', 'routes.conf', 'core.conf'] == code_order
    # Split-layout include lines are also documented; they must not break the
    # legacy-order check above.


def test_api_doc_matches_live_response_fields():
    """Documented response fields must exist in the real payload."""
    doc = _api_doc()

    status_fields = [
        'status', 'action', 'user_id', 'last_applied', 'last_diff_summary',
        'has_backup', 'bird_running', 'reload_warning', 'reload_detail',
        'bird_last_reboot', 'bird_last_reconfiguration', 'backup',
    ]
    backup_item_fields = ['id', 'created_at', 'display_time', 'legacy_time', 'size', 'latest']
    peer_fields = [
        'name', 'state', 'bgp_state', 'channel_state', 'neighbor', 'neighbor_as',
        'local_as', 'since', 'info', 'last_error', 'imported', 'exported',
        'managed', 'in_bird',
    ]
    service_fields = ['name', 'proto', 'state', 'channel_state', 'import_state', 'since']

    with open('app/routes.py', 'r', encoding='utf-8') as stream:
        source = stream.read()
    for field in status_fields + backup_item_fields + peer_fields + service_fields:
        assert f'`{field}`' in doc, f'{field} is not documented'
    # A couple of shapes that must stay in sync with the implementation.
    assert "'error_files'" in source
    assert 'error_files' in doc
    assert 'syntax_error_rows' in doc
    assert 'BIRD_PUBLISH_BAK_DIR' in doc
    assert 'сохраняет `.bak`' not in doc, 'apply no longer keeps .bak in the live dir'


def test_api_doc_documents_access_control_and_backup_contract():
    doc = _api_doc()
    for key in (
        'BIRD_TRUSTED_SOURCES', 'BIRD_TRUSTED_PROXIES', 'BIRD_TRUSTED_ORIGINS',
        'BIRD_MAX_BODY_BYTES', 'BIRD_DEBUG_ERRORS', 'APP_HOST',
    ):
        assert f'`{key}`' in doc
    for code in ('400', '403', '404', '413', '500'):
        assert f'| `{code}` |' in doc, f'status {code} is not documented'
    # The archive contents table must not claim live-bak is part of a snapshot.
    assert '| `state/managed-files.json` |' in doc
    assert '| `main/bird.conf` |' in doc


# ---------------------------------------------------------------------------
# No dead code at module level
# ---------------------------------------------------------------------------


def test_no_unreferenced_module_level_functions():
    """Every plain function in app/ must be referenced outside its definition.

    Guards against dead exports creeping back: they are invisible in production
    (0 references), cost maintenance, and hide real behaviour. Counted as dead
    are module-level functions that nothing mentions by name. Decorated
    functions are exempt — route handlers are referenced by the ROUTES registry
    through @route, and static/class methods through attribute access.
    """
    import ast
    import pathlib
    import re

    app_dir = pathlib.Path('app')
    corpus = '\n'.join(
        path.read_text(encoding='utf-8')
        for path in sorted(app_dir.rglob('*.py')) + sorted(pathlib.Path('tests').rglob('*.py'))
        if '__pycache__' not in str(path)
    )
    occurrences = {
        name: len(re.findall(r'\b' + re.escape(name) + r'\b', corpus))
        for name in re.findall(r'^(?:async )?def (\w+)\(', corpus, re.M)
    }

    dead = []
    for path in sorted(app_dir.rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name.startswith('__'):
                continue
            if node.decorator_list:
                continue        # @route / @staticmethod / @property: referenced elsewhere
            if occurrences.get(node.name, 0) <= 1:
                dead.append(f'{path}::{node.name}')
    assert not dead, f'functions without references: {dead}'


def test_removed_state_dir_is_gone_everywhere():
    """last-published was removed; nothing may create or document it again."""
    import pathlib

    targets = [
        '.env', 'scripts/build-deb.sh', 'packaging/debian/postinst',
        'packaging/bird-manager.8', 'README.md',
        'docs/api_backend.md', 'docs/build.md', 'docs/frontend.md',
    ]
    for name in targets:
        path = pathlib.Path(name)
        if not path.exists():
            continue
        content = path.read_text(encoding='utf-8')
        assert 'last-published' not in content, name
        assert 'LAST_PUBLISHED' not in content, name
    for path in sorted(pathlib.Path('app').rglob('*.py')):
        if '__pycache__' in str(path):
            continue
        assert 'last_published' not in path.read_text(encoding='utf-8'), str(path)


# ---------------------------------------------------------------------------
# Malformed CIDR review: the parser classifies every candidate, the routes ask
# for confirmation instead of storing the raw string, and the dialog drives
# the choice.
# ---------------------------------------------------------------------------


def _request_status(request, method, path, payload=None):
    """Like the fixture's request(), but returns (status, body) for 4xx too."""
    try:
        return request(method, path, payload)
    except urllib.error.HTTPError as err:
        body = err.read()
        try:
            return err.code, json.loads(body)
        except ValueError:
            return err.code, {}


@pytest.fixture
def parser():
    from app.parsing_engine import ParsingEngine

    return ParsingEngine()


@pytest.mark.parametrize('raw,expected', [
    ('192.168.1.43/24', '192.168.1.0/24'),
    ('10.9.240.0/12', '10.0.0.0/12'),
])
def test_classify_token_masks_host_bits(parser, raw, expected):
    verdict = parser.classify_token(raw)
    assert verdict['status'] == 'fixable'
    assert verdict['suggestion'] == expected
    assert verdict['reason']


@pytest.mark.parametrize('raw,expected', [
    ('10.0.0.0/33', '10.0.0.0/32'),
    ('10.0.0.0/99', '10.0.0.0/32'),
    ('10.0.0.0/24', None),
])
def test_classify_token_repairs_prefix_length(parser, raw, expected):
    verdict = parser.classify_token(raw)
    if expected is None:
        assert verdict['status'] == 'valid'
        return
    assert verdict['status'] == 'fixable'
    assert verdict['suggestion'] == expected


def test_classify_token_offers_host_route_for_bare_address(parser):
    verdict = parser.classify_token('10.0.0.1')
    assert verdict['status'] == 'fixable'
    assert verdict['suggestion'] == '10.0.0.1/32'


@pytest.mark.parametrize('raw', ['999.1.1.1/24', '10.0.0.256/24', 'not-a-prefix', ''])
def test_classify_token_reports_unfixable_without_suggestion(parser, raw):
    verdict = parser.classify_token(raw)
    assert verdict['status'] == 'unfixable'
    assert verdict['suggestion'] == ''
    assert verdict['reason']


@pytest.mark.parametrize('raw', ['10.0.0.0/24', '0.0.0.0/0', '1.2.3.4/31', '1.2.3.4/32'])
def test_classify_token_accepts_canonical_prefixes(parser, raw):
    assert parser.classify_token(raw)['status'] == 'valid'


def test_parse_and_map_only_returns_canonical_prefixes(parser):
    result = parser.parse_and_map(
        '10.0.0.0/24, 192.168.1.43/24; 10.9.240.0/12 10.0.0.0/33 999.1.1.1/24',
        'text',
    )

    assert [net['prefix'] for net in result['networks']] == ['10.0.0.0/24']

    repairs = {entry['input']: entry['suggestion'] for entry in result['invalid']}
    assert repairs == {
        '192.168.1.43/24': '192.168.1.0/24',
        '10.9.240.0/12': '10.0.0.0/12',
        '10.0.0.0/33': '10.0.0.0/32',
        '999.1.1.1/24': '',
    }


def test_parse_and_map_surfaces_bare_addresses(parser):
    result = parser.parse_and_map('10.0.0.1 10.0.0.0/24', 'text')
    assert [net['prefix'] for net in result['networks']] == ['10.0.0.0/24']
    assert result['invalid'] == [{
        'input': '10.0.0.1', 'suggestion': '10.0.0.1/32',
        'reason': 'не указана длина маски',
    }]


def test_bulk_commit_canonicalizes_before_deduplicating(db_session):
    result = CRUDOperations.bulk_commit_networks(db_session, [
        {'prefix': '10.9.240.0/12', 'community_ids': [], 'enabled': True},
        {'prefix': '10.0.0.0/12', 'community_ids': [], 'enabled': True},
    ])

    assert result['created'] == 1
    assert result['skipped_duplicates'] == 1
    assert db_session.query(Network).one().prefix == '10.0.0.0/12'


def test_bulk_commit_skips_unparseable_prefixes(db_session):
    result = CRUDOperations.bulk_commit_networks(db_session, [
        {'prefix': '10.0.0.0/24', 'community_ids': [], 'enabled': True},
        {'prefix': '999.1.1.1/24', 'community_ids': [], 'enabled': True},
    ])

    assert result['created'] == 1
    assert result['skipped_invalid'] == 1
    assert db_session.query(Network).one().prefix == '10.0.0.0/24'


def test_create_network_refuses_unparseable_prefix(db_session):
    with pytest.raises(ValueError):
        CRUDOperations.create_network(db_session, prefix='999.1.1.1/24')


def test_create_network_canonicalizes_prefix(db_session):
    created = CRUDOperations.create_network(db_session, prefix='192.168.1.43/24')
    assert created.prefix == '192.168.1.0/24'


def test_bulk_endpoint_returns_structured_invalid(http_api):
    request, _routes, _live, _staged, _main = http_api
    status, body = request('POST', '/api/networks/bulk', {
        'raw_data': '10.0.0.0/24 192.168.1.43/24 999.1.1.1/24', 'format': 'text',
    })

    assert status == 200
    assert [net['prefix'] for net in body['parsed_networks']] == ['10.0.0.0/24']
    entries = {entry['input']: entry for entry in body['invalid']}
    assert entries['192.168.1.43/24']['suggestion'] == '192.168.1.0/24'
    assert entries['999.1.1.1/24']['suggestion'] == ''


@pytest.mark.parametrize('raw,suggestion', [
    ('192.168.1.43/24', '192.168.1.0/24'),
    ('10.0.0.0/33', '10.0.0.0/32'),
    ('10.0.0.1', '10.0.0.1/32'),
])
def test_create_network_asks_confirmation_instead_of_storing(http_api, db_session, raw, suggestion):
    request, _routes, _live, _staged, _main = http_api
    status, body = _request_status(request, 'POST', '/api/networks', {
        'prefix': raw, 'description': '', 'status': 'pending',
    })

    assert status == 409
    assert body['needs_confirmation'] is True
    assert body['invalid'][0]['input'] == raw
    assert body['invalid'][0]['suggestion'] == suggestion
    assert db_session.query(Network).count() == 0


def test_create_network_stores_confirmed_repair(http_api, db_session):
    request, _routes, _live, _staged, _main = http_api
    status, body = request('POST', '/api/networks', {
        'prefix': '192.168.1.0/24', 'description': '', 'status': 'pending',
    })

    assert status == 201
    assert body['network']['prefix'] == '192.168.1.0/24'
    assert db_session.query(Network).one().prefix == '192.168.1.0/24'


def test_update_network_asks_confirmation_for_edited_prefix(http_api, db_session):
    request, _routes, _live, _staged, _main = http_api
    created = CRUDOperations.create_network(db_session, prefix='10.0.0.0/24')

    status, body = _request_status(request, 'PUT', f'/api/networks/{created.id}', {
        'prefix': '10.9.240.0/12',
    })

    assert status == 409
    assert body['invalid'][0]['suggestion'] == '10.0.0.0/12'
    assert db_session.query(Network).one().prefix == '10.0.0.0/24'


def test_cidr_review_dialog_is_read_only_but_selectable():
    with open('templates/dashboard.html', 'r', encoding='utf-8') as stream:
        html = stream.read()

    assert 'id="cidr-review"' in html
    assert 'Найдены некорректные CIDR. Исправить или удалить?' in html
    assert "найдено</th>" in html
    assert "исправление</th>" in html
    assert 'id="cidr-review-fix"' in html
    assert 'id="cidr-review-drop"' in html
    # Read-only list, not an editable form: no input elements inside.
    dialog = html.split('id="cidr-review"', 1)[1].split('</dialog>', 1)[0]
    assert '<input' not in dialog
    assert '<textarea' not in dialog

    with open('static/css/app.css', 'r', encoding='utf-8') as stream:
        css = stream.read()
    body_rule = css.split('.cidr-body {', 1)[1].split('}', 1)[0]
    assert 'user-select: text' in body_rule

    with open('static/js/app.js', 'r', encoding='utf-8') as stream:
        script = stream.read()
    assert 'function reviewCidrs(entries)' in script
    assert 'async function saveNetworkReviewed(' in script
    # Esc behaves like «Удалить»: only the correct prefixes get added.
    assert "const onCancel = () => finish('drop');" in script
    assert "safeFetch('/api/networks/bulk'" in script
    assert "saveNetworkReviewed('/api/networks', 'POST'" in script
    assert "saveNetworkReviewed('/api/networks/' + net.id, 'PUT'" in script


@pytest.mark.parametrize('raw', [
    '1.2.3.4.5/24',
    '10.0.0.1.2.3/8',
    'permit ip 1.2.3.4.5/24 any',
])
def test_junk_address_tokens_are_dropped_without_a_dialog_entry(parser, raw):
    """A token with the wrong octet count is dropped, not offered for repair."""
    result = parser.parse_and_map(raw, 'text')

    assert result['networks'] == []
    assert result['invalid'] == []


def test_junk_token_never_yields_a_regex_fragment(parser):
    """The loose regex would otherwise pull 2.3.4.5/24 out of 1.2.3.4.5/24."""
    result = parser.parse_and_map('1.2.3.4.5/24, 10.0.0.0/24', 'text')

    assert [net['prefix'] for net in result['networks']] == ['10.0.0.0/24']
    assert result['invalid'] == []


def test_junk_token_is_dropped_in_command_format(parser):
    result = parser.parse_and_map(
        'community SITE permit ip 10.0.0.0/24\n 1.2.3.4.5/24', 'command')

    assert [net['prefix'] for net in result['networks']] == ['10.0.0.0/24']
    assert [c['name'] for c in result['communities']] == ['SITE']
    assert result['invalid'] == []


def test_junk_token_is_dropped_in_json_format(parser):
    result = parser.parse_and_map(
        {'prefixes': ['10.0.0.0/24', '1.2.3.4.5/24']}, 'json')

    assert [net['prefix'] for net in result['networks']] == ['10.0.0.0/24']
    assert result['invalid'] == []


def test_four_octet_value_error_still_reaches_the_dialog(parser):
    """Only the octet count silences an entry; a bad value still gets a row."""
    result = parser.parse_and_map('999.1.1.1/24', 'text')

    assert result['networks'] == []
    assert result['invalid'] == [{
        'input': '999.1.1.1/24', 'suggestion': '', 'reason': 'некорректный адрес',
    }]


def test_bulk_endpoint_omits_junk_tokens_from_the_review(http_api):
    request, _routes, _live, _staged, _main = http_api
    status, body = request('POST', '/api/networks/bulk', {
        'raw_data': '1.2.3.4.5/24 10.0.0.0/24', 'format': 'text',
    })

    assert status == 200
    assert [net['prefix'] for net in body['parsed_networks']] == ['10.0.0.0/24']
    assert body['invalid'] == []


# ---------------------------------------------------------------------------
# Reproducible extraction, ordered insertion, and the retired write path.
# ---------------------------------------------------------------------------


def test_extraction_keeps_order_of_appearance_and_deduplicates(parser):
    raw = '10.0.0.0/24 192.168.1.0/24 172.16.0.0/16 10.0.0.0/24 203.0.113.0/24'
    result = parser.parse_and_map(raw, 'text')

    assert [net['prefix'] for net in result['networks']] == [
        '10.0.0.0/24', '192.168.1.0/24', '172.16.0.0/16', '203.0.113.0/24',
    ]


def test_extraction_is_reproducible_across_calls(parser):
    from app.parsing_engine import ParsingEngine

    raw = '10.0.0.0/24 192.168.1.0/24 172.16.0.0/16 203.0.113.0/24 198.51.100.0/24'
    first = parser.parse_and_map(raw, 'text')['networks']
    second = ParsingEngine().parse_and_map(raw, 'text')['networks']

    assert first == second


@pytest.mark.parametrize('raw', [
    '10.0.0.0/24/8',
    '10.0.0.0/24.5',
    '999.1.1.1/24/8',
])
def test_trailing_garbage_is_dropped_without_a_dialog_entry(parser, raw):
    result = parser.parse_and_map(raw, 'text')

    assert result['networks'] == []
    assert result['invalid'] == []


@pytest.mark.parametrize('raw', [
    '10.0.0.0/24.',
    '10.0.0.0/24,',
    '10.0.0.0/24;',
    '10.0.0.0/24)',
    '{10.0.0.0/24}',
    '(10.0.0.0/24)',
    '"10.0.0.0/24"',
    '[10.0.0.0/24]',
    'ip prefix 10.0.0.0/24',
])
def test_punctuation_wrappers_are_not_treated_as_garbage(parser, raw):
    result = parser.parse_and_map(raw, 'text')

    assert [net['prefix'] for net in result['networks']] == ['10.0.0.0/24']


def test_review_table_stays_alphabetical(parser):
    result = parser.parse_and_map(
        '10.0.0.1 192.168.1.43/24 999.1.1.1/24 10.0.0.0/33', 'text')

    assert [entry['input'] for entry in result['invalid']] == [
        '10.0.0.0/33', '10.0.0.1', '192.168.1.43/24', '999.1.1.1/24',
    ]


def test_community_names_keep_order_of_appearance(parser):
    result = parser.parse_and_map(
        'community ZONE_B permit ip 10.0.0.0/24\n group SITE permit ip 10.0.0.1/24',
        'command')

    assert [c['name'] for c in result['communities']] == ['ZONE_B', 'SITE']


def test_json_extraction_keeps_order_of_appearance(parser):
    result = parser.parse_and_map(
        {'prefixes': ['10.0.0.0/24', '192.168.0.0/16', '10.0.0.0/24']}, 'json')

    assert [net['prefix'] for net in result['networks']] == [
        '10.0.0.0/24', '192.168.0.0/16',
    ]


def test_network_sort_key_orders_by_address_not_by_string():
    from app.parsing_engine import network_sort_key

    batch = ['10.0.0.0/24', '10.0.0.0/8', '9.0.0.0/8', '100.0.0.0/8', '10.0.0.0/16']

    assert sorted(batch, key=network_sort_key) == [
        '9.0.0.0/8', '10.0.0.0/8', '10.0.0.0/16', '10.0.0.0/24', '100.0.0.0/8',
    ]


def test_bulk_commit_inserts_sorted_by_network_address(db_session):
    CRUDOperations.bulk_commit_networks(db_session, [
        {'prefix': '100.0.0.0/8', 'community_ids': [], 'enabled': True},
        {'prefix': '10.0.0.0/24', 'community_ids': [], 'enabled': True},
        {'prefix': '9.0.0.0/8', 'community_ids': [], 'enabled': True},
        {'prefix': '10.0.0.0/8', 'community_ids': [], 'enabled': True},
    ])

    assert [net.prefix for net in CRUDOperations.get_networks(db_session)] == [
        '9.0.0.0/8', '10.0.0.0/8', '10.0.0.0/24', '100.0.0.0/8',
    ]


def test_bulk_commit_sort_covers_repaired_entries(db_session):
    """A dialog repair lands inside the sorted block, not after it.

    The repair (192.168.1.0/24) sorts between the two parsed prefixes, so a
    plain concatenation would put it last and fail here.
    """
    CRUDOperations.bulk_commit_networks(db_session, _browser_batch(
        '10.0.0.0/8 203.0.113.0/24 192.168.1.43/24'))

    assert [net.prefix for net in CRUDOperations.get_networks(db_session)] == [
        '10.0.0.0/8', '192.168.1.0/24', '203.0.113.0/24',
    ]


def _browser_batch(raw):
    """Build the batch a browser sends: parsed networks, then accepted repairs."""
    from app.parsing_engine import ParsingEngine

    parsed = ParsingEngine().parse_and_map(raw, 'text')
    records = [
        dict(net, community_ids=[], enabled=True) for net in parsed['networks']
    ]
    for entry in parsed['invalid']:
        if entry['suggestion']:
            records.append({'prefix': entry['suggestion'], 'description': '',
                            'status': 'pending', 'community_ids': [], 'enabled': True})
    return records


def test_bulk_commit_leaves_existing_rows_untouched(db_session):
    first = CRUDOperations.create_network(db_session, prefix='3.24.1.208/28')
    CRUDOperations.bulk_commit_networks(db_session, [
        {'prefix': '9.0.0.0/8', 'community_ids': [], 'enabled': True},
        {'prefix': '10.0.0.0/8', 'community_ids': [], 'enabled': True},
    ])

    rows = CRUDOperations.get_networks(db_session)
    assert rows[0].id == first.id
    assert rows[0].prefix == '3.24.1.208/28'
    assert [net.prefix for net in rows] == [
        '3.24.1.208/28', '9.0.0.0/8', '10.0.0.0/8',
    ]


def test_second_batch_appends_as_its_own_sorted_block(db_session):
    CRUDOperations.bulk_commit_networks(db_session, [
        {'prefix': '10.0.0.0/8', 'community_ids': [], 'enabled': True},
        {'prefix': '9.0.0.0/8', 'community_ids': [], 'enabled': True},
    ])
    CRUDOperations.bulk_commit_networks(db_session, [
        {'prefix': '192.168.0.0/16', 'community_ids': [], 'enabled': True},
        {'prefix': '172.16.0.0/12', 'community_ids': [], 'enabled': True},
    ])

    assert [net.prefix for net in CRUDOperations.get_networks(db_session)] == [
        '9.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
    ]


def test_retired_write_path_is_gone():
    """bulk_import_networks had no callers and stored prefixes raw."""
    assert not hasattr(CRUDOperations, 'bulk_import_networks')


def test_get_networks_order_is_explicit(db_session):
    CRUDOperations.create_network(db_session, prefix='100.0.0.0/8')
    CRUDOperations.create_network(db_session, prefix='9.0.0.0/8')

    assert [net.prefix for net in CRUDOperations.get_networks(db_session)] == [
        '100.0.0.0/8', '9.0.0.0/8',
    ]


# ---------------------------------------------------------------------------
# One scanner for every format, address-key bare addresses, chunked import.
# ---------------------------------------------------------------------------


def _networks(raw, fmt='text'):
    from app.parsing_engine import ParsingEngine

    return [net['prefix'] for net in ParsingEngine().parse_and_map(raw, fmt)['networks']]


@pytest.mark.parametrize('payload', [
    '10.0.0.0/24 192.168.0.0/16',
    ['10.0.0.0/24', '192.168.0.0/16'],
    {'prefixes': ['10.0.0.0/24', '192.168.0.0/16']},
    '[{"address":"10.0.0.0/24"},{"address":"192.168.0.0/16"}]',
    '{"data":[{"cidr":"10.0.0.0/24"},{"cidr":"192.168.0.0/16"}]}',
])
def test_every_format_produces_the_same_prefixes(payload):
    """format is kept for compatibility but no longer changes the scanner."""
    expected = ['10.0.0.0/24', '192.168.0.0/16']

    assert _networks(payload, 'text') == expected
    assert _networks(payload, 'json') == expected
    assert _networks(payload, 'command') == expected


def test_json_array_is_no_longer_a_silent_empty_result():
    """The old JSON branch required a dict and dropped arrays without a word."""
    assert _networks('[{"address":"10.0.0.0/24"}]', 'json') == ['10.0.0.0/24']


def test_retired_json_helpers_are_gone():
    """extract_from_json required a dict and its target_keys was never used."""
    from app.parsing_engine import ParsingEngine

    engine = ParsingEngine()
    assert not hasattr(engine, 'extract_from_json')
    assert not hasattr(engine, '_extract_from_dict')


@pytest.mark.parametrize('key', ['address', 'cidr', 'prefix', 'net', 'network'])
def test_bare_address_under_an_address_key_offers_a_host_route(key):
    from app.parsing_engine import ParsingEngine

    result = ParsingEngine().parse_and_map('{"%s":"5.63.111.80"}' % key, 'text')

    assert result['networks'] == []
    assert result['invalid'] == [{
        'input': '5.63.111.80', 'suggestion': '5.63.111.80/32',
        'reason': 'не указана длина маски',
    }]


@pytest.mark.parametrize('key', ['version', 'router_id', 'timestamp', 'id'])
def test_dotted_quad_under_an_unrelated_key_stays_ignored(key):
    assert _networks('{"%s":"1.2.3.4"}' % key, 'text') == []


def test_address_key_ignores_shapes_that_are_not_prefixes():
    """Five octets and IPv6 are not host-route candidates."""
    assert _networks('{"address":"1.2.3.4.5"}', 'text') == []
    assert _networks('{"address":"2a03:b0c0::/32"}', 'text') == []
    assert _networks("{'address':'5.63.111.80'}", 'text') == []


def test_address_key_with_bad_octet_reaches_the_dialog():
    from app.parsing_engine import ParsingEngine

    result = ParsingEngine().parse_and_map('{"address":"999.1.1.1"}', 'text')

    assert result['networks'] == []
    assert result['invalid'] == [{
        'input': '999.1.1.1', 'suggestion': '', 'reason': 'некорректный адрес',
    }]


def test_pasted_geo_feed_parses_regardless_of_json_validity():
    """The scanner never parses JSON, so a truncated paste still works."""
    truncated = ('[{"address":"5.63.111.80/28","address_type":"ipv4","region":null,'
                 '"tag":"cdn"},{"address":"5.187.84.32/28","address_type":"ipv4",'
                 '"region":null,"tag":"cdn"},{"address":"31.44.8.0/21","address_type":"ipv "')
    expected = ['5.63.111.80/28', '5.187.84.32/28', '31.44.8.0/21']

    assert _networks(truncated, 'text') == expected
    assert _networks(truncated, 'json') == expected


def test_junk_surviving_inside_compact_json_is_dropped():
    """Quoting hides the junk token, so the fragment check must be positional."""
    payload = '{"prefixes": ["10.0.0.0/24", "1.2.3.4.5/24"]}'

    assert _networks(payload, 'json') == ['10.0.0.0/24']
    assert _networks('{"address":"1.2.3.4.5/24","address2":"10.0.0.0/24"}', 'text') == ['10.0.0.0/24']


def test_two_commit_chunks_produce_two_ordered_blocks(db_session):
    """Chunking is visible in the row order: one sorted block per request."""
    first = [{'prefix': p, 'community_ids': [], 'enabled': True}
             for p in ('203.0.113.0/24', '10.0.0.0/8', '9.0.0.0/8')]
    second = [{'prefix': p, 'community_ids': [], 'enabled': True}
              for p in ('192.168.0.0/16', '172.16.0.0/12')]

    CRUDOperations.bulk_commit_networks(db_session, first)
    CRUDOperations.bulk_commit_networks(db_session, second)

    assert [net.prefix for net in CRUDOperations.get_networks(db_session)] == [
        '9.0.0.0/8', '10.0.0.0/8', '203.0.113.0/24',   # chunk 1, sorted
        '172.16.0.0/12', '192.168.0.0/16',              # chunk 2, sorted
    ]


def test_bulk_progress_dialog_blocks_and_can_be_aborted():
    with open('templates/dashboard.html', 'r', encoding='utf-8') as stream:
        html = stream.read()
    assert 'id="bulk-progress"' in html
    assert 'id="bulk-progress-cancel"' in html
    assert '>Прервать<' in html
    assert '<progress' in html

    with open('static/js/app.js', 'r', encoding='utf-8') as stream:
        script = stream.read()
    assert 'const BulkProgress' in script
    assert 'function splitForBulk(' in script
    # The abort flag is checked before each chunk, so a click lands between
    # requests and never in the middle of one.
    assert 'if (BulkProgress.aborted) break;' in script
    # Modal dialog is what blocks the panel: no manual disabled juggling.
    assert "dialog.showModal();" in script
    assert 'BULK_CHUNK_BYTES' in script
    assert 'BULK_COMMIT_RECORDS' in script

    # One blocking dialog for the whole import. Two opens left the panel live
    # for the length of the community lookup, and with a short paste the first
    # dialog only flashed.
    assert script.count('BulkProgress.open()') == 1
    assert script.count('BulkProgress.close()') == 1
    # finally: the dialog comes down on every exit, a throw included.
    assert script.count('} finally {') >= 1
    assert re.search(
        r'\}\s*finally\s*\{\s*\n\s*BulkProgress\.close\(\);', script
    ), 'BulkProgress.close() must sit in a finally block'


def test_bulk_progress_shows_a_label_for_every_phase():
    with open('static/js/app.js', 'r', encoding='utf-8') as stream:
        script = stream.read()
    # Stretches that are not a chunk loop must not keep a stale label.
    assert re.search(r'phase\(text\)\s*\{', script)
    assert "BulkProgress.phase(t('m031'))" in script
    assert "BulkProgress.phase(t('m065'))" in script
    assert "BulkProgress.update(t('m084')" in script
    assert "BulkProgress.update(t('m082')" in script


def test_split_for_bulk_never_cuts_a_prefix_in_half(tmp_path):
    """Exercise the shipped JS splitter through node.

    The payload goes in over stdin so no temporary file is needed; the point
    is to assert the property against the real function rather than a mirror.
    """
    node = shutil.which('node')
    if node is None:
        pytest.skip('node is not available')

    with open('static/js/app.js', 'r', encoding='utf-8') as stream:
        source = stream.read()
    start = source.index('function splitForBulk(')
    end = source.index('return chunks;', start) + len('return chunks;')
    end = source.index('\n}', end) + 2
    function = source[start:end]

    entries = [
        {'address': '10.%d.%d.%d/24' % (i // 65536 % 256, i // 256 % 256, i % 256),
         'address_type': 'ipv4'}
        for i in range(4000)
    ]
    # Both shapes matter: with ", " json.dumps puts spaces in, and a feed
    # without any space at all is what broke the separator search before.
    payloads = {
        'spaced': json.dumps(entries),
        'no-spaces': json.dumps(entries, separators=(',', ':')),
    }
    harness = function + """
const fs = require('fs');
const text = fs.readFileSync(0, 'utf8');
const chunks = splitForBulk(text, 4096);
let intact = true;
chunks.forEach(function (chunk, i) {
    if (i < chunks.length - 1 && !/[ ,\\n]$/.test(chunk)) intact = false;
});
process.stdout.write(JSON.stringify({
    count: chunks.length,
    rejoined: chunks.join('') === text,
    intact: intact
}));
"""
    for shape, payload in payloads.items():
        result = subprocess.run(
            [node, '-e', harness], input=payload,
            capture_output=True, text=True, timeout=60,
        )

        assert result.returncode == 0, '%s: %s' % (shape, result.stderr)
        report = json.loads(result.stdout)
        assert report['count'] > 1, '%s: the fixture must actually be split' % shape
        assert report['rejoined'] is True, (
            '%s: chunks must reassemble to the original text' % shape)
        assert report['intact'] is True, (
            '%s: every cut must land on a separator' % shape)


# ---------------------------------------------------------------------------
# The browser orders the batch before splitting it into chunks, so a big paste
# lands as one continuous run. That only holds while the two sort keys agree.
# ---------------------------------------------------------------------------


def _run_split_pipeline(n, chunk_size):
    """Client sort + chunk via the real JS, then re-sort each chunk in Python.

    Mirrors runBulkAdd: sort the batch, cut it, and let bulk_commit_networks
    sort every chunk again by its own key. If the keys diverge, the two passes
    fight and the result stops being ordered.
    """
    node = shutil.which('node')
    if node is None:
        pytest.skip('node is not available')

    with open('static/js/app.js', 'r', encoding='utf-8') as stream:
        source = stream.read()

    def js_function(name, last_line):
        start = source.index(name)
        end = source.index(last_line, start) + len(last_line)
        return source[start:source.index('\n}', end) + 2]

    js = (js_function('function prefixSortKey(', 'return network * 33 + mask;')
          + '\n'
          + js_function('function splitForBulk(', 'return chunks;')
          + """
const fs = require('fs');
const items = JSON.parse(fs.readFileSync(0, 'utf8'));
items.sort((a, b) => prefixSortKey(a) - prefixSortKey(b));
const chunks = [];
for (let i = 0; i < items.length; i += CHUNK) {
    chunks.push(items.slice(i, i + CHUNK));
}
process.stdout.write(JSON.stringify(chunks));
""")

    # Not canonical: the mask leaves host bits set on most entries, which is
    # what pins the browser's masking against the server's.
    feed = []
    for i in range(n):
        octets = [i // 16777216 % 256, i // 65536 % 256, i // 256 % 256, i % 256]
        feed.append('%d.%d.%d.%d/%d' % (octets[0], octets[1], octets[2], octets[3],
                                        32 - i % 6))
    state = 12345                       # deterministic shuffle
    for i in range(len(feed) - 1, 0, -1):
        state = (state * 1103515245 + 12345) % 2147483648
        j = state % (i + 1)
        feed[i], feed[j] = feed[j], feed[i]

    result = subprocess.run(
        [node, '-e', 'const CHUNK = %d;\n%s' % (chunk_size, js)],
        input=json.dumps(feed), capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout), feed


def test_client_and_server_sort_keys_agree_on_a_tricky_fixture():
    """The two keys must order the awkward cases identically."""
    from app.parsing_engine import network_sort_key

    node = shutil.which('node')
    if node is None:
        pytest.skip('node is not available')

    # Host bits are deliberately included: the browser must mask them the way
    # the server does. The top of the address space is here too, because a
    # shift back without >>> 0 overflows into a negative int32 there.
    tricky = ['9.255.255.255/32', '10.0.0.0/24', '10.0.0.0/8', '10.0.0.0/32',
              '10.0.0.0/16', '100.0.0.0/8', '99.255.255.255/32', '1.2.3.4/32',
              '1.2.3.4/24', '0.0.0.0/0', '0.0.0.0/32', '0.0.0.1/31',
              '0.0.0.1/32', '192.168.0.0/16', '192.168.1.0/24',
              '192.168.1.43/24', '11.0.0.0/8', '255.255.255.255/32',
              '255.255.255.255/1', '255.255.255.254/1', '255.255.255.0/24',
              '255.255.0.0/16', '128.0.0.0/1', '127.255.255.255/32',
              '100.64.0.0/10']

    with open('static/js/app.js', 'r', encoding='utf-8') as stream:
        source = stream.read()
    start = source.index('function prefixSortKey(')
    end = source.index('return network * 33 + mask;', start)
    function = source[start:source.index('\n}', end) + 2]

    result = subprocess.run(
        [node, '-e', function + """
const items = JSON.parse(process.argv[1]);
items.sort((a, b) => prefixSortKey(a) - prefixSortKey(b));
process.stdout.write(JSON.stringify(items));
""", json.dumps(tricky)],
        capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == sorted(tricky, key=network_sort_key)


def test_sorted_before_chunking_yields_one_ordered_run():
    """The chunks line up, so the whole batch comes out in address order."""
    from app.parsing_engine import network_sort_key

    chunks, original = _run_split_pipeline(900, 250)
    assert len(chunks) == 4, 'the fixture must actually be split'

    # What bulk_commit_networks does to each chunk.
    committed = [sorted(chunk, key=network_sort_key) for chunk in chunks]
    rows = [prefix for chunk in committed for prefix in chunk]

    keys = [network_sort_key(prefix) for prefix in rows]
    assert keys == sorted(keys), 'the joined result must be one ordered run'
    # The feed carries host bits on purpose. The server canonicalises before
    # insert and drops repeats, so the table ends up with the canonical set —
    # every prefix of the input present exactly once.
    expected = {str(ipaddress.ip_network(prefix, strict=False)) for prefix in original}
    landed = {str(ipaddress.ip_network(prefix, strict=False)) for prefix in rows}
    assert landed == expected


def test_unsorted_client_order_would_break_the_run():
    """Why the client sort exists: without it the chunks do not line up.

    Same feed, chunk boundaries in input order instead of sorted order.
    """
    from app.parsing_engine import network_sort_key

    chunks, original = _run_split_pipeline(900, 250)
    scrambled = [original[i:i + 250] for i in range(0, len(original), 250)]
    rows = [
        prefix
        for chunk in scrambled
        for prefix in sorted(chunk, key=network_sort_key)
    ]

    keys = [network_sort_key(prefix) for prefix in rows]
    assert keys != sorted(keys), 'the fixture must be genuinely out of order'


# ---------------------------------------------------------------------------
# The community name is the source of truth for its values; the ranges follow
# RFC 5398 for the ASN and RFC 1997 for the value.
# ---------------------------------------------------------------------------


def test_derive_community_values():
    from app.validators import derive_community_values

    assert derive_community_values('65500:102') == '65500:102'
    assert derive_community_values('65500') == '65500:0'
    assert derive_community_values('65534') == '65534:0'
    assert derive_community_values('yandex') is None
    assert derive_community_values('') is None
    assert derive_community_values(None) is None


@pytest.mark.parametrize('name', [
    '65500:102', '65500', '65534', '64512:1', '65534:65280', '65123'
])
def test_valid_community_names(name):
    from app.validators import validate_community_name

    normalized, derived, error, _ = validate_community_name(name)
    assert error is None
    assert normalized == name.strip()
    expected = name.strip() if ':' in name else name.strip() + ':0'
    assert derived == expected


@pytest.mark.parametrize('name,fragment', [
    ('65534:0', 'зарезервировано'),
    ('65500:65536', 'вне диапазона'),
    ('100:1', 'вне приватного диапазона'),
    ('65600:1', 'вне приватного диапазона'),
    ('64511:1', 'вне приватного диапазона'),
    ('yandex', 'Недопустимое имя'),
    ('65500:', 'Недопустимое имя'),
    (':102', 'Недопустимое имя'),
    ('65500:100:1', 'Недопустимое имя'),
    ('65500: 102', 'Недопустимое имя'),
    ('', 'не может быть пустым'),
])
def test_invalid_community_names(name, fragment):
    from app.validators import validate_community_name

    normalized, derived, error, _ = validate_community_name(name)
    assert normalized is None and derived is None
    assert error and fragment in error


@pytest.mark.parametrize('raw', [
    '0', '65534:0', '65500:102', '100,200', '65280', '65500:65280',
    '65500:102, 64600:100',
])
def test_valid_community_values(raw):
    from app.validators import validate_community_ref

    normalized, error, _ = validate_community_ref(raw)
    assert error is None
    assert normalized


@pytest.mark.parametrize('raw,fragment', [
    ('65535', 'вне диапазона'),        # No Community, RFC 1997
    ('65535:0', 'вне приватного диапазона'),
    ('65600:1', 'вне приватного диапазона'),
    ('64511:1', 'вне приватного диапазона'),
    ('x', 'Недопустимое значение'),
    ('', 'не может быть пустым'),
    ('   ', 'не может быть пустым'),
])
def test_invalid_community_values(raw, fragment):
    from app.validators import validate_community_ref

    normalized, error, _ = validate_community_ref(raw)
    assert normalized is None
    assert error and fragment in error


def test_bare_value_is_read_as_a_value_not_an_asn():
    """A bare number in values is a value under 65534, never an ASN."""
    from app.validators import validate_community_ref
    from app.config_generator import _community_pairs

    normalized, error, _ = validate_community_ref('102')
    assert error is None
    assert _community_pairs(normalized) == [(65534, 102)]

    # 65500 is a plausible ASN but an out-of-range value, and it is the value
    # that gets checked — which is exactly the confusion worth pinning down.
    normalized, error, _ = validate_community_ref('65500')
    assert normalized is None
    assert 'вне диапазона' in error


def test_creation_fills_values_from_the_name(http_api, db_session):
    request, _routes, _live, _staged, _main = http_api

    status, body = request('POST', '/api/communities', {'name': '65500:7'})
    assert status == 201
    assert body['community']['values'] == '65500:7'

    status, body = request('POST', '/api/communities', {'name': '64600'})
    assert status == 201
    assert body['community']['values'] == '64600:0'


def test_creation_rejects_a_bad_name(http_api, db_session):
    request, _routes, _live, _staged, _main = http_api

    for name in ('yandex', '65534:0', '100:1', '65500:99999'):
        status, body = _request_status(request, 'POST', '/api/communities', {'name': name})
        assert status == 400, name
        assert body['error']

    # The bootstrap default exists; none of the rejected names may appear.
    status, listing = request('GET', '/api/communities')
    assert status == 200
    stored = [c['name'] for c in listing['communities']]
    assert not any(n in stored for n in ('yandex', '65534:0', '100:1', '65500:99999'))


def test_rename_rederives_values_only_when_they_were_derived(http_api, db_session):
    request, _routes, _live, _staged, _main = http_api

    status, body = request('POST', '/api/communities', {'name': '65500:102'})
    auto = body['community']['id']
    status, body = request('POST', '/api/communities', {'name': '64600:100', 'values': '64600:7'})
    explicit = body['community']['id']
    status, body = request('POST', '/api/communities', {'name': '65000', 'values': '100,200'})
    listed = body['community']['id']

    # Derived from the name: it carries no information of its own, so it moves.
    status, body = request('PUT', f'/api/communities/{auto}', {'name': '65500:103'})
    assert status == 200
    assert body['community']['values'] == '65500:103'

    # Set on purpose: the rename must not throw it away.
    status, body = request('PUT', f'/api/communities/{explicit}', {'name': '64600:101'})
    assert status == 200
    assert body['community']['values'] == '64600:7'

    # A list is never derived from a name, so it survives too.
    status, body = request('PUT', f'/api/communities/{listed}', {'name': '65100'})
    assert status == 200
    assert body['community']['values'] == '100,200'


def test_rename_must_still_be_a_valid_community(http_api, db_session):
    request, _routes, _live, _staged, _main = http_api

    status, body = request('POST', '/api/communities', {'name': '65500:102'})
    community_id = body['community']['id']

    status, body = _request_status(request, 'PUT', f'/api/communities/{community_id}',
                                   {'name': 'yandex'})
    assert status == 400
    assert body['community']['name'] if 'community' in body else True
    assert db_session.query(Community).filter_by(id=community_id).one().name == '65500:102'


def test_community_values_migration_keeps_the_generated_config_identical(tmp_path):
    """Both migration steps must not move a single prefix or tag."""
    import sqlite3 as sqlite

    from app.config_generator import (build_community_blocks, build_export_config,
                                      build_routes_config)
    from app.models import ensure_schema
    from app.validators import derive_community_values

    path = tmp_path / 'app.db'
    engine = create_engine(f'sqlite:///{path}')
    Base.metadata.create_all(bind=engine)

    peers = [{'name': 'p1', 'local_as': '65500', 'neighbor': '192.0.2.1',
              'neighbor_as': '64998', 'export_all': 0},
             {'name': 'p2', 'local_as': '64600', 'neighbor': '192.0.2.2',
              'neighbor_as': '64997', 'export_all': 0}]
    networks = [{'prefix': '203.0.113.0/24', 'enabled': True, 'community_ids': [1]},
                {'prefix': '198.51.100.0/24', 'enabled': True, 'community_ids': [2]}]
    before = [{'id': 1, 'name': '65534', 'values': '0', 'description': 'default', 'enabled': True},
              {'id': 2, 'name': '65500:102', 'values': '', 'description': 'amazon', 'enabled': True},
              {'id': 3, 'name': '64600:100', 'values': '', 'description': 'geo', 'enabled': True}]
    addrs = ['192.0.2.1/32', '192.0.2.2/32']

    export_before = build_export_config(networks, before, addrs, peers)
    routes_before = build_routes_config(networks, peers)
    define_before = build_community_blocks(before)

    with engine.begin() as connection:
        connection.execute(text(
            'INSERT INTO communities (id, name, "values", description, enabled) '
            'VALUES (:id, :name, :values, :description, :enabled)'
        ), [{'id': 1, 'name': '65534', 'values': '0', 'description': 'default', 'enabled': 1},
            {'id': 2, 'name': '65500:102', 'values': '', 'description': 'amazon', 'enabled': 1},
            {'id': 3, 'name': '64600:100', 'values': '', 'description': 'geo', 'enabled': 1}])

    ensure_schema(engine)

    with engine.connect() as migrated:
        rows = migrated.execute(text(
            'SELECT id, name, "values", description, enabled FROM communities '
            'ORDER BY id')).fetchall()
    after = [{'id': r[0], 'name': r[1], 'values': r[2], 'description': r[3],
              'enabled': r[4]} for r in rows]

    # Both steps: empty values take the name, and the default '0' is respelled.
    assert [c['values'] for c in after] == ['65534:0', '65500:102', '64600:100']
    assert derive_community_values('65534') == '65534:0'

    assert build_export_config(networks, after, addrs, peers) == export_before
    assert build_routes_config(networks, peers) == routes_before
    assert build_community_blocks(after) == define_before
    assert sqlite.connect(path).execute('SELECT COUNT(*) FROM communities').fetchone()[0] == 3


def test_migration_skips_a_name_that_is_not_a_community(tmp_path):
    from app.models import ensure_schema

    path = tmp_path / 'labels.db'
    engine = create_engine(f'sqlite:///{path}')
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(text(
            'INSERT INTO communities (name, "values", description, enabled) '
            'VALUES (:name, :values, :description, :enabled)'
        ), [{'name': 'yandex', 'values': '', 'description': 'label', 'enabled': 1}])

    ensure_schema(engine)

    with engine.connect() as connection:
        row = connection.execute(text(
            'SELECT "values" FROM communities WHERE name = :name'), {'name': 'yandex'}).one()
    # A bare name is never copied verbatim: '65534' would become (65534, 65534).
    assert row[0] == ''


def test_default_community_is_created_with_the_derived_value(tmp_path):
    from app.crud_operations import CRUDOperations

    path = tmp_path / 'default.db'
    engine = create_engine(f'sqlite:///{path}')
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()

    community = CRUDOperations.ensure_default_community(session)

    assert community.name == '65534'
    assert community.values == '65534:0'
    from app.config_generator import _community_pairs
    assert _community_pairs(community.values) == [(65534, 0)]
