from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
import ipaddress
import random
import re
from datetime import datetime, UTC
import logging

from models import Client
import wireguard
import hosts

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

class PingRequest(BaseModel):
    hostname: Optional[str] = None

class PingResponse(BaseModel):
    status: str

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
            timestamp=datetime.now(UTC)
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

def get_unique_hostname(session: Session, fleet_id: str, requested: str) -> str:
    """
    Get unique hostname for fleet, adding number suffix if needed.

    Args:
        session: Database session
        fleet_id: Fleet name
        requested: Requested hostname

    Returns:
        Unique hostname (possibly with number suffix)
    """
    base_hostname = requested
    counter = 2
    current = base_hostname

    while session.query(Client).filter_by(
        fleet_id=fleet_id,
        hostname=current
    ).first() is not None:
        current = f"{base_hostname}{counter}"
        counter += 1

    return current

@api_router.post("/fleet/{fleet_name}/ping", response_model=PingResponse)
async def ping_client(
    fleet_name: str,
    ping_req: PingRequest,
    request: Request,
    db: Session = Depends(get_db_session)
):
    """
    Client heartbeat/ping endpoint.

    1. Verify IP is in fleet subnet
    2. Look up client by IP
    3. Update timestamp
    4. Handle hostname if provided
    """
    # Validate fleet exists
    if fleet_name not in app_config.fleets:
        raise HTTPException(status_code=404, detail=f"Fleet '{fleet_name}' not found")

    fleet_config = app_config.fleets[fleet_name]

    # Get client IP (support X-Forwarded-For for testing and reverse proxies)
    client_ip = request.headers.get('X-Forwarded-For')
    if not client_ip:
        client_ip = request.client.host

    # Verify IP is in fleet subnet
    try:
        client_addr = ipaddress.IPv6Address(client_ip)
        fleet_network = ipaddress.IPv6Network(fleet_config.subnet)

        if client_addr not in fleet_network:
            raise HTTPException(status_code=403, detail="IP not in fleet subnet")
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid IP address")

    # Look up client
    client_record = db.query(Client).filter_by(
        fleet_id=fleet_name,
        assigned_ip=client_ip
    ).first()

    if not client_record:
        raise HTTPException(status_code=404, detail="Client not registered")

    # Update timestamp
    client_record.timestamp = datetime.now(UTC)

    # Handle hostname if provided
    hostname_changed = False
    if ping_req.hostname:
        # Validate hostname format
        if not re.match(r'^[a-z0-9_-]+$', ping_req.hostname):
            raise HTTPException(status_code=400, detail="Invalid hostname format")

        # Check if different from current
        if client_record.hostname != ping_req.hostname:
            # Get unique hostname (handles deduplication)
            unique_hostname = get_unique_hostname(db, fleet_name, ping_req.hostname)
            client_record.hostname = unique_hostname
            hostname_changed = True
            logger.info(f"Hostname updated: fleet={fleet_name}, ip={client_ip}, hostname={unique_hostname}")

    db.commit()

    # Regenerate hosts file if hostname changed
    if hostname_changed:
        hosts.regenerate_hosts_file(app_config, _session_factory)

    return PingResponse(status="ok")

# Jinja2 templates setup
templates = Jinja2Templates(directory="templates")

@web_router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Fleet list page"""
    return templates.TemplateResponse("index.html", {
        "request": request,
        "fleets": app_config.fleets.keys()
    })

@web_router.get("/fleet/{fleet_name}", response_class=HTMLResponse)
async def fleet_detail(
    fleet_name: str,
    request: Request,
    db: Session = Depends(get_db_session)
):
    """Fleet detail page with active clients"""
    if fleet_name not in app_config.fleets:
        raise HTTPException(status_code=404, detail="Fleet not found")

    # Get clients from database
    clients = db.query(Client).filter_by(fleet_id=fleet_name).all()

    # Optionally merge WireGuard stats
    try:
        wg_peers = wireguard.list_peers(fleet_name)
        wg_map = {peer['public_key']: peer for peer in wg_peers}

        for client in clients:
            if client.public_key in wg_map:
                peer = wg_map[client.public_key]
                client.wg_last_handshake = peer['last_handshake']
                client.wg_rx_bytes = peer.get('rx_bytes', 0)
                client.wg_tx_bytes = peer.get('tx_bytes', 0)
            else:
                client.wg_last_handshake = None
    except Exception as e:
        logger.warning(f"Failed to fetch WireGuard stats: {e}")

    return templates.TemplateResponse("fleet.html", {
        "request": request,
        "fleet_name": fleet_name,
        "clients": clients
    })

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
