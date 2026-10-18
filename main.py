#!/usr/bin/env python3
import asyncio
import logging
import sys
import argparse
from contextlib import asynccontextmanager

from fastapi import FastAPI
import uvicorn

from config import load_config
from database import init_db, get_session_factory
from routes import create_app
from pruning import prune_stale_clients
from fleet_source import FleetSource
from fleet_registry import initialize_fleets, refresh_fleets
import host_ca
import hooks  # Import to register hooks

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

def make_lifespan(config, session_factory, source=None):
    """
    Build the app lifespan, which runs the background pruning and fleet refresh tasks.

    Args:
        config: Application configuration
        session_factory: SQLAlchemy session factory
        source: fleets.json source, if fleets_url is set
    """
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = [asyncio.create_task(prune_stale_clients(config, session_factory))]
        if source is not None:
            tasks.append(asyncio.create_task(refresh_fleets(config, session_factory, source)))
        logger.info("Server started successfully")
        yield
        logger.info("Shutting down wg-fleet server")
        for task in tasks:
            task.cancel()

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
        host_ca.ensure_host_ca(comment=f"wg-fleet host CA {config.server_name or 'wg-fleet'}")
        session_factory = get_session_factory(engine)

        source = FleetSource(config) if config.fleets_url else None
        initialize_fleets(config, session_factory, source)
    except Exception as e:
        logger.error(f"Startup failed: {e}", exc_info=True)
        sys.exit(1)

    app = create_app(config, session_factory, engine,
                     lifespan=make_lifespan(config, session_factory, source))

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
