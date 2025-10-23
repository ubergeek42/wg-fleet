#!/usr/bin/env python3
import asyncio
import logging
import sys
import argparse
from pathlib import Path

from fastapi import FastAPI
import uvicorn

from config import load_config
from database import init_db, get_session_factory
from routes import create_app
from pruning import prune_stale_clients
from models import Client
import wireguard
import hosts

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Global state
app_config = None
session_factory = None
engine = None

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
        wireguard.create_interface_config(fleet_name, fleet_config, private_key)
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

async def startup():
    """Application startup logic"""
    global app_config, session_factory, engine

    try:
        logger.info("Starting wg-fleet server")

        # Parse CLI arguments
        parser = argparse.ArgumentParser(description='WireGuard Fleet Manager')
        parser.add_argument('--config', default='/etc/wg-fleet.yaml',
                          help='Path to configuration file')
        args = parser.parse_args()

        # Load config
        logger.info(f"Loading configuration from: {args.config}")
        app_config = load_config(args.config)

        # Initialize database
        engine = init_db()
        session_factory = get_session_factory(engine)

        # Setup WireGuard interfaces for each fleet
        for fleet_name, fleet_config in app_config.fleets.items():
            setup_fleet_interface(fleet_name, fleet_config)
            reconcile_fleet_state(fleet_name, session_factory)

        # Generate initial hosts file
        hosts.regenerate_hosts_file(app_config, session_factory)

        # Start background pruning task
        asyncio.create_task(prune_stale_clients(app_config, session_factory))

        logger.info("Server started successfully")

    except Exception as e:
        logger.error(f"Startup failed: {e}", exc_info=True)
        sys.exit(1)

async def shutdown():
    """Application shutdown logic"""
    logger.info("Shutting down wg-fleet server")

def main():
    """Main entry point"""
    # Create FastAPI app
    from config import load_config
    from database import init_db, get_session_factory

    # Load config for app creation
    parser = argparse.ArgumentParser(description='WireGuard Fleet Manager')
    parser.add_argument('--config', default='/etc/wg-fleet.yaml',
                      help='Path to configuration file')
    args = parser.parse_args()

    config = load_config(args.config)
    engine = init_db()
    session_factory = get_session_factory(engine)

    app = create_app(config, session_factory, engine)

    # Register startup/shutdown
    app.add_event_handler("startup", startup)
    app.add_event_handler("shutdown", shutdown)

    # Run server on all interfaces (IPv4 and IPv6)
    uvicorn.run(app, host=["::", "0.0.0.0"], port=8000)

if __name__ == "__main__":
    main()
