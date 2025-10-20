from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
import ipaddress
import random
from datetime import datetime
import logging

from models import Client
import wireguard

logger = logging.getLogger(__name__)

api_router = APIRouter()
web_router = APIRouter()

# Global config and session factory (set by create_app)
app_config = None
_session_factory = None

# Dependency for database session
def get_db_session():
    with _session_factory() as session:
        yield session

class RegisterResponse(BaseModel):
    status: str
    config: str

def allocate_random_ip(subnet_str: str) -> str:
    """
    Allocate a random IPv6 address from the subnet.

    Args:
        subnet_str: IPv6 subnet in CIDR notation (e.g., "fd00::/64")

    Returns:
        Random IPv6 address as string
    """
    network = ipaddress.IPv6Network(subnet_str)
    # Generate random host portion
    random_int = random.randint(1, 2**(128 - network.prefixlen) - 1)
    random_ip = network.network_address + random_int
    return str(random_ip)

@api_router.post("/fleet/{fleet_name}/register", response_model=RegisterResponse)
async def register_client(
    fleet_name: str,
    request: Request,
    db: Session = Depends(get_db_session)
):
    """
    Register a new client to the fleet.

    1. Validate fleet exists
    2. Generate keypair
    3. Allocate random IPv6
    4. Add peer to WireGuard
    5. Save to database
    6. Return WireGuard config
    """
    # Validate fleet exists
    if fleet_name not in app_config.fleets:
        raise HTTPException(status_code=404, detail=f"Fleet '{fleet_name}' not found")

    fleet_config = app_config.fleets[fleet_name]

    try:
        # Generate client keypair
        client_private, client_public = wireguard.generate_keypair()

        # Allocate random IPv6
        client_ip = allocate_random_ip(fleet_config.subnet)

        # Add peer to WireGuard
        wireguard.add_peer(fleet_name, client_public, client_ip)

        # Save to database
        client_record = Client(
            fleet_id=fleet_name,
            public_key=client_public,
            assigned_ip=client_ip,
            http_request_ip=request.client.host,
            hostname=None,
            timestamp=datetime.utcnow()
        )
        db.add(client_record)
        db.commit()

        logger.info(f"Client registered: fleet={fleet_name}, ip={client_ip}, source={request.client.host}")

        # Build client config
        server_pubkey = wireguard.get_server_public_key(fleet_name)
        config_text = wireguard.build_client_config(
            client_private_key=client_private,
            client_ip=client_ip,
            server_public_key=server_pubkey,
            endpoint_ip=fleet_config.external_ip,
            endpoint_port=fleet_config.port,
            server_ip=fleet_config.ip6
        )

        return RegisterResponse(status="success", config=config_text)

    except Exception as e:
        logger.error(f"Registration failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

def create_app(config, session_factory, engine):
    """
    Create FastAPI application.

    Args:
        config: Application configuration
        session_factory: SQLAlchemy session factory
        engine: SQLAlchemy engine

    Returns:
        FastAPI app instance
    """
    from fastapi import FastAPI

    global app_config, _session_factory
    app_config = config
    _session_factory = session_factory

    app = FastAPI(title="wg-fleet")

    app.include_router(api_router)
    app.include_router(web_router)

    return app
