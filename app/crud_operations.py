"""CRUD operations for Network, Community, and AuditLog using SQLAlchemy."""

import json

from sqlalchemy.orm import Session

try:
    from .models import Network, Community, AuditLog, BgpPeer
    from .parsing_engine import ParsingEngine, network_sort_key
    from .config_generator import _as_community_ids
except ImportError:
    from models import Network, Community, AuditLog, BgpPeer
    from parsing_engine import ParsingEngine, network_sort_key
    from config_generator import _as_community_ids


class CRUDOperations:
    """Database operations for all models."""

    #: Shared parser instance used to keep stored prefixes canonical. The
    #: routes ask for confirmation first; this is the backstop that stops a
    #: hand-crafted request from storing a prefix ``bird -p`` would only
    #: reject at Apply time.
    _parser = ParsingEngine()

    @staticmethod
    def get_networks(session: Session):
        """Get all networks from the database.

        Args:
            session: SQLAlchemy session.

        Order is pinned with an explicit sort rather than left to whatever order the
        query planner happens to use: the generated routes.conf follows this
        order, so it has to be part of the contract.

        Returns:
            List of Network objects.
        """
        return session.query(Network).order_by(Network.id).all()

    @staticmethod
    def get_network_by_id(session: Session, network_id: int):
        """Get a single network by ID.

        Args:
            session: SQLAlchemy session.
            network_id: Network ID.

        Returns:
            Network object or None.
        """
        return session.query(Network).filter(Network.id == network_id).first()

    @staticmethod
    def ensure_default_community(session: Session):
        communities = sorted(
            [comm for comm in session.query(Community).all()
             if (comm.name or '').strip() == '65534'],
            key=lambda comm: comm.id,
        )
        if not communities:
            community = Community(
                name='65534',
                # The same (65534, 0) pair the bare '0' described, spelled so
                # a rename recognises it as derived and re-derives it.
                values='65534:0',
                description='default',
                enabled=True,
            )
            session.add(community)
            session.commit()
            return community

        canonical = communities[0]
        if not (canonical.description or '').strip():
            canonical.description = 'default'
        if not (canonical.values or '').strip():
            canonical.values = '0'
        duplicate_ids = {comm.id for comm in communities[1:]}
        for network in session.query(Network).all():
            current_ids = _as_community_ids(network.community_ids)
            if not current_ids:
                current_ids = [canonical.id]
            else:
                current_ids = [canonical.id if cid in duplicate_ids else cid for cid in current_ids]
            current_ids = list(dict.fromkeys(current_ids))
            if current_ids != _as_community_ids(network.community_ids):
                network.community_ids = json.dumps(current_ids)
        for duplicate in communities[1:]:
            session.delete(duplicate)
        session.commit()
        return canonical

    @staticmethod
    def create_network(session: Session, prefix: str, description: str = None,
                       status: str = 'pending', community_ids: str = None,
                       enabled: bool = True, user_id: str = 'system'):
        """Create a new network record.

        Args:
            session: SQLAlchemy session.
            prefix: CIDR prefix string.
            description: Optional description.
            status: Network status (default: 'pending').
            community_ids: Comma-separated community IDs or None.
            user_id: User who performed the action.

        Returns:
            Created Network object.
        """
        if not _as_community_ids(community_ids):
            default_community = CRUDOperations.ensure_default_community(session)
            community_ids = json.dumps([default_community.id])

        canonical = CRUDOperations._parser.canonical_prefix(prefix)
        if canonical is None:
            raise ValueError(f'Некорректный префикс: {prefix!r}')
        prefix = canonical

        existing = session.query(Network).filter(Network.prefix == prefix).first()
        if existing is not None:
            return existing

        new_net = Network(
            prefix=prefix,
            description=description,
            status=status,
            community_ids=community_ids,
            enabled=enabled,
        )
        session.add(new_net)
        session.commit()

        # Audit log
        audit = AuditLog(
            action='CREATE_NETWORK',
            user_id=user_id,
            diff_summary=f'Added network: {prefix}',
        )
        session.add(audit)
        session.commit()
        return new_net

    @staticmethod
    def update_network(session: Session, network_id: int, **updates):
        """Update an existing network record.

        Args:
            session: SQLAlchemy session.
            network_id: Network ID to update.
            **updates: Key-value pairs of fields to update.

        Returns:
            Updated Network object or None if not found.
        """
        net = session.query(Network).filter(Network.id == network_id).first()
        if net:
            for key, value in updates.items():
                if hasattr(net, key):
                    setattr(net, key, value)
            net.prefix = CRUDOperations._parser.canonical_prefix(net.prefix)
            if net.prefix is None:
                raise ValueError('Некорректный префикс')
            session.commit()

            audit = AuditLog(
                action='UPDATE_NETWORK',
                user_id='system',
                diff_summary=f'Updated network {network_id}: {updates}',
            )
            session.add(audit)
            session.commit()
        return net

    @staticmethod
    def delete_network(session: Session, network_id: int):
        """Delete a network record.

        Args:
            session: SQLAlchemy session.
            network_id: Network ID to delete.

        Returns:
            Deleted Network object or None if not found.
        """
        net = session.query(Network).filter(Network.id == network_id).first()
        if net:
            session.delete(net)
            session.commit()

            audit = AuditLog(
                action='DELETE_NETWORK',
                user_id='system',
                diff_summary=f'Deleted network {network_id}: {net.prefix}',
            )
            session.add(audit)
            session.commit()
        return net

    @staticmethod
    def get_bgp_peers(session: Session):
        """Get all BGP peers from the database.

        Args:
            session: SQLAlchemy session.

        Returns:
            List of BgpPeer objects.
        """
        return session.query(BgpPeer).order_by(BgpPeer.name).all()

    @staticmethod
    def get_bgp_peer_by_id(session: Session, peer_id: int):
        """Get a single BGP peer by ID."""
        return session.query(BgpPeer).filter(BgpPeer.id == peer_id).first()

    @staticmethod
    def create_bgp_peer(session: Session, name: str, local_as: str = '65500',
                        neighbor: str = '', neighbor_as: str = '',
                        status: str = 'active', user_id: str = 'system',
                        export_all: bool = False):
        """Create a new BGP peer record.

        Args:
            session: SQLAlchemy session.
            name: BIRD protocol instance name.
            local_as: Local ASN (default: '65500').
            neighbor: IPv4 address of the peer.
            neighbor_as: ASN of the peer.
            status: Peer status (default: 'active').
            user_id: User who performed the action.
            export_all: Send every network to this peer instead of only the
                prefixes whose community ASN matches its local AS.

        Returns:
            Created BgpPeer object.
        """
        peer = BgpPeer(
            name=name,
            local_as=local_as,
            neighbor=neighbor,
            neighbor_as=neighbor_as,
            status=status,
            export_all=bool(export_all),
        )
        session.add(peer)
        session.commit()

        audit = AuditLog(
            action='CREATE_BGP_PEER',
            user_id=user_id,
            diff_summary=f'Added BGP peer {name}: neighbor {neighbor} as {neighbor_as}',
        )
        session.add(audit)
        session.commit()
        return peer

    @staticmethod
    def delete_bgp_peer(session: Session, peer_id: int, user_id: str = 'system'):
        """Delete a BGP peer record.

        Returns:
            Deleted BgpPeer object or None if not found.
        """
        peer = session.query(BgpPeer).filter(BgpPeer.id == peer_id).first()
        if peer:
            session.delete(peer)
            session.commit()

            audit = AuditLog(
                action='DELETE_BGP_PEER',
                user_id=user_id,
                diff_summary=f'Deleted BGP peer {peer_id}: {peer.name}',
            )
            session.add(audit)
            session.commit()
        return peer

    @staticmethod
    def get_communities(session: Session):
        """Get all communities from the database.

        Args:
            session: SQLAlchemy session.

        Returns:
            List of Community objects.
        """
        return session.query(Community).all()

    @staticmethod
    def get_community_by_id(session: Session, community_id: int):
        """Get a single community by ID.

        Args:
            session: SQLAlchemy session.
            community_id: Community ID.

        Returns:
            Community object or None.
        """
        return session.query(Community).filter(Community.id == community_id).first()

    @staticmethod
    def create_community(session: Session, name: str, values: str = '',
                         description: str = None, enabled: bool = True,
                         user_id: str = 'system'):
        """Create a new community record.

        Args:
            session: SQLAlchemy session.
            name: Community name.
            values: Comma-separated values or configuration for the community.
            description: Optional description.
            user_id: User who performed the action.

        Returns:
            Created Community object.
        """
        new_comm = Community(
            name=name,
            values=values,
            description=description,
            enabled=enabled,
        )
        session.add(new_comm)
        session.commit()

        audit = AuditLog(
            action='CREATE_COMMUNITY',
            user_id=user_id,
            diff_summary=f'Added community: {name}',
        )
        session.add(audit)
        session.commit()
        return new_comm

    @staticmethod
    def update_community(session: Session, community_id: int, **updates):
        """Update an existing community record.

        Args:
            session: SQLAlchemy session.
            community_id: Community ID to update.
            **updates: Key-value pairs of fields to update.

        Returns:
            Updated Community object or None if not found.
        """
        comm = session.query(Community).filter(Community.id == community_id).first()
        if comm:
            for key, value in updates.items():
                if hasattr(comm, key):
                    setattr(comm, key, value)
            session.commit()

            audit = AuditLog(
                action='UPDATE_COMMUNITY',
                user_id='system',
                diff_summary=f'Updated community {community_id}: {updates}',
            )
            session.add(audit)
            session.commit()
        return comm

    @staticmethod
    def delete_community(session: Session, community_id: int):
        """Delete a community record.

        Args:
            session: SQLAlchemy session.
            community_id: Community ID to delete.

        Returns:
            Deleted Community object or None if not found.
        """
        comm = session.query(Community).filter(Community.id == community_id).first()
        if comm:
            related = [
                network for network in session.query(Network).all()
                if comm.id in _as_community_ids(network.community_ids)
            ]
            for network in related:
                session.delete(network)
            session.delete(comm)
            session.commit()

            audit = AuditLog(
                action='DELETE_COMMUNITY',
                user_id='system',
                diff_summary=f'Deleted community and {len(related)} related networks: {comm.name}',
            )
            session.add(audit)
            session.commit()
        return comm

    @staticmethod
    def count_networks(session: Session) -> int:
        """Get the total number of network records."""
        return session.query(Network).count()

    @staticmethod
    def count_communities(session: Session) -> int:
        """Get the total number of community records."""
        return session.query(Community).count()

    @staticmethod
    def count_bgp_peers(session: Session) -> int:
        """Get the total number of BGP peer records."""
        return session.query(BgpPeer).count()

    @staticmethod
    def bulk_commit_networks(session: Session, networks: list, user_id: str = 'system'):
        """Commit already-parsed network records with deduplication by prefix.

        Args:
            session: SQLAlchemy session.
            networks: List of dicts parsed by ParsingEngine.parse_and_map
                (keys: prefix, description, status, community_ids).
            user_id: User who performed the action.

        Returns:
            Dict with 'created', 'skipped_duplicates' and 'audit_id'.
        """
        # Canonicalize every candidate, then order the batch by network address
        # before it is inserted. Row order is what get_networks returns, and
        # that order is what lands in routes.conf, so sorting here — once —
        # is enough; the generator never has to sort. Sorting by address
        # rather than by string matters: 10.0.0.0/8 contains 10.0.0.0/24, but
        # as text "/24" sorts first.
        candidates = []
        unparseable = 0
        for item in networks:
            prefix = item.get('prefix')
            if not prefix:
                continue
            canonical = CRUDOperations._parser.canonical_prefix(prefix)
            if canonical is None:
                unparseable += 1
                continue
            candidates.append(dict(item, prefix=canonical))

        candidates.sort(key=lambda entry: network_sort_key(entry['prefix']))

        # Deduplicate within the incoming list first. Prefixes are canonical
        # by now, so 10.9.240.0/12 and 10.0.0.0/12 collapse into one row.
        seen_in_batch = set()
        unique_networks = []
        batch_duplicates = 0
        for item in candidates:
            if item['prefix'] in seen_in_batch:
                batch_duplicates += 1
                continue
            seen_in_batch.add(item['prefix'])
            unique_networks.append(item)

        # Deduplicate against existing database records.
        existing_prefixes = {net.prefix for net in session.query(Network).all()}
        to_create = [item for item in unique_networks if item['prefix'] not in existing_prefixes]
        skipped = batch_duplicates + (len(unique_networks) - len(to_create))

        created = 0
        default_community = CRUDOperations.ensure_default_community(session) if to_create else None
        for item in to_create:
            community_ids = item.get('community_ids') or []
            if not _as_community_ids(community_ids):
                community_ids = [default_community.id]
            net = Network(
                prefix=item['prefix'],
                description=item.get('description', ''),
                status=item.get('status', 'pending'),
                community_ids=json.dumps(community_ids) if isinstance(community_ids, list) else community_ids,
                enabled=item.get('enabled', True),
            )
            session.add(net)
            created += 1

        audit_id = None
        if created > 0:
            session.commit()
            audit = AuditLog(
                action='BULK_IMPORT_COMMIT',
                user_id=user_id,
                diff_summary=f'Bulk committed {created} networks, skipped {skipped} duplicates',
            )
            session.add(audit)
            session.commit()
            audit_id = audit.id

        return {'created': created, 'skipped_duplicates': skipped,
                'skipped_invalid': unparseable, 'audit_id': audit_id}

    @staticmethod
    def get_audit_log(session: Session, limit: int = 50):
        """Get recent audit log entries.

        Args:
            session: SQLAlchemy session.
            limit: Maximum number of records to return.

        Returns:
            List of AuditLog objects.
        """
        return session.query(AuditLog).order_by(AuditLog.id.desc()).limit(limit).all()
