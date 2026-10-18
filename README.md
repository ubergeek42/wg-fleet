# wg-fleet - WireGuard Fleet Management System

A WireGuard-based fleet management system that enables automatic client registration, dynamic IPv6 allocation, hostname management, and fleet monitoring via a web dashboard.

## Overview

wg-fleet simplifies managing large numbers of WireGuard clients that may be clones or ephemeral instances. Instead of manually configuring each client, they can automatically register with the server, receive configuration, and maintain connectivity through periodic pings. The system handles hostname deduplication, automatic pruning of inactive clients, and provides a web dashboard for monitoring.

**Key Features:**
- Automatic client registration with dynamic IPv6 allocation
- Multiple fleet support (separate WireGuard interfaces)
- Hostname management with automatic deduplication
- Background pruning of inactive clients
- Web dashboard for monitoring active clients
- Automatic hosts file generation for DNS integration

## Architecture

```
Clients (WireGuard) -> FastAPI Application -> SQLite Database + WireGuard Interfaces
                                           -> /run/wg_fleet_hosts
```

The application uses a layered architecture:
- **FastAPI** for HTTP API and web dashboard
- **SQLAlchemy** ORM for database management
- **WireGuard CLI** tools for VPN operations
- **Background asyncio task** for pruning
- **Jinja2** templates for admin UI

## Installation

### Requirements

- Python 3.14 or later
- WireGuard tools (`wireguard-tools` package)
- Root or sudo access (for WireGuard interface management)

### Install Dependencies

```bash
pip install -r requirements.txt
```

### Install WireGuard Tools

On Debian/Ubuntu:
```bash
sudo apt-get install wireguard-tools
```

On Arch Linux:
```bash
sudo pacman -S wireguard-tools
```

## Configuration

### Configuration File

Create a configuration file at `/etc/wg-fleet.yaml`:

```yaml
---
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

**Configuration Fields:**

- `domain`: Base domain for generating FQDNs (hostname.fleet.domain)
- `prune_timeout`: Duration string for client inactivity timeout (e.g., "30m", "1h", "2h30m")
- `fleets`: Dictionary of static fleet configurations. Fleet names must be 1-12 characters of
  lowercase letters, digits, or `-` (the name becomes the interface `wg_<name>`, which
  Linux limits to 15 characters). Static fleets accept registration from anyone.
  - `ip6`: Server's IPv6 address for this fleet
  - `subnet`: IPv6 subnet for client allocation (CIDR notation)
  - `external_ip`: Public IP address (IPv4 or IPv6) clients connect to. Optional when the
    server-level `external_ip` is set.
  - `port`: UDP port for this WireGuard interface

### Fleets from contestforge

Fleets can also come from a signed `fleets.json` that contestforge publishes to a static
bucket. wg-fleet fetches it every `refresh_interval`, verifies its signature, and serves the
entries whose `server` is its `server_name`. Static fleets can be configured alongside.

```yaml
fleets_url: https://configs.cloudcontest.org/v2/fleets.json
server_name: mgmt.env.cloudcontest.org
external_ip: 203.0.113.5
config_signers: |
  contestforge ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA...
```

- `fleets_url`: URL of `fleets.json`; its signature is at `<fleets_url>.sig`
- `server_name`: Which `fleets.json` entries are this server's
- `external_ip`: WireGuard endpoint for these fleets (fleets.json doesn't carry one)
- `config_signers`: `allowed_signers` lines (a list, or one block) for the keys that may sign
  `fleets.json`, as printed by contestforge's `flask signing-key init`
- `config_signer_id` (default `contestforge`): signer identity the signature must match
- `refresh_interval` (default `1m`)
- `removed_fleet_grace` (default `24h`): how long a removed fleet's interface is kept after
  its last client is gone

How they behave:

- The file is verified with `ssh-keygen -Y verify -n contestforge` against
  `config_signers`. A file that doesn't verify, or whose `revision` is lower than the last
  accepted one, is refused and logged, and the previous one stays in use.
- The last accepted file is kept in `/var/lib/wg-fleet/fleets.json` (with `.sig`), so
  wg-fleet starts with the same fleets when the URL is unreachable. If contestforge's database
  was reset and its revisions went backwards, delete those two files and restart.
- Each fleet gets the interface `wgf` + the first 8 hex digits of sha256(fleet name), since
  these names can be up to 32 characters.
- A fleet's subnet and port never change once it has an interface; a later file that moves
  them is logged and ignored. Fleets whose name, port or subnet collide with another fleet
  are ignored.
- A fleet that leaves the file or passes its `valid_until` stops accepting registrations at
  once. Its interface stays up until its clients are pruned and `removed_fleet_grace` has
  passed, and is then removed along with its config file.

### Duration Format

The `prune_timeout` field accepts duration strings:
- `30m` - 30 minutes
- `1h` - 1 hour
- `2h30m` - 2 hours 30 minutes

Only hours and minutes are supported, in that order. An invalid value is rejected when the config is loaded.

### Database Setup

The database is automatically initialized on first startup. By default, it's stored at `/var/lib/wg-fleet/clients.db`. Columns added by newer versions are added to an existing database on startup.

Ensure the directory exists and has proper permissions:

```bash
sudo mkdir -p /var/lib/wg-fleet
sudo chown root:root /var/lib/wg-fleet
sudo chmod 755 /var/lib/wg-fleet
```

### WireGuard Configuration

On first startup, wg-fleet will:
1. Generate server keypairs for each fleet (if config doesn't exist)
2. Create `/etc/wireguard/wg_<fleetname>.conf` files (`wgf<hash>.conf` for fleets from
   `fleets_url`)
3. Bring up the WireGuard interfaces

These configs persist across restarts. If you need to regenerate, remove the config files and restart wg-fleet.

## Running the Server

### Development Mode

```bash
python3 main.py --config /etc/wg-fleet.yaml
```

Or use a custom config location:

```bash
python3 main.py --config ./example-config.yaml
```

The server will start on `http://0.0.0.0:8000` by default.

### Production Deployment with systemd

Create a systemd service file at `/etc/systemd/system/wg-fleet.service`:

```ini
[Unit]
Description=wg-fleet WireGuard Fleet Manager
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/wg-fleet/main.py --config /etc/wg-fleet.yaml
Restart=on-failure
User=root
WorkingDirectory=/opt/wg-fleet

[Install]
WantedBy=multi-user.target
```

Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable wg-fleet
sudo systemctl start wg-fleet
```

Check status:

```bash
sudo systemctl status wg-fleet
sudo journalctl -u wg-fleet -f
```

### File Locations

**Standard Deployment:**
- Application: `/opt/wg-fleet/`
- Configuration: `/etc/wg-fleet.yaml`
- Database: `/var/lib/wg-fleet/clients.db`
- WireGuard configs: `/etc/wireguard/wg_<fleet>.conf`, `/etc/wireguard/wgf<hash>.conf`
- Last accepted fleets.json: `/var/lib/wg-fleet/fleets.json` and `fleets.json.sig`
- Host CA: `/var/lib/wg-fleet/host_ca` and `host_ca.pub` (back these up)
- Hosts file: `/run/wg_fleet_hosts`
- Served fleets, for the management server's host-side script: `/run/wg_fleets.json`

## API Endpoints

### Client Registration

**Endpoint:** `POST /fleet/{fleet_name}/register`

**Description:** Register a new client to the fleet. Returns WireGuard configuration.

**Request:** No body required for static fleets. Fleets from `fleets_url` require:

```json
{
  "site": "BigSouth2026/MidAtlantic/VirginiaTech",
  "token": "<the site's enrollment token>",
  "host_key": "ssh-ed25519 AAAA..."
}
```

The token's sha256 must match the site's `token_sha256` in fleets.json; an unknown site or
a wrong token gets 403, and a missing or malformed `host_key` 400. Static fleets ignore these
fields and store neither. A removed or expired fleet refuses every registration (403).

**Response:**
```json
{
  "status": "success",
  "config": "[Interface]\nPrivateKey = ...\nAddress = fd00::1234/64\n\n[Peer]\nPublicKey = ...\nEndpoint = 1.2.3.4:51820\nAllowedIPs = fd00::1/128\nPersistentKeepalive = 25\n",
  "port": 51820,
  "server_ip": "fd00::1"
}
```

**Example:**
```bash
curl -X POST http://server:8000/fleet/southeast/register
```

The returned config can be saved to a file and used with WireGuard:
```bash
curl -X POST http://server:8000/fleet/southeast/register | jq -r '.config' > wg0.conf
sudo wg-quick up ./wg0.conf
```

### Client Heartbeat/Ping

**Endpoint:** `POST /fleet/{fleet_name}/ping`

**Description:** Send heartbeat to update last-seen timestamp and optionally set hostname.

**Request Body:**
```json
{
  "hostname": "optional-hostname"
}
```

**Response:**
```json
{
  "status": "ok",
  "host_certificate": "ssh-ed25519-cert-v01@openssh.com AAAA..."
}
```

`host_certificate` is only present for clients of fleets from `fleets_url` that registered a
host key and have a hostname. It certifies that host key for
`<hostname>.<fleet>.<domain>` (the name after deduplication) until the fleet's
`valid_until`, signed by the host CA. Every ping returns it; it's re-signed only when the
name or `valid_until` changes. Clients install it as their sshd `HostCertificate` when it
differs from the one they have. Static fleets never get certificates, since anyone can
register in them.

**Requirements:**
- Must be called from an IP within the fleet's subnet (via WireGuard tunnel)
- Hostname must be 1-59 characters of `[a-z0-9_-]`, not starting or ending with `-`
- Duplicate hostnames will be automatically numbered (e.g., excavator--2, excavator--3).
  A client keeps its numbered name on later pings that request the same base name.

**Example:**
```bash
# Without hostname
curl -X POST http://server:8000/fleet/southeast/ping -H "Content-Type: application/json" -d '{}'

# With hostname
curl -X POST http://server:8000/fleet/southeast/ping -H "Content-Type: application/json" -d '{"hostname":"excavator"}'
```

### Host CA

**Endpoint:** `GET /ssh/host_ca.pub`

The host CA's public key, created in `/var/lib/wg-fleet/host_ca` on first start (an existing
key there, e.g. restored from a backup, is kept). Trust it with
`@cert-authority *.icpcnet.internal <key>` in `known_hosts`, and give it to contestforge for
configs' `ssh_host_ca_keys`. Back it up: a new CA means every `known_hosts` and config has to
change.

### Re-registration

A client of a fleet from `fleets_url` that registers again with the same site and host key
(a new WireGuard key after a reboot or VPN failure) takes over its previous record's hostname
and certificate; the previous record and peer are removed at once instead of waiting to be
pruned. A different host key never takes another machine's name.

### Admin Dashboard

**Index Page:** `GET /`

Displays list of all configured fleets with links to detail pages.

**Fleet Detail Page:** `GET /fleet/{fleet_name}`

Shows table of all active clients in the fleet with:
- Hostname (or "(none)")
- Assigned IPv6 address
- Public key (truncated)
- Source IP from registration
- Last seen timestamp
- Last WireGuard handshake time

**Example:**
```bash
# View in browser
firefox http://server:8000/

# Or fetch with curl
curl http://server:8000/fleet/southeast
```

## Client Usage

### Initial Registration

On a client machine:

```bash
# Register and save config
curl -X POST http://server:8000/fleet/southeast/register | jq -r '.config' > /etc/wireguard/wg-fleet.conf

# Bring up the tunnel
sudo wg-quick up wg-fleet

# Verify connectivity
ping6 fd00:a0a8:34d:2a00::1
```

### Setting Hostname

After registration and tunnel is up:

```bash
curl -X POST http://server:8000/fleet/southeast/ping \
  -H "Content-Type: application/json" \
  -d '{"hostname":"myhost"}'
```

### Periodic Ping (Keep-Alive)

Set up a cron job or systemd timer to ping periodically:

```bash
# Add to crontab (ping every 5 minutes)
*/5 * * * * curl -X POST http://server:8000/fleet/southeast/ping -H "Content-Type: application/json" -d '{}'
```

Or create a systemd timer:

**`/etc/systemd/system/wg-fleet-ping.service`:**
```ini
[Unit]
Description=wg-fleet ping

[Service]
Type=oneshot
ExecStart=/usr/bin/curl -X POST http://server:8000/fleet/southeast/ping -H "Content-Type: application/json" -d '{}'
```

**`/etc/systemd/system/wg-fleet-ping.timer`:**
```ini
[Unit]
Description=wg-fleet ping timer

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
```

Enable the timer:
```bash
sudo systemctl enable wg-fleet-ping.timer
sudo systemctl start wg-fleet-ping.timer
```

## Hook System

wg-fleet uses a hook system for event-driven actions when clients change. This allows easy addition of integrations like DNS updates, monitoring, or service discovery.

### Adding a New Hook

1. Create a new file in the `hooks/` directory (e.g., `hooks/my_hook.py`)
2. Implement your hook function:

```python
from hook_manager import register_hook, HookContext, EventType
import logging

logger = logging.getLogger(__name__)

@register_hook
def my_hook(context: HookContext):
    """Description of what this hook does"""

    # Filter events if needed
    if context.event_type != EventType.CLIENT_ADDED:
        return

    # Your hook logic here
    # Access context.config, context.session_factory, context.client_data
    logger.info("My hook executed")
```

3. Import your hook in `hooks/__init__.py`:

```python
from . import my_hook
```

4. Done! Your hook will automatically run on client events.

### Available Events

- `EventType.STARTUP`: Application startup
- `EventType.CLIENT_ADDED`: A client re-registered and took over its previous hostname
  (a brand-new client has no hostname yet, so the hooks' output wouldn't change)
- `EventType.CLIENT_HOSTNAME_CHANGED`: Client hostname updated
- `EventType.CLIENT_REMOVED`: Client pruned or removed
- `EventType.FLEETS_CHANGED`: The served fleets changed (a new fleets.json, or a fleet
  expired or was torn down)

### Existing Hooks

- `hooks/hosts_file.py`: Generates `/run/wg_fleet_hosts` for hostname resolution
- `hooks/prometheus_sd.py`: Generates `/run/prometheus_targets.json` (Prometheus file-based service discovery, node_exporter on port 9100)
- `hooks/ansible_inventory.py`: Generates `/run/ansible_inventory.yml` with one group per fleet,
  hosts named by FQDN (`<hostname>.<fleet>.<domain>`)
- `hooks/fleets_file.py`: Generates `/run/wg_fleets.json` on startup and `FLEETS_CHANGED`: the
  fleets served (not removed ones) with their interface, port, subnet, `node` and
  `admin_principals` (`["node:*"]` for static fleets), plus the user CA keys from fleets.json.
  wg-fleet runs in a container, so creating users, repos, units and firewall rules is left to
  a script on the host run by a systemd `.path` unit on this file.

### Hook Behavior

- Hooks execute sequentially in registration order
- If a hook fails, the error is logged and execution continues with remaining hooks
- Each hook receives a `HookContext` with configuration, database session factory, and event metadata

## Hosts File Integration

wg-fleet generates `/run/wg_fleet_hosts` with entries for all clients that have hostnames:

```
fd00:a0a8:34d:2a00::1234 excavator.southeast.icpcnet.internal
fd00:a0a8:34d:2000::5678 driller.northwest.icpcnet.internal
```

To integrate with system DNS, include this file in your DNS server configuration or append to `/etc/hosts`:

```bash
# Option 1: Symlink or copy to /etc/hosts.d/ (if supported)
sudo ln -s /run/wg_fleet_hosts /etc/hosts.d/fleet

# Option 2: Append to /etc/hosts
cat /run/wg_fleet_hosts | sudo tee -a /etc/hosts
```

For dynamic integration, you could use a cron job to periodically merge the fleet_hosts file.

## Development and Testing

### Running Tests

Install test dependencies:
```bash
pip install -r requirements-dev.txt
```

Run all tests:
```bash
pytest -v
```

Run specific test file:
```bash
pytest test_config.py -v
```

Run with coverage:
```bash
pytest --cov=. --cov-report=html
```

### Test Structure

The test suite includes:
- **Unit tests**: `test_config.py`, `test_database.py`, `test_command.py`, `test_wireguard.py`, `test_hook_manager.py`, `test_hooks.py`, `test_pruning.py`, `test_main.py`, `test_fleet_source.py`, `test_fleet_registry.py`
- **API tests**: `test_routes.py`
- **Integration tests**: `test_integration.py`

### Manual Testing Checklist

1. **Registration**: Multiple clients register successfully, receive unique IPs
2. **Ping**: Clients ping with/without hostnames
3. **Hostname Deduplication**: Multiple clients with same hostname get numbered suffixes
4. **Pruning**: Inactive clients removed after timeout
5. **Dashboard**: Shows accurate client information with WireGuard stats
6. **Startup Reconciliation**: Server recovers correctly after restart
7. **Fleet Isolation**: Clients in different fleets don't interfere

## Troubleshooting

### Server won't start

**Check logs:**
```bash
sudo journalctl -u wg-fleet -n 50
```

**Common issues:**
- Config file not found or invalid YAML
- Database directory doesn't exist or wrong permissions
- WireGuard tools not installed
- Port already in use

### Client can't register

**Verify server is reachable:**
```bash
curl http://server:8000/
```

**Check fleet name is correct:**
- Must match exactly (case-sensitive)
- Must be defined in config file

### Client gets pruned

Pruning is based on the WireGuard handshake time. A client whose last handshake is older
than `prune_timeout` is removed. A client that has never completed a handshake is removed
once its registration/ping timestamp is older than `prune_timeout`.

**Causes:**
- WireGuard handshake timeout (tunnel down, or no traffic through it)
- Registered but never brought the tunnel up

**Solution:**
- Make sure the tunnel is up; the generated config's `PersistentKeepalive = 25` keeps handshakes fresh
- Increase `prune_timeout` in config if needed

### Hostname not appearing

**Check:**
1. Hostname was set via ping endpoint
2. Client is active (not pruned)
3. `/run/wg_fleet_hosts` file exists and contains entry

```bash
cat /run/wg_fleet_hosts | grep myhost
```

### Dashboard shows "N/A" for handshake

**Causes:**
- Client hasn't sent any traffic through WireGuard tunnel yet
- WireGuard interface is down

**Solution:**
- Send some traffic (e.g., ping the server IPv6)
- Verify WireGuard interface is up: `sudo wg show`

## Security Considerations

### Authentication

Registration to fleets from `fleets_url` needs the site's enrollment token, which machines
get in their site's encrypted config; wg-fleet only ever sees the token's hash. Machines keep
using the token they got at setup, so rotating a site's token in contestforge stops machines
already set up there from re-registering after a reboot or VPN failure.

Static fleets accept registration from anyone who can reach the server. The dashboard has
no authentication of its own. Put a reverse proxy in front: basic auth for the dashboard, and
optionally a per-source-IP rate limit (e.g. nginx `limit_req`) on `/fleet/*/register`. Make
its burst generous: a site's machines usually share one NAT address and all register at
once (at setup, or after a power cut or a server restart).

wg-fleet itself has no rate limit. A bad token is refused after one sha256, before anything
is created, and tokens are too long to guess; a limit per fleet would mostly let anyone
lock a fleet's machines out by sending bad requests.

### IP Validation

The ping endpoint validates that requests originate from within the fleet's IPv6 subnet, preventing external hosts from modifying client records.

### Sensitive Data

Private keys are never logged: they are passed to `wg` on stdin, and only command arguments are logged (at DEBUG level). Keep secrets out of command arguments.

### Database Permissions

Ensure database file has appropriate permissions:
```bash
sudo chown root:root /var/lib/wg-fleet/clients.db
sudo chmod 600 /var/lib/wg-fleet/clients.db
```

## License

[Specify license here]

## Contributing

[Specify contribution guidelines here]

## Support

[Specify support contact or issue tracker here]
