import yaml
from pathlib import Path
from dataclasses import dataclass
import re
from datetime import timedelta
from typing import Dict
import ipaddress

@dataclass
class FleetConfig:
    """Configuration for a single fleet"""
    ip6: str
    subnet: str
    external_ip: str
    port: int

@dataclass
class Config:
    """Main application configuration"""
    domain: str
    prune_timeout: str
    fleets: Dict[str, FleetConfig]

def parse_duration(duration_str: str) -> timedelta:
    """
    Parse duration string like '30m', '1h', '2h30m'.

    Args:
        duration_str: Duration string with format like "30m" or "1h" or "2h30m"

    Returns:
        timedelta object

    Raises:
        ValueError: If format is invalid
    """
    match = re.fullmatch(r'(?:(\d+)h)?(?:(\d+)m)?', str(duration_str).strip())
    if not match:
        raise ValueError(f"Invalid duration format: {duration_str}")

    hours, minutes = match.groups()
    total_minutes = int(hours or 0) * 60 + int(minutes or 0)

    if total_minutes == 0:
        raise ValueError(f"Invalid duration format: {duration_str}")

    return timedelta(minutes=total_minutes)

def load_config(path: str = "/etc/wg-fleet.yaml") -> Config:
    """
    Load and validate configuration file.

    Args:
        path: Path to YAML configuration file

    Returns:
        Config object

    Raises:
        FileNotFoundError: If config file doesn't exist
        ValueError: If config is invalid
    """
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with config_path.open() as f:
        data = yaml.safe_load(f)

    # Validate required fields
    if 'domain' not in data:
        raise ValueError("Missing 'domain' in config")
    if 'prune_timeout' not in data:
        raise ValueError("Missing 'prune_timeout' in config")
    try:
        parse_duration(data['prune_timeout'])
    except ValueError as e:
        raise ValueError(f"Invalid 'prune_timeout': {e}")
    if 'fleets' not in data or not data['fleets']:
        raise ValueError("No fleets configured")

    # Parse fleets
    fleets = {}
    for name, fleet_data in data['fleets'].items():
        # Fleet name becomes the interface name wg_<name> (Linux limit: 15 chars),
        # a config file path, and a DNS label
        if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9-]{1,12}', name):
            raise ValueError(
                f"Fleet name '{name}' must be 1-12 characters of lowercase letters, digits, or '-'"
            )

        required_fields = ['ip6', 'subnet', 'external_ip', 'port']
        for field in required_fields:
            if field not in fleet_data:
                raise ValueError(f"Fleet '{name}' missing required field: {field}")

        # Validate port type and range
        port = fleet_data['port']
        if not isinstance(port, int):
            raise ValueError(f"Fleet '{name}': port must be an integer")
        if port < 1 or port > 65535:
            raise ValueError(f"Fleet '{name}': port must be between 1 and 65535")

        # Validate IPv6 address
        try:
            ipaddress.IPv6Address(fleet_data['ip6'])
        except (ipaddress.AddressValueError, ValueError) as e:
            raise ValueError(f"Fleet '{name}': invalid IPv6 address '{fleet_data['ip6']}': {e}")

        # Validate external IP (clients connect to it; IPv4 or IPv6)
        try:
            ipaddress.ip_address(fleet_data['external_ip'])
        except ValueError as e:
            raise ValueError(f"Fleet '{name}': invalid external_ip '{fleet_data['external_ip']}': {e}")

        # Validate IPv6 subnet (strict: no host bits set, e.g. fd00::/64 not fd00::1/64)
        try:
            network = ipaddress.IPv6Network(fleet_data['subnet'])
        except (ipaddress.AddressValueError, ValueError) as e:
            raise ValueError(f"Fleet '{name}': invalid IPv6 subnet '{fleet_data['subnet']}': {e}")

        # Client addresses are picked at random without collision checks, so the subnet must be large
        if network.prefixlen > 64:
            raise ValueError(f"Fleet '{name}': subnet must be /64 or larger, got /{network.prefixlen}")

        if ipaddress.IPv6Address(fleet_data['ip6']) not in network:
            raise ValueError(f"Fleet '{name}': ip6 {fleet_data['ip6']} is not in subnet {network}")

        fleets[name] = FleetConfig(
            ip6=fleet_data['ip6'],
            subnet=fleet_data['subnet'],
            external_ip=fleet_data['external_ip'],
            port=port
        )

    return Config(
        domain=data['domain'],
        prune_timeout=data['prune_timeout'],
        fleets=fleets
    )
