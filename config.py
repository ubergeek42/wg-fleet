import yaml
from pathlib import Path
from dataclasses import dataclass
import re
from datetime import timedelta
from typing import Dict

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
    total_minutes = 0

    # Match hours
    hour_match = re.search(r'(\d+)h', duration_str)
    if hour_match:
        total_minutes += int(hour_match.group(1)) * 60

    # Match minutes
    min_match = re.search(r'(\d+)m', duration_str)
    if min_match:
        total_minutes += int(min_match.group(1))

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
    if 'fleets' not in data or not data['fleets']:
        raise ValueError("No fleets configured")

    # Parse fleets
    fleets = {}
    for name, fleet_data in data['fleets'].items():
        required_fields = ['ip6', 'subnet', 'external_ip', 'port']
        for field in required_fields:
            if field not in fleet_data:
                raise ValueError(f"Fleet '{name}' missing required field: {field}")

        fleets[name] = FleetConfig(
            ip6=fleet_data['ip6'],
            subnet=fleet_data['subnet'],
            external_ip=fleet_data['external_ip'],
            port=fleet_data['port']
        )

    return Config(
        domain=data['domain'],
        prune_timeout=data['prune_timeout'],
        fleets=fleets
    )
