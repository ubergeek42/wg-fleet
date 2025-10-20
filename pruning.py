import asyncio
from datetime import datetime, timedelta
import logging

from models import Client
from config import parse_duration
import wireguard
import hosts

logger = logging.getLogger(__name__)

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
    cutoff = datetime.utcnow() - parse_duration(config.prune_timeout)

    for fleet_name, fleet_config in config.fleets.items():
        try:
            # Get WireGuard peers with handshake times
            wg_peers = wireguard.list_peers(fleet_name)

            with session_factory() as session:
                for peer in wg_peers:
                    # Skip if no handshake data
                    if peer['last_handshake'] is None:
                        continue

                    # Check if stale
                    if peer['last_handshake'] < cutoff:
                        # Remove from WireGuard
                        wireguard.remove_peer(fleet_name, peer['public_key'])

                        # Remove from database
                        client = session.query(Client).filter_by(
                            fleet_id=fleet_name,
                            public_key=peer['public_key']
                        ).first()

                        if client:
                            session.delete(client)
                            prune_count += 1
                            logger.debug(f"Pruned client: {client.assigned_ip}")

                session.commit()

        except Exception as e:
            logger.error(f"Error pruning fleet {fleet_name}: {e}", exc_info=True)

    # Regenerate hosts file after pruning
    if prune_count > 0:
        hosts.regenerate_hosts_file(config, session_factory)

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
