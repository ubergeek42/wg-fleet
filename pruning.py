import asyncio
from datetime import datetime, timedelta, UTC
import logging

from models import Client
from config import parse_duration
import wireguard
from hook_manager import trigger_hooks, EventType, HookContext
import hooks

logger = logging.getLogger(__name__)

def as_utc(dt: datetime) -> datetime:
    """Database timestamps are naive but represent UTC"""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt

def prune_stale_clients_once(config, session_factory) -> int:
    """
    Run one pruning cycle across all fleets.

    Args:
        config: Application configuration
        session_factory: SQLAlchemy session factory

    Returns:
        Number of clients pruned
    """
    prune_count = 0
    cutoff = datetime.now(UTC) - parse_duration(config.prune_timeout)

    for fleet_name in config.fleets:
        try:
            # Get WireGuard peers with handshake times
            wg_peers = wireguard.list_peers(fleet_name)
        except Exception as e:
            logger.error(f"Error listing peers for fleet {fleet_name}: {e}", exc_info=True)
            continue

        with session_factory() as session:
            for peer in wg_peers:
                # Get client from database
                client = session.query(Client).filter_by(
                    fleet_id=fleet_name,
                    public_key=peer['public_key']
                ).first()

                should_prune = False

                # Check if client should be pruned
                if peer['last_handshake'] is None:
                    # Never connected - check registration timestamp
                    if client and as_utc(client.timestamp) < cutoff:
                        should_prune = True
                else:
                    # Has connected - check handshake timestamp
                    if peer['last_handshake'] < cutoff:
                        should_prune = True

                if not should_prune:
                    continue

                # Commit per client so one failure doesn't roll back the others
                try:
                    wireguard.remove_peer(fleet_name, peer['public_key'])
                    if client:
                        session.delete(client)
                        session.commit()
                        prune_count += 1
                        logger.debug(f"Pruned client: {client.assigned_ip}")
                except Exception as e:
                    session.rollback()
                    logger.error(f"Error pruning peer {peer['public_key'][:16]}... in fleet {fleet_name}: {e}",
                                 exc_info=True)

            # Remove stale records whose peer is no longer in WireGuard (e.g. the peer was
            # removed but deleting the record failed). Only stale ones, so a client whose
            # registration is in progress is never touched.
            try:
                wg_pubkeys = {peer['public_key'] for peer in wg_peers}
                for client in session.query(Client).filter_by(fleet_id=fleet_name).all():
                    if client.public_key not in wg_pubkeys and as_utc(client.timestamp) < cutoff:
                        session.delete(client)
                        prune_count += 1
                        logger.info(f"Removed stale client with no WireGuard peer: {client.assigned_ip}")
                session.commit()
            except Exception as e:
                session.rollback()
                logger.error(f"Error removing orphaned clients in fleet {fleet_name}: {e}", exc_info=True)

    # Trigger hooks after pruning
    if prune_count > 0:
        trigger_hooks(HookContext(
            event_type=EventType.CLIENT_REMOVED,
            config=config,
            session_factory=session_factory,
            client_data={'count': prune_count}
        ))

    return prune_count

async def prune_stale_clients(config, session_factory, interval: int = 300):
    """
    Background task that runs periodically to prune inactive clients.

    Args:
        config: Application configuration
        session_factory: SQLAlchemy session factory
        interval: Seconds between pruning cycles (default 300 = 5 minutes)
    """
    while True:
        try:
            await asyncio.sleep(interval)

            logger.info("Starting pruning cycle")
            prune_count = prune_stale_clients_once(config, session_factory)

            if prune_count > 0:
                logger.info(f"Pruned {prune_count} stale clients")
            else:
                logger.debug("No stale clients to prune")

        except Exception as e:
            logger.error(f"Error in pruning task: {e}", exc_info=True)
