# wg-fleet System Design

**Date:** 2025-10-19
**Status:** Approved for Implementation

## Overview

This document describes the detailed system design for wg-fleet, a WireGuard-based fleet management system that enables automatic client registration, dynamic IPv6 allocation, hostname management, and fleet monitoring via a web dashboard.

## Architecture

### High-Level Architecture

```
┌─────────────┐
│   Clients   │
│ (WireGuard) │
└──────┬──────┘
       │ HTTP/WireGuard
       ▼
┌─────────────────────────────────────┐
│      FastAPI Application            │
│  ┌─────────────────────────────┐   │
│  │  API Routes                  │   │
│  │  - /fleet/<name>/register    │   │
│  │  - /fleet/<name>/ping        │   │
│  └─────────────────────────────┘   │
│  ┌─────────────────────────────┐   │
│  │  Dashboard Routes            │   │
│  │  - /                         │   │
│  │  - /fleet/<name>             │   │
│  └─────────────────────────────┘   │
│  ┌─────────────────────────────┐   │
│  │  Background Pruning Task     │   │
│  │  (asyncio loop)              │   │
│  └─────────────────────────────┘   │
└──────┬──────────────────┬──────────┘
       │                  │
       ▼                  ▼
┌─────────────┐    ┌──────────────┐
│   SQLite    │    │  WireGuard   │
│  Database   │    │  Interfaces  │
└─────────────┘    └──────────────┘
       │
       ▼
┌─────────────┐
│/run/        │
│fleet_hosts  │
└─────────────┘
```

### Module Organization (Layered Architecture)

```
wg-fleet/
├── main.py              # Application entry point, startup/shutdown
├── config.py            # YAML configuration loading and validation
├── models.py            # SQLAlchemy ORM models
├── database.py          # Database session management
├── command.py           # Subprocess wrapper with logging/error handling
├── wireguard.py         # WireGuard CLI operations wrapper
├── routes.py            # FastAPI route handlers
├── pruning.py           # Background pruning task
├── hosts.py             # /run/fleet_hosts file generation
├── templates/
│   ├── index.html       # Fleet list page
│   └── fleet.html       # Fleet detail page
├── static/              # Optional CSS/JS
└── requirements.txt     # Python dependencies
```

## Component Details

### 1. Configuration Management (`config.py`)

**Purpose:** Load, parse, and validate YAML configuration file.

**Data Structures:**
```python
@dataclass
class FleetConfig:
    ip6: str              # Server's IPv6 address
    subnet: str           # IPv6 subnet for client allocation (CIDR)
    external_ip: str      # Public IP for clients to connect to
    port: int             # UDP port for WireGuard

@dataclass
class Config:
    domain: str           # Base domain for FQDNs
    prune_timeout: str    # Duration string (e.g., "30m")
    fleets: dict[str, FleetConfig]
```

**Key Functions:**
- `load_config(path: str) -> Config` - Load and validate YAML file
- `parse_duration(duration_str: str) -> timedelta` - Parse duration strings like "30m", "1h"

**Validation:**
- Ensures required fields are present
- Validates fleet configurations
- Raises `ValueError` on invalid config
- Raises `FileNotFoundError` if config file missing

**CLI Integration:**
- Uses `argparse` to support `--config` flag
- Default path: `/etc/wg-fleet.yaml`

---

### 2. Database Layer (`models.py`, `database.py`)

**ORM Model (`models.py`):**
```python
class Client(Base):
    __tablename__ = 'clients'

    id = Column(Integer, primary_key=True, autoincrement=True)
    fleet_id = Column(String, nullable=False)
    public_key = Column(String, nullable=False)
    assigned_ip = Column(String, nullable=False)
    http_request_ip = Column(String, nullable=False)
    hostname = Column(String, nullable=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow)
```

**Session Management (`database.py`):**
- SQLite database file: `/var/lib/wg-fleet/clients.db` (or configurable)
- `create_engine()` with SQLite
- `sessionmaker()` for session factory
- `init_db()` function creates tables on startup (`Base.metadata.create_all()`)
- Dependency injection via `get_db()` for FastAPI routes

**Key Operations:**
- `get_client_by_ip(fleet_id: str, ip: str) -> Client` - Lookup for ping endpoint
- `get_clients_by_fleet(fleet_id: str) -> list[Client]` - For dashboard
- `check_hostname_exists(fleet_id: str, hostname: str) -> bool` - Deduplication
- `delete_client(client_id: int)` - For pruning
- Standard CRUD for registration

**Data Lifecycle:**
- Created on registration
- Updated on ping (timestamp, hostname)
- Hard deleted on prune (no soft deletes)

---

### 3. Command Execution (`command.py`)

**Purpose:** Centralized subprocess wrapper with logging, error handling, and sensitive data redaction.

**Key Function:**
```python
def run_command(
    args: list[str],
    sensitive_patterns: Optional[list[str]] = None,
    input_data: Optional[str] = None
) -> str:
    """
    Execute command with error handling and logging.

    Args:
        args: Command and arguments as list
        sensitive_patterns: Strings to redact in logs (e.g., private keys)
        input_data: Optional stdin data

    Returns:
        stdout as string

    Raises:
        CommandError: On non-zero exit code
    """
```

**Features:**
- Logs command invocations with sanitized arguments
- Redacts sensitive data (private keys) from logs
- Captures stdout/stderr
- Raises `CommandError` on failure with stderr details
- Uses `subprocess.run(..., check=True, capture_output=True, text=True)`

**Example Usage:**
```python
# Redact private key from logs
run_command(['wg', 'set', 'wg_southeast', 'private-key', key],
            sensitive_patterns=[key])
```

---

### 4. WireGuard Operations (`wireguard.py`)

**Purpose:** Thin wrapper around `wg` and `wg-quick` CLI tools.

**Key Functions:**

```python
def generate_keypair() -> tuple[str, str]:
    """Generate WireGuard keypair. Returns (private_key, public_key)"""
    # Uses: wg genkey | wg pubkey

def create_interface_config(fleet_name: str, fleet_config: FleetConfig,
                            private_key: str) -> None:
    """Write /etc/wireguard/wg_<fleetname>.conf"""
    # Template: [Interface] with Address, ListenPort, PrivateKey

def interface_exists(fleet_name: str) -> bool:
    """Check if wg_<fleetname> interface exists"""
    # Uses: wg show wg_<fleetname>

def bring_up_interface(fleet_name: str) -> None:
    """Bring up interface with wg-quick up"""

def bring_down_interface(fleet_name: str) -> None:
    """Shutdown interface with wg-quick down"""

def add_peer(fleet_name: str, public_key: str, allowed_ip: str) -> None:
    """Add peer to interface"""
    # Uses: wg set wg_<fleetname> peer <pubkey> allowed-ips <ip>

def remove_peer(fleet_name: str, public_key: str) -> None:
    """Remove peer from interface"""
    # Uses: wg set wg_<fleetname> peer <pubkey> remove

def list_peers(fleet_name: str) -> list[dict]:
    """Get all peers with handshake info"""
    # Parses: wg show wg_<fleetname> dump
    # Returns: [{"public_key": "...", "allowed_ips": "...",
    #           "last_handshake": datetime, ...}]

def get_server_public_key(fleet_name: str) -> str:
    """Extract server's public key from interface"""
    # Uses: wg show wg_<fleetname> public-key
```

**WireGuard Config Template:**
```ini
[Interface]
Address = <fleet.ip6>
ListenPort = <fleet.port>
PrivateKey = <generated_server_private_key>
```

**Client Config Template (returned by /register):**
```ini
[Interface]
PrivateKey = <client_private_key>
Address = <assigned_ipv6>

[Peer]
PublicKey = <server_public_key>
Endpoint = <fleet.external_ip>:<fleet.port>
AllowedIPs = <fleet.ip6>/128
PersistentKeepalive = 25
```

**Key Management:**
- Server keypairs stored in `/etc/wireguard/wg_<fleetname>.conf`
- Generated once on first startup if config doesn't exist
- Persisted across restarts (read from existing config file)

---

### 5. API Routes (`routes.py`)

**FastAPI Routers:**
- `api_router` - Client API endpoints (/fleet/<name>/register, /ping)
- `web_router` - Admin dashboard routes (/, /fleet/<name>)

#### Registration Endpoint

**Route:** `POST /fleet/{fleet_name}/register`

**Request:** None (no body needed)

**Response:**
```json
{
  "status": "success",
  "config": "<wireguard config text>"
}
```

**Logic:**
1. Validate fleet exists in configuration
2. Generate client WireGuard keypair
3. Allocate random IPv6 from fleet subnet
   - Use `ipaddress.IPv6Network` to generate random address
   - No collision checking (IPv6 space is large enough)
4. Add peer to WireGuard interface via `wireguard.add_peer()`
5. Create database record:
   - `fleet_id` = fleet_name
   - `public_key` = client's public key
   - `assigned_ip` = allocated IPv6
   - `http_request_ip` = request.client.host
   - `timestamp` = current time
   - `hostname` = NULL
6. Build and return WireGuard config text

**Error Handling:**
- 404 if fleet doesn't exist
- 500 on any WireGuard or database error

#### Ping Endpoint

**Route:** `POST /fleet/{fleet_name}/ping`

**Request Body:**
```json
{
  "hostname": "optional-hostname-string"
}
```

**Response:**
```json
{
  "status": "ok"
}
```

**Logic:**
1. Extract client IP from `request.client.host`
2. Verify IP is within fleet's subnet using `ipaddress` module
   - Return 403 Forbidden if not in subnet
3. Look up client in database by `(fleet_id, assigned_ip)`
4. Update `timestamp` to current time
5. If hostname provided:
   - Validate format: `[a-z0-9_-]+` regex
   - Check for existing hostname in fleet
   - If duplicate, append incrementing number (excavator → excavator2)
   - Update database with (possibly modified) hostname
   - Call `hosts.regenerate_hosts_file()`

**Hostname Deduplication:**
```python
def get_unique_hostname(session, fleet_id: str, requested: str) -> str:
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
```

#### Dashboard Routes

**Index Route:** `GET /`
- Renders `templates/index.html`
- Displays list of fleets from config
- Links to individual fleet pages

**Fleet Detail Route:** `GET /fleet/{fleet_name}`
- Renders `templates/fleet.html`
- Queries all clients for the fleet from database
- Optionally fetches WireGuard peer stats via `wireguard.list_peers()`
- Merges database and WireGuard data
- Displays table with columns:
  - Hostname (or "(none)")
  - Assigned IP
  - Public Key (truncated)
  - Source IP
  - Last Seen (timestamp)
  - Last Handshake (from WireGuard)

---

### 6. Background Pruning (`pruning.py`)

**Purpose:** Periodically remove inactive clients based on WireGuard handshake time.

**Implementation:**
```python
async def prune_stale_clients(config: Config, db_session_factory):
    """
    Async loop that runs every 5 minutes.

    For each fleet:
      1. Fetch WireGuard peers with handshake times
      2. Calculate cutoff = now - prune_timeout
      3. For each peer with last_handshake < cutoff:
         - Remove from WireGuard interface
         - Delete from database
      4. Log number of pruned clients
      5. Regenerate /run/fleet_hosts if any pruned
    """
    while True:
        await asyncio.sleep(300)  # 5 minutes

        try:
            prune_count = 0

            for fleet_name, fleet_config in config.fleets.items():
                wg_peers = wireguard.list_peers(fleet_name)
                cutoff = datetime.utcnow() - parse_duration(config.prune_timeout)

                with db_session_factory() as session:
                    for peer in wg_peers:
                        if peer['last_handshake'] < cutoff:
                            wireguard.remove_peer(fleet_name, peer['public_key'])

                            client = session.query(Client).filter_by(
                                fleet_id=fleet_name,
                                public_key=peer['public_key']
                            ).first()

                            if client:
                                session.delete(client)
                                prune_count += 1

                    session.commit()

            if prune_count > 0:
                logger.info(f"Pruned {prune_count} stale clients")
                hosts.regenerate_hosts_file(config, db_session_factory)

        except Exception as e:
            logger.error(f"Error in pruning task: {e}", exc_info=True)
```

**Race Condition Handling:**
- Pruning runs independently of API endpoints
- If a client pings while being pruned, database transaction handling determines winner
- Acceptable for client to be pruned and re-register immediately

**Tuning:**
- Run interval: 5 minutes (hardcoded, can be made configurable)
- Prune timeout: From config file (e.g., "30m")

---

### 7. Hosts File Management (`hosts.py`)

**Purpose:** Generate `/run/fleet_hosts` file for DNS/hostname resolution.

**Format:**
```
<ipv6_address> <hostname>.<fleet>.<domain>
```

**Example:**
```
fd00:a0a8:34d:2a00::1234 excavator.southeast.icpcnet.internal
fd00:a0a8:34d:2000::5678 driller.northwest.icpcnet.internal
```

**Implementation:**
```python
def regenerate_hosts_file(config: Config, db_session_factory):
    """
    Full regeneration from database.

    1. Query all clients with non-null hostnames
    2. Build FQDN: <hostname>.<fleet_id>.<config.domain>
    3. Format lines as: <assigned_ip> <fqdn>
    4. Write atomically (temp file + rename)
    """
    lines = []

    with db_session_factory() as session:
        clients = session.query(Client).filter(
            Client.hostname.isnot(None)
        ).all()

        for client in clients:
            fqdn = f"{client.hostname}.{client.fleet_id}.{config.domain}"
            lines.append(f"{client.assigned_ip} {fqdn}")

    # Atomic write
    temp_path = Path(f"{HOSTS_FILE_PATH}.tmp")
    with temp_path.open('w') as f:
        f.write('\n'.join(lines) + '\n')

    temp_path.rename(HOSTS_FILE_PATH)
    logger.info(f"Regenerated hosts file with {len(lines)} entries")
```

**Trigger Points:**
- On startup (initial generation)
- After any ping that changes a hostname
- After pruning cycle (if clients were removed)

**File Location:** `/run/fleet_hosts` (tmpfs, doesn't persist across reboots)

---

### 8. Application Startup (`main.py`)

**Startup Sequence:**

```python
@app.on_event("startup")
async def startup():
    """
    1. Parse CLI arguments (--config flag)
    2. Load and validate configuration
    3. Initialize database (create tables if needed)
    4. For each fleet in config:
       a. Check if /etc/wireguard/wg_<fleet>.conf exists
       b. If not: generate server keypair, create config file
       c. If exists: read existing config (keys already present)
       d. Bring up interface with wg-quick up wg_<fleet>
       e. Reconcile state:
          - Query WireGuard for current peers
          - Query database for registered clients
          - Remove WireGuard peers not in database
          - Remove database clients not in WireGuard
    5. Generate initial /run/fleet_hosts file
    6. Start background pruning task with asyncio.create_task()
    7. Log "Server started successfully"

    On any error: log and sys.exit(1)
    """
```

**Reconciliation Logic:**
```python
def reconcile_fleet_state(fleet_name: str):
    """
    Ensure database and WireGuard are in sync.

    - Get WireGuard peers: wg_peers = wireguard.list_peers(fleet_name)
    - Get DB clients: db_clients = session.query(Client).filter_by(fleet_id=fleet_name)

    For each wg_peer:
      if not in db_clients:
        wireguard.remove_peer(fleet_name, wg_peer.public_key)

    For each db_client:
      if not in wg_peers:
        session.delete(db_client)

    Trust WireGuard as source of truth, clean up DB to match.
    """
```

**Shutdown:**
```python
@app.on_event("shutdown")
async def shutdown():
    """
    Graceful shutdown - allow in-flight requests to complete.
    WireGuard interfaces remain up (system-managed).
    """
    logger.info("Shutting down wg-fleet server")
```

---

### 9. Admin Dashboard Templates

**Technology:**
- Jinja2 templating
- Minimal CSS (monospace font, simple table styling)
- No JavaScript initially
- Optional: htmx for auto-refresh later

**Index Template (`templates/index.html`):**
- Lists all fleets from configuration
- Links to individual fleet detail pages

**Fleet Detail Template (`templates/fleet.html`):**
- Table displaying all active clients
- Columns: Hostname, Assigned IP, Public Key (truncated), Source IP, Last Seen, Handshake
- Back link to index

**Styling:**
- Monospace font for technical feel
- Simple borders and padding
- No external CSS frameworks (KISS)

---

## Data Flow Diagrams

### Client Registration Flow

```
Client                    API                  Database            WireGuard
  |                       |                       |                    |
  |--POST /register------>|                       |                    |
  |                       |                       |                    |
  |                       |--Generate keypair-----|                    |
  |                       |--Allocate IPv6--------|                    |
  |                       |                       |                    |
  |                       |------------------add_peer()--------------->|
  |                       |                       |                    |
  |                       |--Create Client------->|                    |
  |                       |                       |                    |
  |<--WG config text------|                       |                    |
  |                       |                       |                    |
```

### Client Ping Flow

```
Client                    API                  Database            Hosts File
  |                       |                       |                    |
  |--POST /ping---------->|                       |                    |
  | (via WG tunnel)       |                       |                    |
  |                       |--Verify IP in subnet--|                    |
  |                       |                       |                    |
  |                       |--Lookup client------->|                    |
  |                       |--Update timestamp---->|                    |
  |                       |--Update hostname----->|                    |
  |                       |                       |                    |
  |                       |---------------regenerate()---------------->|
  |                       |                       |                    |
  |<--{"status":"ok"}-----|                       |                    |
  |                       |                       |                    |
```

### Pruning Flow

```
Background Task         WireGuard            Database            Hosts File
  |                       |                       |                    |
  |--Every 5 min--------->|                       |                    |
  |                       |                       |                    |
  |--list_peers()-------->|                       |                    |
  |<--peers with times----|                       |                    |
  |                       |                       |                    |
  |--For each stale-------|                       |                    |
  |--remove_peer()------->|                       |                    |
  |--delete_client()------|--------------------->|                    |
  |                       |                       |                    |
  |---------------regenerate()---------------------|------------------>|
  |                       |                       |                    |
```

---

## Key Design Decisions

### 1. IPv6 Random Allocation
- **Decision:** Generate random IPv6 addresses within subnet
- **Rationale:** IPv6 space is enormous (/64 = 2^64 addresses), collision probability negligible
- **Alternative considered:** Sequential allocation (more complexity, no real benefit)

### 2. Stateless Client Identity
- **Decision:** Clients don't have persistent identity, can re-register anytime
- **Rationale:** Clients are clones, no way to uniquely identify, simplifies design
- **Implication:** Each registration gets new IP, old one pruned by timeout

### 3. Database + WireGuard Reconciliation
- **Decision:** On startup, sync DB and WG bidirectionally (remove mismatches from both)
- **Rationale:** After crash/restart, DB and WG may be inconsistent, need single source of truth
- **Alternative considered:** Clear everything on startup (loses hostname data)

### 4. Full Hosts File Regeneration
- **Decision:** Regenerate entire file on every change
- **Rationale:** KISS - simple, idempotent, file is small (<1000 entries)
- **Alternative considered:** Incremental updates (more code, error-prone)

### 5. Separate Asyncio Task for Pruning
- **Decision:** Use `asyncio.create_task()` for background pruning loop
- **Rationale:** Clean separation, runs continuously alongside FastAPI
- **Alternative considered:** FastAPI BackgroundTasks (less suitable for long-running loops)

### 6. Thin Subprocess Wrapper
- **Decision:** Create `command.py` module with logging and redaction
- **Rationale:** Centralized error handling, security (redact keys), consistency
- **Benefit:** All shell commands logged uniformly, sensitive data protected

### 7. SQLAlchemy ORM
- **Decision:** Use ORM instead of raw SQL
- **Rationale:** Cleaner code, type safety, easier to maintain despite slight abstraction overhead
- **Alternative considered:** Raw sqlite3 (more KISS but more boilerplate)

---

## Security Considerations

### No Authentication
- **Decision:** No auth on any endpoint
- **Rationale:** Clients operate in environment where secrets would be immediately compromised
- **Assumption:** Network-level security or trusted clients only

### Sensitive Data Logging
- **Mitigation:** `command.py` redacts private keys from logs
- **Implementation:** Pass `sensitive_patterns` to `run_command()`

### IP Validation on Ping
- **Protection:** Verify ping requests originate from fleet subnet
- **Rationale:** Prevent external hosts from updating client records

### Database Location
- **Recommendation:** `/var/lib/wg-fleet/clients.db` with appropriate file permissions
- **Owner:** Same user as application (root if managing WireGuard)

---

## Testing Strategy

### Manual Testing Checklist
1. **Registration:** Multiple clients register successfully, receive unique IPs
2. **Ping:** Clients ping with/without hostnames
3. **Hostname Deduplication:** Multiple clients with same hostname get numbered suffixes
4. **Pruning:** Inactive clients removed after timeout
5. **Dashboard:** Shows accurate client information with WireGuard stats
6. **Startup Reconciliation:** Server recovers correctly after restart
7. **Fleet Isolation:** Clients in different fleets don't interfere

### Unit Testing Opportunities
- `config.py`: Configuration parsing and validation
- `hosts.py`: Hosts file generation logic
- `command.py`: Command sanitization and error handling
- Hostname deduplication logic
- IPv6 allocation functions

### Integration Testing
- End-to-end client registration → ping → prune workflow
- Database and WireGuard state consistency

---

## Deployment

### Dependencies
```
fastapi
uvicorn
sqlalchemy
pyyaml
jinja2
python-multipart
```

### System Requirements
- Python 3.14
- WireGuard tools (`wireguard-tools` package)
- Root/sudo access for WireGuard interface management
- systemd for service management

### Systemd Service
```ini
[Unit]
Description=wg-fleet WireGuard Fleet Manager
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/wg-fleet/main.py --config /etc/wg-fleet.yaml
Restart=on-failure
User=root

[Install]
WantedBy=multi-user.target
```

### File Locations
- Application: `/opt/wg-fleet/` or similar
- Config: `/etc/wg-fleet.yaml`
- Database: `/var/lib/wg-fleet/clients.db`
- WireGuard configs: `/etc/wireguard/wg_<fleet>.conf`
- Hosts file: `/run/fleet_hosts`

---

## Logging

### Log Levels
- **INFO:** Normal operations (registrations, pings, pruning counts, startup/shutdown)
- **ERROR:** Failures (WireGuard commands, database errors, config errors)

### Log Output
- Stdout (captured by journald when running as systemd service)
- Use Python's standard `logging` module

### Key Events to Log
- Client registration: `"Client registered: fleet={fleet}, ip={ip}, source={source_ip}"`
- Hostname changes: `"Hostname updated: fleet={fleet}, ip={ip}, hostname={hostname}"`
- Pruning: `"Pruned {count} stale clients"`
- Startup: `"Server started successfully"`
- WireGuard commands: `"Running command: wg set wg_southeast peer ..."`
- Errors: Full exception details with stack traces

---

## Future Enhancements (Out of Scope)

- Web-based configuration editor
- Client revocation by admin action
- Bandwidth statistics and graphs
- Email/webhook notifications
- Client groups or tags
- Custom IP allocation strategies
- IPv4 support
- Metrics/monitoring endpoints (Prometheus, etc.)
- TLS/HTTPS (assume reverse proxy)
- Authentication for admin dashboard

---

## Summary

This design provides a clean, maintainable architecture for wg-fleet that adheres to the KISS principle while being robust enough for production use. The layered module structure separates concerns, the thin WireGuard wrapper centralizes operations, and the command execution wrapper provides security and observability. The system handles edge cases (hostname deduplication, state reconciliation, race conditions) gracefully while keeping the codebase simple and understandable.
