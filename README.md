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
- `fleets`: Dictionary of fleet configurations
  - `ip6`: Server's IPv6 address for this fleet
  - `subnet`: IPv6 subnet for client allocation (CIDR notation)
  - `external_ip`: Public IP address clients connect to
  - `port`: UDP port for this WireGuard interface

### Duration Format

The `prune_timeout` field accepts duration strings:
- `30m` - 30 minutes
- `1h` - 1 hour
- `2h30m` - 2 hours 30 minutes

### Database Setup

The database is automatically initialized on first startup. By default, it's stored at `/var/lib/wg-fleet/clients.db`.

Ensure the directory exists and has proper permissions:

```bash
sudo mkdir -p /var/lib/wg-fleet
sudo chown root:root /var/lib/wg-fleet
sudo chmod 755 /var/lib/wg-fleet
```

### WireGuard Configuration

On first startup, wg-fleet will:
1. Generate server keypairs for each fleet (if config doesn't exist)
2. Create `/etc/wireguard/wg_<fleetname>.conf` files
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
- WireGuard configs: `/etc/wireguard/wg_<fleet>.conf`
- Hosts file: `/run/wg_fleet_hosts`

## API Endpoints

### Client Registration

**Endpoint:** `POST /fleet/{fleet_name}/register`

**Description:** Register a new client to the fleet. Returns WireGuard configuration.

**Request:** No body required.

**Response:**
```json
{
  "status": "success",
  "config": "[Interface]\nPrivateKey = ...\nAddress = fd00::1234\n\n[Peer]\nPublicKey = ...\nEndpoint = 1.2.3.4:51820\nAllowedIPs = fd00::1/128\nPersistentKeepalive = 25\n"
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
  "status": "ok"
}
```

**Requirements:**
- Must be called from an IP within the fleet's subnet (via WireGuard tunnel)
- Hostname must match regex: `[a-z0-9_-]+`
- Duplicate hostnames will be automatically numbered (e.g., excavator2, excavator3)

**Example:**
```bash
# Without hostname
curl -X POST http://server:8000/fleet/southeast/ping -H "Content-Type: application/json" -d '{}'

# With hostname
curl -X POST http://server:8000/fleet/southeast/ping -H "Content-Type: application/json" -d '{"hostname":"excavator"}'
```

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
pip install pytest pytest-asyncio
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
- **Unit tests**: `test_config.py`, `test_database.py`, `test_command.py`, `test_wireguard.py`, `test_hosts.py`, `test_pruning.py`, `test_main.py`
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

**Causes:**
- Not pinging regularly
- WireGuard handshake timeout (no traffic through tunnel)

**Solution:**
- Set up periodic ping (see Client Usage section)
- Ensure traffic flows through tunnel periodically
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

### No Authentication

wg-fleet currently has **no authentication** on any endpoint. This design assumes:
- Deployment in trusted network
- Network-level security (firewall rules)
- Clients operate in environment where secrets would be compromised anyway

**Recommendation:** Use a reverse proxy with authentication if deploying in untrusted environments.

### IP Validation

The ping endpoint validates that requests originate from within the fleet's IPv6 subnet, preventing external hosts from modifying client records.

### Sensitive Data

Private keys are never logged. The `command.py` module redacts sensitive patterns from log output.

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
