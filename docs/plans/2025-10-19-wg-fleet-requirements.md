# wg-fleet - Requirements Document

## Overview
A server for managing a fleet of client machines deployed in wildly varying environments using WireGuard VPN tunnels. The system provides automatic client registration, dynamic IPv6 allocation, hostname management, and a simple admin dashboard.

## Core Components
- FastAPI web server
- SQLite database for client tracking
- WireGuard interface management per fleet
- Admin dashboard (htmx-based)
- Support for multiple independent fleets

---

## Functional Requirements

### 1. Client Registration

**Endpoint:** `POST /fleet/<fleetname>/register`

**Behavior:**
1. Server generates a new WireGuard keypair (private/public)
2. Server selects a random IPv6 address from the fleet's subnet
3. Server adds the peer to the `wg_<fleetname>` interface with:
   - Generated public key
   - Assigned IPv6 address
4. Server saves to database:
   - `public_key` (text)
   - `assigned_ip` (text)
   - `http_request_ip` (text, for informational purposes)
   - `timestamp` (datetime of registration)
   - `fleet_id` (text)
   - `hostname` (text, nullable, initially NULL)
5. Server returns WireGuard configuration to client

**IP Allocation:**
- Random selection from fleet's IPv6 subnet
- No exhaustion handling needed (IPv6 subnets are sufficiently large)
- No deduplication or reservation mechanism

**Client Re-registration:**
- Clients can re-register at any time
- Each registration assigns a new IP (clients have no persistent identity)
- Old registrations will be pruned by timeout mechanism

**Response Format:**
```json
{
  "status": "success",
  "config": "<wireguard config text>"
}
```

**WireGuard Config Structure:**
The returned config should include:
- `[Interface]` section with client's private key and assigned IP
- `[Peer]` section with:
  - Server's public key
  - `Endpoint` = fleet's external_ip:port from config
  - `AllowedIPs` = server's IP only (fleet's ip6 from config)
  - Recommended: `PersistentKeepalive = 25`

**HTTP Status Codes:**
- `200 OK` - Successful registration
- `500 Internal Server Error` - Any server-side error

**Security:**
- No authentication required (clients operate in controlled environment where secrets would be immediately compromised)

---

### 2. Client Ping/Heartbeat

**Endpoint:** `POST /fleet/<fleetname>/ping`

**Request Body (JSON):**
```json
{
  "hostname": "optional-hostname-string"
}
```

**Behavior:**
1. Verify request originates from an IP within the fleet's subnet (reject otherwise)
2. Identify client by source IP address
3. Update database record's timestamp to current time
4. If hostname provided and differs from current:
   - Validate hostname format: `[a-z0-9_-]+`
   - Check for duplicates within the fleet
   - If duplicate exists, append incrementing number (e.g., `excavator`, `excavator2`, `excavator3`)
   - Update database with (possibly modified) hostname
   - Regenerate `/run/fleet_hosts` file

**Response Format:**
```json
{
  "status": "ok"
}
```

**Hostname Management:**
- Hostnames are optional
- Scope: per-fleet uniqueness
- FQDN format: `<hostname>.<fleetname>.<domain>`
- Example: `excavator.southeast.icpcnet.internal`

**Hosts File Format (`/run/fleet_hosts`):**
```
fd00:a0a8:34d:2a00::1234 excavator.southeast.icpcnet.internal
fd00:a0a8:34d:2000::5678 driller.northwest.icpcnet.internal
```

**HTTP Status Codes:**
- `200 OK` - Successful ping
- `403 Forbidden` - Request not from fleet subnet
- `500 Internal Server Error` - Any server-side error

---

### 3. Client Pruning

**Mechanism:**
- Background periodic task (recommended: run every 5-10 minutes)
- Query WireGuard interfaces for peer handshake times
- For each peer, if last handshake > `config.prune_timeout`:
  - Remove peer from WireGuard interface using `wg` CLI
  - Delete record from database (hard delete)
  - Regenerate `/run/fleet_hosts` file

**Race Conditions:**
- Pruning runs independently of ping/register endpoints
- If a client pings while being pruned, whichever operation completes first wins
- Acceptable behavior: client will re-register if pruned

**Notes:**
- Aggressive pruning is acceptable
- Clients will detect connection loss and re-register automatically

---

### 4. Admin Dashboard

**Routes:**
- `/` - Index page listing all fleets with links
- `/fleet/<fleetname>` - Fleet-specific page showing active clients

**Fleet Page Display:**
Per client, show:
- Public key
- Assigned IP
- Source IP (from registration)
- Registration timestamp
- Hostname (if set)
- WireGuard handshake information (if performance allows):
  - Last handshake time
  - Bytes sent/received

**Data Source:**
- Primary: SQLite database
- Optional enhancement: Real-time WireGuard peer stats via `wg show` CLI

**Filtering/Sorting:**
- Not required initially
- Only show active clients (pruned clients are deleted from DB)

**Authentication:**
- None required

**Technology:**
- FastAPI + Jinja2 templates
- htmx for dynamic updates (optional)
- Simple, minimal CSS

---

## Configuration

**File Location:**
- Default: `/etc/wg-fleet.yaml`
- Override: `--config` CLI flag

**Format (YAML):**
```yaml
domain: icpcnet.internal
prune_timeout: 30m

fleets:
  southeast:
    ip6: fd00:a0a8:34d:2a00::1
    subnet: fd00:a0a8:34d:2a00::/64
    external_ip: 1.2.3.4
    port: 51820

  northwest:
    ip6: fd00:a0a8:34d:2000::1
    subnet: fd00:a0a8:34d:2000::/64
    external_ip: 1.2.3.4
    port: 51821
```

**Fields:**
- `domain` (string) - Base domain for FQDN construction
- `prune_timeout` (duration string) - Age threshold for pruning clients (e.g., "30m", "1h")
- `fleets` (map) - Fleet name → fleet configuration
  - `ip6` (string) - Server's IPv6 address for this fleet
  - `subnet` (string) - IPv6 subnet in CIDR notation for client allocation
  - `external_ip` (string) - Public IP address clients should connect to
  - `port` (integer) - UDP port for WireGuard

**Validation:**
- Invalid configuration → exit with non-zero status code and error message
- No runtime configuration reload (restart required for changes)

---

## System Behavior

### Startup Sequence
1. Load and validate configuration file
2. Initialize SQLite database (create tables if needed)
3. For each fleet in configuration:
   - Generate server WireGuard keypair (or load existing)
   - Create WireGuard interface config at `/etc/wireguard/wg_<fleetname>.conf`
   - Bring up interface using `wg-quick up wg_<fleetname>`
   - Query WireGuard for current peers
   - Query database for registered clients
   - **Reconciliation:** Remove database entries for clients not present in WireGuard
4. If any interface fails to come up → fail with non-zero exit code
5. Generate initial `/run/fleet_hosts` file
6. Start FastAPI web server
7. Start background pruning task

**WireGuard Interface Config Template (`/etc/wireguard/wg_<fleetname>.conf`):**
```ini
[Interface]
Address = <fleet.ip6>
ListenPort = <fleet.port>
PrivateKey = <generated_server_private_key>
```

### Shutdown Behavior
- Graceful: Allow in-flight requests to complete
- WireGuard interfaces remain up (system-managed)

---

## Database Schema

**Table: `clients`**

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | Internal record ID |
| `fleet_id` | TEXT | NOT NULL | Fleet name (e.g., "southeast") |
| `public_key` | TEXT | NOT NULL | Client's WireGuard public key |
| `assigned_ip` | TEXT | NOT NULL | Allocated IPv6 address |
| `http_request_ip` | TEXT | NOT NULL | Source IP of registration request |
| `hostname` | TEXT | NULL | Client-provided hostname (nullable) |
| `timestamp` | DATETIME | NOT NULL | Last registration or ping time |

**Indexes:**
- None needed (expected maximum ~1000 clients per fleet)

**Data Lifecycle:**
- Created on registration
- Updated on ping (timestamp, hostname)
- Hard deleted on prune

---

## Implementation Guidelines

### Technology Stack
- **Language:** Python 3.14
- **Web Framework:** FastAPI
- **Database:** SQLite (via sqlite3 or SQLAlchemy)
- **Templating:** Jinja2
- **WireGuard Management:** Shell out to `wg` and `wg-quick` CLI tools

### Dependencies/Assumptions
- WireGuard tools (`wireguard-tools` package) pre-installed on host
- Root/sudo access for WireGuard interface management
- System will run as systemd service (logs to stdout → journald)

### Logging
- Use Python's standard `logging` module
- Log level: INFO for normal operations, ERROR for failures
- Output: stdout (journald will capture)
- Log key events:
  - Client registrations
  - Client pings with hostname changes
  - Pruning operations (number of clients pruned)
  - Startup/shutdown
  - Configuration errors
  - WireGuard command failures

### Error Handling
- Fail fast on critical errors (invalid config, WireGuard interface failure)
- Log and return 500 for runtime errors in API endpoints
- Don't expose internal error details to clients (log server-side only)

### Code Style
- **KISS Principle:** Keep it simple
- Don't over-engineer for unlikely edge cases
- Prefer readability over premature optimization
- Direct subprocess calls to `wg` CLI are acceptable (no need for libraries)

---

## Out of Scope (Explicitly Not Required)

- Authentication/authorization for any endpoint
- TLS/HTTPS (assume reverse proxy if needed)
- Client deduplication or persistent identity
- IP exhaustion handling
- Soft deletes or audit trails
- Complex hostname validation beyond basic character set
- Rate limiting or DDoS protection
- Database migrations (schema is fixed)
- High availability or clustering
- Backup/restore functionality
- Metrics/monitoring endpoints (beyond basic logging)
- Runtime configuration reload

---

## Testing Considerations

While comprehensive tests are not required, the following manual testing should verify correct behavior:

1. **Registration:** Multiple clients can register and receive unique IPs
2. **Ping:** Clients can ping with/without hostnames
3. **Hostname Deduplication:** Multiple clients with same hostname get numbered suffixes
4. **Pruning:** Inactive clients are removed after timeout
5. **Dashboard:** Shows accurate client information
6. **Startup:** Server recovers correctly after restart
7. **Fleet Isolation:** Clients in different fleets don't interfere with each other

---

## Future Enhancements (Not in Scope)

- Web-based configuration management
- Client revocation by admin
- Bandwidth statistics and graphs
- Email/webhook notifications for events
- Client groups or tags
- Custom IP allocation strategies
- IPv4 support
