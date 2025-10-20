from command import run_command
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Tuple
import logging

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

def create_interface_config(
    fleet_name: str,
    fleet_config: Dict,
    private_key: str,
    config_path: str = None
) -> None:
    """
    Write WireGuard interface configuration file.

    Args:
        fleet_name: Name of the fleet
        fleet_config: Fleet configuration dict with ip6, port
        private_key: Server's private key
        config_path: Optional custom path (defaults to /etc/wireguard/wg_<fleet>.conf)
    """
    if config_path is None:
        config_path = f'/etc/wireguard/wg_{fleet_name}.conf'

    config_content = f"""[Interface]
Address = {fleet_config['ip6']}
ListenPort = {fleet_config['port']}
PrivateKey = {private_key}
"""

    Path(config_path).write_text(config_content)
    logger.info(f"Created WireGuard config: {config_path}")

def interface_exists(fleet_name: str) -> bool:
    """
    Check if WireGuard interface exists.

    Args:
        fleet_name: Name of the fleet

    Returns:
        True if interface exists, False otherwise
    """
    try:
        run_command(['wg', 'show', f'wg_{fleet_name}'])
        return True
    except:
        return False

def bring_up_interface(fleet_name: str) -> None:
    """
    Bring up WireGuard interface.

    Args:
        fleet_name: Name of the fleet
    """
    run_command(['wg-quick', 'up', f'wg_{fleet_name}'])
    logger.info(f"Brought up interface: wg_{fleet_name}")

def bring_down_interface(fleet_name: str) -> None:
    """
    Shutdown WireGuard interface.

    Args:
        fleet_name: Name of the fleet
    """
    run_command(['wg-quick', 'down', f'wg_{fleet_name}'])
    logger.info(f"Brought down interface: wg_{fleet_name}")

def add_peer(fleet_name: str, public_key: str, allowed_ip: str) -> None:
    """
    Add peer to WireGuard interface.

    Args:
        fleet_name: Name of the fleet
        public_key: Peer's public key
        allowed_ip: IPv6 address for peer
    """
    run_command([
        'wg', 'set', f'wg_{fleet_name}',
        'peer', public_key,
        'allowed-ips', allowed_ip
    ])
    logger.debug(f"Added peer to wg_{fleet_name}: {allowed_ip}")

def remove_peer(fleet_name: str, public_key: str) -> None:
    """
    Remove peer from WireGuard interface.

    Args:
        fleet_name: Name of the fleet
        public_key: Peer's public key to remove
    """
    run_command([
        'wg', 'set', f'wg_{fleet_name}',
        'peer', public_key,
        'remove'
    ])
    logger.debug(f"Removed peer from wg_{fleet_name}")

def list_peers(fleet_name: str) -> List[Dict]:
    """
    Get all peers with handshake information.

    Args:
        fleet_name: Name of the fleet

    Returns:
        List of peer dicts with keys: public_key, allowed_ips, last_handshake, etc.
    """
    output = run_command(['wg', 'show', f'wg_{fleet_name}', 'dump'])

    peers = []
    for line in output.split('\n')[1:]:  # Skip header line
        if not line.strip():
            continue

        parts = line.split('\t')
        if len(parts) < 4:
            continue

        # Parse last handshake timestamp
        last_handshake = None
        if parts[3] and parts[3] != '0':
            last_handshake = datetime.fromtimestamp(int(parts[3]))

        peers.append({
            'public_key': parts[0],
            'allowed_ips': parts[1],
            'last_handshake': last_handshake,
            'rx_bytes': int(parts[4]) if len(parts) > 4 else 0,
            'tx_bytes': int(parts[5]) if len(parts) > 5 else 0
        })

    return peers

def get_server_public_key(fleet_name: str) -> str:
    """
    Extract server's public key from interface.

    Args:
        fleet_name: Name of the fleet

    Returns:
        Server's public key string
    """
    return run_command(['wg', 'show', f'wg_{fleet_name}', 'public-key'])

def build_client_config(
    client_private_key: str,
    client_ip: str,
    server_public_key: str,
    endpoint_ip: str,
    endpoint_port: int,
    server_ip: str
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

    Returns:
        WireGuard config as string
    """
    return f"""[Interface]
PrivateKey = {client_private_key}
Address = {client_ip}

[Peer]
PublicKey = {server_public_key}
Endpoint = {endpoint_ip}:{endpoint_port}
AllowedIPs = {server_ip}/128
PersistentKeepalive = 25
"""
