#!/usr/bin/env python3
import asyncio
import logging
import sys
import argparse
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
import uvicorn

from config import load_config
from database import init_db, get_session_factory
from routes import create_app
from pruning import prune_stale_clients
from models import Client
import wireguard
from hook_manager import trigger_hooks, EventType, HookContext
import hooks  # Import to register hooks

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

def setup_fleet_interface(fleet_name: str, fleet_config):
    """
    Setup WireGuard interface for a fleet.

    Args:
        fleet_name: Name of the fleet
        fleet_config: Fleet configuration object
    """
    config_path = Path(f'/etc/wireguard/wg_{fleet_name}.conf')

    # Check if config exists
    if not config_path.exists():
        logger.info(f"Creating new interface config for fleet: {fleet_name}")
        # Generate server keypair
        private_key, public_key = wireguard.generate_keypair()

        # Create config file
        wireguard.create_interface_config(fleet_name, fleet_config, private_key, str(config_path))
    else:
        # Keep the server key, but apply address/port changes from the fleet config
        private_key = wireguard.read_private_key(str(config_path))
        if config_path.read_text() != wireguard.render_interface_config(fleet_config, private_key):
            logger.warning(
                f"Config for fleet {fleet_name} changed; rewriting wg_{fleet_name}.conf and restarting "
                f"the interface. Existing clients will need to re-register."
            )
            wireguard.create_interface_config(fleet_name, fleet_config, private_key, str(config_path))
            if wireguard.interface_exists(fleet_name):
                wireguard.bring_down_interface(fleet_name)
        else:
            logger.info(f"Using existing interface config for fleet: {fleet_name}")

    # Bring up interface
    try:
        wireguard.bring_up_interface(fleet_name)
    except Exception as e:
        logger.error(f"Failed to bring up interface wg_{fleet_name}: {e}")
        raise

def reconcile_fleet_state(fleet_name: str, session_factory):
    """
    Reconcile database and WireGuard state.

    - Remove WireGuard peers not in database
    - Remove database clients not in WireGuard

    Args:
        fleet_name: Name of the fleet
        session_factory: SQLAlchemy session factory
    """
    logger.info(f"Reconciling state for fleet: {fleet_name}")

    # Get WireGuard peers
    wg_peers = wireguard.list_peers(fleet_name)
    wg_pubkeys = {peer['public_key'] for peer in wg_peers}

    # Get database clients
    with session_factory() as session:
        db_clients = session.query(Client).filter_by(fleet_id=fleet_name).all()
        db_pubkeys = {client.public_key for client in db_clients}

        # Remove WG peers not in DB
        for pubkey in wg_pubkeys - db_pubkeys:
            logger.info(f"Removing WireGuard peer not in database: {pubkey[:16]}...")
            wireguard.remove_peer(fleet_name, pubkey)

        # Remove DB clients not in WG
        for client in db_clients:
            if client.public_key not in wg_pubkeys:
                logger.info(f"Removing database client not in WireGuard: {client.assigned_ip}")
                session.delete(client)

        session.commit()

def remove_unconfigured_fleet_clients(config, session_factory):
    """
    Remove database clients of fleets that are no longer in the config.

    Pruning and reconciliation only look at configured fleets, so these would
    otherwise stay in the generated hosts/Prometheus/Ansible files forever.

    Args:
        config: Application configuration
        session_factory: SQLAlchemy session factory
    """
    with session_factory() as session:
        stale = session.query(Client).filter(Client.fleet_id.notin_(list(config.fleets))).all()
        for client in stale:
            logger.info(f"Removing client of unconfigured fleet {client.fleet_id}: {client.assigned_ip}")
            session.delete(client)
        session.commit()

def initialize_fleets(config, session_factory):
    """
    Bring up WireGuard interfaces, reconcile state, and run startup hooks.

    Args:
        config: Application configuration
        session_factory: SQLAlchemy session factory
    """
    remove_unconfigured_fleet_clients(config, session_factory)

    for fleet_name, fleet_config in config.fleets.items():
        setup_fleet_interface(fleet_name, fleet_config)
        reconcile_fleet_state(fleet_name, session_factory)

    # Generate initial hosts file etc. via hook system
    trigger_hooks(HookContext(
        event_type=EventType.STARTUP,
        config=config,
        session_factory=session_factory
    ))

def make_lifespan(config, session_factory):
    """
    Build the app lifespan, which runs the background pruning task.

    Args:
        config: Application configuration
        session_factory: SQLAlchemy session factory
    """
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        prune_task = asyncio.create_task(prune_stale_clients(config, session_factory))
        logger.info("Server started successfully")
        yield
        logger.info("Shutting down wg-fleet server")
        prune_task.cancel()

    return lifespan

def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description='WireGuard Fleet Manager')
    parser.add_argument('--config', default='/etc/wg-fleet.yaml',
                      help='Path to configuration file')
    args = parser.parse_args()

    try:
        logger.info("Starting wg-fleet server")
        logger.info(f"Loading configuration from: {args.config}")
        config = load_config(args.config)

        engine = init_db()
        session_factory = get_session_factory(engine)

        initialize_fleets(config, session_factory)
    except Exception as e:
        logger.error(f"Startup failed: {e}", exc_info=True)
        sys.exit(1)

    app = create_app(config, session_factory, engine,
                     lifespan=make_lifespan(config, session_factory))

    # Run server on all interfaces (IPv4 and IPv6)
    # proxy_headers=True enables trusting X-Forwarded-* headers from reverse proxies
    # forwarded_allow_ips restricts which IPs we trust proxy headers from (localhost/nginx)
    uvicorn.run(
        app,
        host=["::", "0.0.0.0"],
        port=8000,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1,::1"
    )

if __name__ == "__main__":
    main()
