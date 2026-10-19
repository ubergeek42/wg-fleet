from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, PlainTextResponse
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
from enrollment import host_key_signature_valid, normalize_host_key, token_matches
from fleet_registry import as_utc
import host_ca
import wireguard
from hook_manager import trigger_hooks, EventType, HookContext
import hooks  # Import to register hooks

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

class RegisterRequest(BaseModel):
    # Required for fleets from fleets_url; static fleets ignore them
    site: Optional[str] = None
    token: Optional[str] = None
    host_key: Optional[str] = None
    # host_key's `ssh-keygen -Y sign -n wg-fleet-register` signature over
    # enrollment.registration_message(); proves the client owns host_key
    host_key_signature: Optional[str] = None

class RegisterResponse(BaseModel):
    status: str
    config: str
    port: int
    server_ip: str

class PingRequest(BaseModel):
    hostname: Optional[str] = None

class PingResponse(BaseModel):
    status: str
    # Host certificate for the client's host key and current name (fleets from fleets_url)
    host_certificate: Optional[str] = None

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
    body: Optional[RegisterRequest] = None,
    db: Session = Depends(get_db_session)
):
    """
    Register a new client to the fleet.

    1. Validate fleet exists and accepts registrations
    2. For fleets from fleets_url, check the site's enrollment token and the host key
    3. Generate keypair
    4. Allocate random IPv6
    5. Build client config
    6. Add peer to WireGuard
    7. Save to database (removing the peer again if this fails)
    8. Return WireGuard config
    """
    # Validate fleet exists
    if fleet_name not in app_config.fleets:
        raise HTTPException(status_code=404, detail=f"Fleet '{fleet_name}' not found")

    fleet_config = app_config.fleets[fleet_name]
    if fleet_config.removed:
        raise HTTPException(status_code=403, detail=f"Fleet '{fleet_name}' no longer accepts registrations")

    site = host_key = None
    host_key_proven = False
    if fleet_config.static:
        # Unauthenticated, so never used for a certificate or to take over a record here.
        # Kept so that when the machine later joins a fleet from fleets_url with its
        # token, this record can be removed (see `moved` below).
        if body is not None and body.host_key:
            host_key = normalize_host_key(body.host_key)
    else:
        body = body or RegisterRequest()
        if not body.site or not body.token or not token_matches(
                body.token, fleet_config.sites.get(body.site)):
            logger.warning(f"Registration refused: fleet={fleet_name}, site={body.site!r}, "
                           f"source={request.client.host}: unknown site or bad enrollment token")
            raise HTTPException(status_code=403, detail="Unknown site or bad enrollment token")
        host_key = normalize_host_key(body.host_key or '')
        if host_key is None:
            raise HTTPException(status_code=400, detail="host_key must be an ssh public key")
        site = body.site
        if body.host_key_signature:
            if not host_key_signature_valid(fleet_name, site, host_key, body.host_key_signature):
                logger.warning(f"Registration refused: fleet={fleet_name}, site={site}, "
                               f"source={request.client.host}: host_key_signature doesn't match host_key")
                raise HTTPException(status_code=403, detail="host_key_signature doesn't match host_key")
            host_key_proven = True
        else:
            logger.info(f"Registration without host_key_signature: fleet={fleet_name}, site={site}; "
                        f"it can't take over a previous record or leave another fleet")

    # A machine that registers again (new WireGuard key after a reboot or VPN failure)
    # takes over its previous record's hostname, instead of getting "name--2" until the
    # old record is pruned. Only the same host key at the same site can do that, and
    # only once the client proved it owns the key.
    previous = []
    # A machine that moved here from another fleet (e.g. from the setup fleet after
    # setup, or to another contest) is removed from that fleet right away, rather than
    # lingering in the hosts file, inventory and Prometheus until it's pruned. Only a
    # registration with a token and a proven host key can do that, never an
    # unauthenticated static one.
    moved = []
    if host_key_proven:
        previous = db.query(Client).filter_by(
            fleet_id=fleet_name, site=site, host_key=host_key
        ).order_by(Client.timestamp.desc()).all()
        moved = db.query(Client).filter(
            Client.fleet_id != fleet_name, Client.host_key == host_key
        ).all()
    reclaimed = previous[0] if previous else None

    peer_added = False

    try:
        # Generate client keypair
        client_private, client_public = wireguard.generate_keypair()

        # Allocate random IPv6
        client_ip = allocate_random_ip(fleet_config.subnet)

        # Build client config
        server_pubkey = wireguard.get_server_public_key(fleet_config.interface)
        config_text = wireguard.build_client_config(
            client_private_key=client_private,
            client_ip=client_ip,
            server_public_key=server_pubkey,
            endpoint_ip=fleet_config.external_ip,
            endpoint_port=fleet_config.port,
            server_ip=fleet_config.ip6,
            subnet=fleet_config.subnet
        )

        # Add peer to WireGuard
        wireguard.add_peer(fleet_config.interface, client_public, client_ip)
        peer_added = True

        # Save to database
        client_record = Client(
            fleet_id=fleet_name,
            public_key=client_public,
            assigned_ip=client_ip,
            http_request_ip=request.client.host,
            hostname=None,
            timestamp=datetime.now(UTC),
            site=site,
            host_key=host_key
        )
        if reclaimed is not None:
            client_record.hostname = reclaimed.hostname
            client_record.host_certificate = reclaimed.host_certificate
            client_record.host_certificate_name = reclaimed.host_certificate_name
            client_record.host_certificate_valid_until = reclaimed.host_certificate_valid_until
            client_record.host_certificate_ca = reclaimed.host_certificate_ca
        old_peers = [(old.fleet_id, old.public_key) for old in previous + moved]
        moved_from = [(old.fleet_id, old.hostname or old.assigned_ip) for old in moved]
        for old in previous + moved:
            db.delete(old)
        db.add(client_record)
        db.commit()

        logger.info(f"Client registered: fleet={fleet_name}, site={site}, ip={client_ip}, "
                    f"source={request.client.host}")

        if old_peers:
            for old_fleet, old_peer in old_peers:
                old_fleet_config = app_config.fleets.get(old_fleet)
                if old_fleet_config is None:
                    continue
                try:
                    wireguard.remove_peer(old_fleet_config.interface, old_peer)
                except Exception as e:
                    # Reconciliation removes it on the next start
                    logger.error(f"Failed to remove the previous peer of a re-registered client: {e}")
            if previous:
                logger.info(f"Client re-registered: fleet={fleet_name}, ip={client_ip}, "
                            f"hostname={client_record.hostname} (replaces {len(previous)} record(s))")
            for old_fleet, old_name in moved_from:
                logger.info(f"Client moved: from fleet={old_fleet} ({old_name}) "
                            f"to fleet={fleet_name}, ip={client_ip}")
            # Names now point at the new address, or are gone
            trigger_hooks(HookContext(
                event_type=EventType.CLIENT_ADDED,
                config=app_config,
                session_factory=_session_factory,
                client_data={
                    'ip': client_ip,
                    'hostname': client_record.hostname,
                    'fleet_id': fleet_name
                }
            ))

        return RegisterResponse(
            status="success",
            config=config_text,
            port=fleet_config.port,
            server_ip=fleet_config.ip6
        )

    except Exception as e:
        logger.error(f"Registration failed: {e}", exc_info=True)
        db.rollback()
        if peer_added:
            # Don't leave a WireGuard peer behind for a client that never got its config
            try:
                wireguard.remove_peer(fleet_config.interface, client_public)
            except Exception as cleanup_error:
                logger.error(f"Failed to remove peer after registration failure: {cleanup_error}")
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

    while hostname_in_use(session, fleet_id, current):
        current = f"{base_hostname}--{counter}"
        counter += 1

    return current

def hostname_in_use(session: Session, fleet_id: str, hostname: str) -> bool:
    """Check whether any client in the fleet has this hostname"""
    return session.query(Client).filter_by(
        fleet_id=fleet_id,
        hostname=hostname
    ).first() is not None

def hostname_matches_request(current: Optional[str], requested: str) -> bool:
    """
    Check whether a client's current hostname satisfies its requested hostname.

    A client that asked for "foo" and was deduplicated to "foo--2" keeps that
    name on later pings instead of being renamed again.
    """
    if current is None:
        return False
    return re.fullmatch(rf'{re.escape(requested)}(--\d+)?', current) is not None

@api_router.post("/fleet/{fleet_name}/ping", response_model=PingResponse, response_model_exclude_none=True)
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

    # Get client IP (uvicorn's ProxyHeadersMiddleware populates this from X-Forwarded-For
    # when proxy_headers=True and the request comes from a trusted proxy)
    client_ip = request.client.host

    # Verify IP is in fleet subnet
    try:
        client_addr = ipaddress.IPv6Address(client_ip)
        fleet_network = ipaddress.IPv6Network(fleet_config.subnet)

        if client_addr not in fleet_network:
            raise HTTPException(status_code=403, detail=f"IP not in fleet subnet: {client_ip}")
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
        # fullmatch, not match with $, which would accept a trailing newline.
        # No leading/trailing '-' (a leading '-' looks like an option to ssh/ansible), and at
        # most 59 chars so a dedup suffix like "--99" stays within the 63-char DNS label limit.
        if not re.fullmatch(r'[a-z0-9_]([a-z0-9_-]{0,57}[a-z0-9_])?', ping_req.hostname):
            raise HTTPException(status_code=400, detail="Invalid hostname format")

        unique_hostname = None
        if not hostname_matches_request(client_record.hostname, ping_req.hostname):
            # Get unique hostname (handles deduplication)
            unique_hostname = get_unique_hostname(db, fleet_name, ping_req.hostname)
        elif (client_record.hostname != ping_req.hostname
              and not hostname_in_use(db, fleet_name, ping_req.hostname)):
            # Deduplicated "name--N" keeps its name across pings, but takes the plain
            # name once it's free (e.g. the stale record of a re-registered machine was pruned)
            unique_hostname = ping_req.hostname

        if unique_hostname:
            client_record.hostname = unique_hostname
            hostname_changed = True
            logger.info(f"Hostname updated: fleet={fleet_name}, ip={client_ip}, hostname={unique_hostname}")

    host_certificate = host_certificate_for(fleet_name, fleet_config, client_record)
    db.commit()

    # Regenerate hosts file if hostname changed
    if hostname_changed:
        trigger_hooks(HookContext(
            event_type=EventType.CLIENT_HOSTNAME_CHANGED,
            config=app_config,
            session_factory=_session_factory,
            client_data={
                'ip': client_ip,
                'hostname': unique_hostname,
                'fleet_id': fleet_name
            }
        ))

    return PingResponse(status="ok", host_certificate=host_certificate)

def host_certificate_for(fleet_name: str, fleet_config, client_record: Client) -> Optional[str]:
    """
    The client's host certificate, signing a new one if its name, its validity or the
    host CA changed.

    Every ping returns it, so a machine that rebooted (and kept its record and name)
    still gets one, and a renewal needs nothing special. Only fleets from fleets_url
    get certificates: anyone can register in a static fleet.

    Returns:
        The certificate, or None if the client doesn't get one
    """
    if fleet_config.static or fleet_config.removed or fleet_config.valid_until is None:
        return None
    if not client_record.host_key or not client_record.hostname:
        return None
    if fleet_config.valid_until <= datetime.now(UTC):
        return None
    try:
        ca_key = ' '.join(host_ca.public_key().split()[:2])
    except FileNotFoundError:
        logger.error("No host CA; can't sign host certificates")
        return None

    if (client_record.host_certificate
            and client_record.host_certificate_name == client_record.hostname
            and as_utc(client_record.host_certificate_valid_until) == fleet_config.valid_until
            and client_record.host_certificate_ca == ca_key):
        return client_record.host_certificate

    principal = f"{client_record.hostname}.{fleet_name}.{app_config.domain}"
    try:
        certificate = host_ca.sign_host_key(
            client_record.host_key,
            principal=principal,
            key_id=f"{fleet_name}/{client_record.hostname}/{client_record.id}",
            valid_until=fleet_config.valid_until,
        )
    except Exception as e:
        logger.error(f"Failed to sign host key for {principal}: {e}")
        return None

    client_record.host_certificate = certificate
    client_record.host_certificate_name = client_record.hostname
    client_record.host_certificate_valid_until = fleet_config.valid_until.astimezone(UTC).replace(tzinfo=None)
    client_record.host_certificate_ca = ca_key
    logger.info(f"Signed host certificate for {principal}, valid until {fleet_config.valid_until}")
    return certificate

@api_router.get("/ssh/host_ca.pub", response_class=PlainTextResponse)
async def get_host_ca():
    """The host CA's public key, for known_hosts and contestforge's ssh_host_ca_keys"""
    try:
        return host_ca.public_key() + '\n'
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="No host CA")

# Jinja2 templates setup
templates = Jinja2Templates(directory="templates")

# Add custom filter for relative time
def humanize_timedelta(dt):
    """Convert datetime to relative time string (e.g., '2m 30s ago')"""
    if dt is None:
        return None

    # Handle both naive and aware datetimes
    now = datetime.now(UTC)

    # If dt is naive, assume it's UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)

    delta = now - dt

    if delta.total_seconds() < 0:
        return "in the future"

    seconds = int(delta.total_seconds())

    if seconds < 60:
        return f"{seconds}s ago"
    elif seconds < 3600:
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes}m {secs}s ago"
    elif seconds < 86400:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m ago"
    else:
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        return f"{days}d {hours}h ago"

templates.env.filters['humanize'] = humanize_timedelta

@web_router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Fleet list page"""
    try:
        host_ca_key = host_ca.public_key()
    except FileNotFoundError:
        host_ca_key = None
    return templates.TemplateResponse(request, "index.html", {
        "fleets": app_config.fleets.keys(),
        "host_ca_key": host_ca_key,
        "domain": app_config.domain
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

    fleet_config = app_config.fleets[fleet_name]

    # Get clients from database
    clients = db.query(Client).filter_by(fleet_id=fleet_name).all()

    # Optionally merge WireGuard stats
    try:
        wg_peers = wireguard.list_peers(fleet_config.interface)
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

    return templates.TemplateResponse(request, "fleet.html", {
        "fleet_name": fleet_name,
        "fleet_config": fleet_config,
        "clients": clients
    })

def create_app(config, session_factory, engine, lifespan=None):
    """
    Create FastAPI application.

    Args:
        config: Application configuration
        session_factory: SQLAlchemy session factory
        engine: SQLAlchemy engine
        lifespan: Optional lifespan context manager for startup/shutdown

    Returns:
        FastAPI app instance
    """
    from fastapi import FastAPI

    global app_config, _session_factory
    app_config = config
    _session_factory = session_factory

    app = FastAPI(title="wg-fleet", lifespan=lifespan)

    app.include_router(api_router)
    app.include_router(web_router)

    return app
