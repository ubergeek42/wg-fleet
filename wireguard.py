from command import run_command, CommandError
from datetime import datetime, UTC
from typing import Dict, List, Tuple
import logging
import ipaddress
import os
from pathlib import Path

logger = logging.getLogger(__name__)

def generate_keypair() -> Tuple[str, str]:
    """
    Generate WireGuard private/public keypair.

    Returns:
        Tuple of (private_key, public_key)
    """
    private_key = run_command(['wg', 'genkey'])
    public_key = run_command(['wg', 'pubkey'], input_data=private_key)
    return (private_key, public_key)

def render_interface_config(fleet_config, private_key: str) -> str:
    """
    Build WireGuard interface configuration text for the server.

    Args:
        fleet_config: Fleet configuration with ip6, subnet, port
        private_key: Server's private key

    Returns:
        Interface config as string
    """
    # Extract prefix length from subnet
    network = ipaddress.IPv6Network(fleet_config.subnet, strict=False)
    prefix_len = network.prefixlen

    return f"""[Interface]
Address = {fleet_config.ip6}/{prefix_len}
ListenPort = {fleet_config.port}
PrivateKey = {private_key}
"""

def read_private_key(config_path: str) -> str:
    """
    Read the server's private key from an existing interface config file.

    Args:
        config_path: Path to the interface config

    Returns:
        Private key string

    Raises:
        ValueError: If the file has no PrivateKey line
    """
    for line in Path(config_path).read_text().splitlines():
        key, sep, value = line.partition('=')
        if sep and key.strip() == 'PrivateKey':
            # Keys are base64 and may end in '=', so take everything after the first '='
            return value.strip()
    raise ValueError(f"No PrivateKey in {config_path}")

def create_interface_config(
    interface: str,
    fleet_config: Dict,
    private_key: str,
    config_path: str = None
) -> None:
    """
    Write WireGuard interface configuration file.

    Args:
        interface: WireGuard interface name
        fleet_config: Fleet configuration dict with ip6, port
        private_key: Server's private key
        config_path: Optional custom path (defaults to /etc/wireguard/<interface>.conf)
    """
    if config_path is None:
        config_path = f'/etc/wireguard/{interface}.conf'

    config_content = render_interface_config(fleet_config, private_key)

    # Create with restrictive permissions up front so the private key is never world-readable
    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(config_content)
    logger.info(f"Created WireGuard config: {config_path}")

def interface_exists(interface: str) -> bool:
    """
    Check if WireGuard interface exists.

    Args:
        interface: WireGuard interface name

    Returns:
        True if interface exists, False otherwise
    """
    try:
        run_command(['wg', 'show', interface])
        return True
    except CommandError:
        return False

def bring_up_interface(interface: str) -> None:
    """
    Bring up WireGuard interface.

    Args:
        interface: WireGuard interface name
    """
    # Check if interface is already up
    if interface_exists(interface):
        logger.info(f"Interface {interface} already exists, skipping bring up")
        return

    run_command(['wg-quick', 'up', interface])
    logger.info(f"Brought up interface: {interface}")

def bring_down_interface(interface: str) -> None:
    """
    Shutdown WireGuard interface.

    Args:
        interface: WireGuard interface name
    """
    run_command(['wg-quick', 'down', interface])
    logger.info(f"Brought down interface: {interface}")

def add_peer(interface: str, public_key: str, allowed_ip: str) -> None:
    """
    Add peer to WireGuard interface.

    Args:
        interface: WireGuard interface name
        public_key: Peer's public key
        allowed_ip: IPv6 address for peer
    """
    run_command([
        'wg', 'set', interface,
        'peer', public_key,
        'allowed-ips', allowed_ip
    ])
    logger.info(f"Added peer to {interface}: {allowed_ip}")

def remove_peer(interface: str, public_key: str) -> None:
    """
    Remove peer from WireGuard interface.

    Args:
        interface: WireGuard interface name
        public_key: Peer's public key to remove
    """
    run_command([
        'wg', 'set', interface,
        'peer', public_key,
        'remove'
    ])
    logger.info(f"Removed peer from {interface}")

def list_peers(interface: str) -> List[Dict]:
    """
    Get all peers with handshake information.

    Args:
        interface: WireGuard interface name

    Returns:
        List of peer dicts with keys: public_key, allowed_ips, last_handshake, etc.
    """
    output = run_command(['wg', 'show', interface, 'dump'])

    peers = []
    for line in output.split('\n')[1:]:  # Skip first line (server interface)
        if not line.strip():
            continue

        parts = line.split('\t')
        if len(parts) < 8:  # Peer lines have 8 fields
            continue

        # Parse last handshake timestamp (field 4)
        last_handshake = None
        if parts[4] and parts[4] != '0':
            last_handshake = datetime.fromtimestamp(int(parts[4]), UTC)

        peers.append({
            'public_key': parts[0],
            'allowed_ips': parts[3],
            'endpoint': parts[2] if parts[2] != '(none)' else None,
            'last_handshake': last_handshake,
            'rx_bytes': int(parts[5]) if parts[5] else 0,
            'tx_bytes': int(parts[6]) if parts[6] else 0
        })

    return peers

def get_server_public_key(interface: str) -> str:
    """
    Extract server's public key from interface.

    Args:
        interface: WireGuard interface name

    Returns:
        Server's public key string
    """
    return run_command(['wg', 'show', interface, 'public-key'])

def build_client_config(
    client_private_key: str,
    client_ip: str,
    server_public_key: str,
    endpoint_ip: str,
    endpoint_port: int,
    server_ip: str,
    subnet: str
) -> str:
    """
    Build WireGuard configuration text for client.

    Args:
        client_private_key: Client's private key
        client_ip: Client's assigned IPv6
        server_public_key: Server's public key
        endpoint_ip: Server's external IP
        endpoint_port: Server's WireGuard port
        server_ip: Server's internal IPv6 (for AllowedIPs)
        subnet: Fleet subnet in CIDR notation (e.g., fd00::/64)

    Returns:
        WireGuard config as string
    """
    # Extract prefix length from subnet
    network = ipaddress.IPv6Network(subnet, strict=False)
    prefix_len = network.prefixlen

    # IPv6 endpoints need brackets so the port isn't read as part of the address
    if ipaddress.ip_address(endpoint_ip).version == 6:
        endpoint_ip = f"[{endpoint_ip}]"

    return f"""[Interface]
PrivateKey = {client_private_key}
Address = {client_ip}/{prefix_len}

[Peer]
PublicKey = {server_public_key}
Endpoint = {endpoint_ip}:{endpoint_port}
AllowedIPs = {server_ip}/128
PersistentKeepalive = 25
"""
