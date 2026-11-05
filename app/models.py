from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, String, Text, inspect, text
from sqlalchemy.orm import declarative_base
from sqlalchemy.schema import CreateTable

try:
    from .utils import local_now
except ImportError:  # pragma: no cover - direct script execution
    from utils import local_now

Base = declarative_base()

# Temporary table name used while rebuilding bgp_peers (see
# :func:`_ensure_bgp_peer_neighbor_unique`).
BGP_PEER_MIGRATION_TABLE = 'bgp_peers__migrated'


def _has_unique_index_on(connection, table_name, column_name):
    """True when *table_name* carries a UNIQUE index over *column_name*.

    Covers both forms SQLite uses: a table-level ``UNIQUE`` clause, which shows
    up as an implicit ``sqlite_autoindex_*`` entry, and a separately created
    unique index.
    """
    for row in connection.execute(text(f"PRAGMA index_list('{table_name}')")):
        if not row[2]:
            continue
        columns = [
            entry[2]
            for entry in connection.execute(text(f"PRAGMA index_info('{row[1]}')"))
        ]
        if columns == [column_name]:
            return True
    return False


def _ensure_bgp_peer_neighbor_unique(connection):
    """Add UNIQUE(bgp_peers.neighbor) to a database created by an older release.

    ``create_all`` only creates missing tables, so upgrading an existing
    install leaves the old table definition in place. The replacement table is
    generated from the model itself, which keeps the DDL from drifting away
    from :class:`BgpPeer`.

    Duplicates are never dropped: when any are found the rebuild is skipped and
    the offending addresses are reported, leaving the application-level
    validation in charge until an operator resolves them. Runs inside the
    caller's transaction, so a failure rolls the table back untouched.
    """
    if _has_unique_index_on(connection, 'bgp_peers', 'neighbor'):
        return

    duplicates = connection.execute(text(
        'SELECT neighbor, COUNT(*) FROM bgp_peers '
        'GROUP BY neighbor HAVING COUNT(*) > 1'
    )).fetchall()
    if duplicates:
        listed = ', '.join(f'{row[0]} (x{row[1]})' for row in duplicates)
        print(
            '[ERROR] bgp_peers: neighbor duplicates found, UNIQUE not added: '
            f'{listed}. Resolve them and restart to add the constraint.',
            flush=True,
        )
        return

    metadata = MetaData()
    migrated = BgpPeer.__table__.to_metadata(metadata, name=BGP_PEER_MIGRATION_TABLE)
    connection.execute(text(f'DROP TABLE IF EXISTS {BGP_PEER_MIGRATION_TABLE}'))
    connection.execute(CreateTable(migrated))
    column_list = ', '.join(column.name for column in BgpPeer.__table__.columns)
    connection.execute(text(
        f'INSERT INTO {BGP_PEER_MIGRATION_TABLE} ({column_list}) '
        f'SELECT {column_list} FROM bgp_peers'
    ))
    connection.execute(text('DROP TABLE bgp_peers'))
    connection.execute(text(
        f'ALTER TABLE {BGP_PEER_MIGRATION_TABLE} RENAME TO bgp_peers'
    ))
    print(
        '[INFO] bgp_peers: UNIQUE(neighbor) added to the existing table',
        flush=True,
    )


def _materialise_community_values(connection):
    """Store the community value each name implies, so behaviour stops
    depending on the name's shape.

    Two steps, both export-neutral because a pair is unchanged:

    * an empty ``values`` becomes what the name implies — ``65500:102``
      stays as it is, a bare ``65500`` becomes ``65500:0``;
    * the default community keeps ``'0'`` rewritten as ``'65534:0'``, the
      same ``(65534, 0)`` pair. Without this it would read as an explicit
      value and a rename would stop updating it.

    A bare name is deliberately not copied verbatim: ``65534`` parsed as a
    bare number means ``(65534, 65534)``, not the default community.
    """
    try:
        from .validators import derive_community_values, validate_community_ref
    except ImportError:  # pragma: no cover - direct script execution
        from validators import derive_community_values, validate_community_ref

    rows = connection.execute(text(
        'SELECT id, name, "values" FROM communities'
    )).fetchall()

    changed = 0
    for community_id, name, values in rows:
        current = (values or '').strip()
        name_text = (name or '').strip()

        if not current:
            derived = derive_community_values(name_text)
            if derived is None:
                continue                      # a label, needs an explicit value
            _, error, _ = validate_community_ref(derived)
            if error:
                continue
            connection.execute(
                text('UPDATE communities SET "values" = :values WHERE id = :id'),
                {'values': derived, 'id': community_id},
            )
            changed += 1
            print(f'[INFO] communities: id={community_id} '
                  f'values <- name ({derived})', flush=True)
        elif name_text == '65534' and current == '0':
            # Same pair, spelled the way a rename can recognise.
            connection.execute(
                text('UPDATE communities SET "values" = :values WHERE id = :id'),
                {'values': '65534:0', 'id': community_id},
            )
            changed += 1
            print(f'[INFO] communities: id={community_id} '
                  f'values 0 -> 65534:0', flush=True)

    return changed


def ensure_schema(engine):
    """Create missing tables and add the manager's enabled columns."""
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        inspector = inspect(connection)
        for table in ('networks', 'communities'):
            columns = {column['name'] for column in inspector.get_columns(table)}
            if 'enabled' not in columns:
                connection.execute(text(
                    f'ALTER TABLE {table} ADD COLUMN enabled BOOLEAN NOT NULL DEFAULT 1'
                ))
        # Per-peer export scope. Additive, so an ALTER is enough and the
        # existing rows keep exporting as before — which is the ASN-scoped
        # default.
        peer_columns = {column['name'] for column in inspector.get_columns('bgp_peers')}
        if 'export_all' not in peer_columns:
            connection.execute(text(
                'ALTER TABLE bgp_peers ADD COLUMN export_all BOOLEAN NOT NULL DEFAULT 0'
            ))
            print('[INFO] bgp_peers: export_all added', flush=True)

        try:
            _ensure_bgp_peer_neighbor_unique(connection)
        except Exception as exc:
            print(
                f'[ERROR] bgp_peers UNIQUE(neighbor) migration failed: '
                f'{type(exc).__name__}: {exc}',
                flush=True,
            )
            raise
        try:
            _materialise_community_values(connection)
        except Exception as exc:
            print(
                f'[ERROR] communities values migration failed: '
                f'{type(exc).__name__}: {exc}',
                flush=True,
            )
            raise


class Network(Base):
    __tablename__ = 'networks'

    id = Column(Integer, primary_key=True)
    prefix = Column(String(255), nullable=False)
    description = Column(Text)
    status = Column(String(50))
    community_ids = Column(Text)  # Stored as comma-separated string or JSON
    enabled = Column(Boolean, nullable=False, default=True, server_default=text('1'))


class Community(Base):
    __tablename__ = 'communities'

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    values = Column(Text)
    description = Column(Text)
    enabled = Column(Boolean, nullable=False, default=True, server_default=text('1'))


class AuditLog(Base):
    __tablename__ = 'audit_log'

    id = Column(Integer, primary_key=True)
    action = Column(String(100))
    timestamp = Column(DateTime, default=local_now)
    user_id = Column(String(50))
    diff_summary = Column(Text)


class BgpPeer(Base):
    __tablename__ = 'bgp_peers'

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False, unique=True)  # BIRD protocol instance name
    local_as = Column(String(20), nullable=False, default='65500')
    # One peer per address: BIRD hands an incoming session to the first
    # matching protocol. Several sessions to one MikroTik are possible, but
    # each needs its own address there, so ``neighbor`` stays unique.
    neighbor = Column(String(45), nullable=False, unique=True)
    neighbor_as = Column(String(20), nullable=False)
    status = Column(String(50), default='active')
    # Opt out of ASN-scoped export. By default a peer is only sent the prefixes
    # whose community ASN equals its own local AS, so one MikroTik holding
    # several ASes gets a different prefix set over each session.
    export_all = Column(Boolean, nullable=False, default=False)
