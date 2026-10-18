import yaml
from pathlib import Path
from dataclasses import dataclass, field
import hashlib
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import ipaddress

# Static fleets keep the interface name wg_<fleet> (Linux limit: 15 chars), so their
# names stay short. Fleets from fleets_url get a hashed interface name (interface_name),
# so their names only have to fit a Unix user name (32 chars) and a DNS label.
STATIC_FLEET_NAME = re.compile(r'[a-z0-9-]{1,12}')
URL_FLEET_NAME = re.compile(r'[a-z]([a-z0-9-]{0,30}[a-z0-9])?')

@dataclass
class FleetConfig:
    """Configuration for a single fleet"""
    ip6: str
    subnet: str
    external_ip: str
    port: int
    # WireGuard interface name; derived from the fleet name by interface_name()
    interface: str = ''
    # Static fleets come from the config file and accept unauthenticated registration.
    # The others come from the signed fleets.json at fleets_url.
    static: bool = True
    # The rest only applies to fleets from fleets_url
    node: Optional[str] = None
    valid_until: Optional[datetime] = None
    # site id -> sha256 hex digest of its enrollment token
    sites: Dict[str, str] = field(default_factory=dict)
    admin_principals: List[str] = field(default_factory=list)
    # Set once the fleet left fleets.json or passed valid_until: existing clients keep
    # their peers until pruned, but nobody new can register, admins lose access
    # (it's left out of /run/wg_fleets.json), and the interface is torn down later
    removed: bool = False

@dataclass
class Config:
    """Main application configuration"""
    domain: str
    prune_timeout: str
    # Fleets currently served: the static ones plus those from fleets_url. Replaced
    # (never mutated) when fleets.json changes, so readers always see a consistent dict.
    fleets: Dict[str, FleetConfig]
    static_fleets: Dict[str, FleetConfig] = field(default_factory=dict)
    fleets_url: Optional[str] = None
    # allowed_signers lines trusted to sign fleets.json
    config_signers: List[str] = field(default_factory=list)
    config_signer_id: str = 'contestforge'
    # Which fleets.json entries are ours (their "server")
    server_name: Optional[str] = None
    # WireGuard endpoint for fleets from fleets_url
    external_ip: Optional[str] = None
    refresh_interval: str = '1m'
    # How long a removed or expired fleet with no clients left is kept before its
    # interface is torn down
    removed_fleet_grace: str = '24h'
    # User CA keys from fleets.json, for the bastion (/run/wg_fleets.json)
    user_ca_keys: List[str] = field(default_factory=list)

def interface_name(fleet_name: str, static: bool) -> str:
    """
    WireGuard interface name for a fleet.

    Static fleets keep wg_<fleet> so running deployments don't change. Fleets from
    fleets_url can have names up to 32 characters, longer than Linux's 15-character
    interface limit, so they get wgf + the first 8 hex digits of sha256(name).
    """
    if static:
        return f'wg_{fleet_name}'
    return 'wgf' + hashlib.sha256(fleet_name.encode()).hexdigest()[:8]

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

def validate_fleet_addresses(name: str, ip6, subnet, port, external_ip) -> None:
    """
    Check a fleet's addresses, for static fleets and those from fleets.json alike.

    Raises:
        ValueError: If any address or the port is invalid
    """
    # Validate port type and range (bool is an int subclass, but never a port)
    if not isinstance(port, int) or isinstance(port, bool):
        raise ValueError(f"Fleet '{name}': port must be an integer")
    if port < 1 or port > 65535:
        raise ValueError(f"Fleet '{name}': port must be between 1 and 65535")

    # Validate IPv6 address
    try:
        ipaddress.IPv6Address(ip6)
    except (ipaddress.AddressValueError, ValueError) as e:
        raise ValueError(f"Fleet '{name}': invalid IPv6 address '{ip6}': {e}")

    # Validate external IP (clients connect to it; IPv4 or IPv6)
    try:
        ipaddress.ip_address(external_ip)
    except ValueError as e:
        raise ValueError(f"Fleet '{name}': invalid external_ip '{external_ip}': {e}")

    # Validate IPv6 subnet (strict: no host bits set, e.g. fd00::/64 not fd00::1/64)
    try:
        network = ipaddress.IPv6Network(subnet)
    except (ipaddress.AddressValueError, ValueError, TypeError) as e:
        raise ValueError(f"Fleet '{name}': invalid IPv6 subnet '{subnet}': {e}")

    # Client addresses are picked at random without collision checks, so the subnet must be large
    if network.prefixlen > 64:
        raise ValueError(f"Fleet '{name}': subnet must be /64 or larger, got /{network.prefixlen}")

    if ipaddress.IPv6Address(ip6) not in network:
        raise ValueError(f"Fleet '{name}': ip6 {ip6} is not in subnet {network}")

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

    fleets_url = data.get('fleets_url')
    if not data.get('fleets') and not fleets_url:
        raise ValueError("No fleets configured (set 'fleets', 'fleets_url', or both)")

    if fleets_url:
        if not str(fleets_url).startswith(('https://', 'http://')):
            raise ValueError(f"'fleets_url' must be an http(s) URL, got '{fleets_url}'")
        for key in ('config_signers', 'server_name', 'external_ip'):
            if not data.get(key):
                raise ValueError(f"'{key}' is required when 'fleets_url' is set")

    signers = data.get('config_signers') or []
    if isinstance(signers, str):
        signers = [line for line in signers.splitlines() if line.strip()]
    if not isinstance(signers, list) or not all(isinstance(s, str) for s in signers):
        raise ValueError("'config_signers' must be a list of allowed_signers lines")

    external_ip = data.get('external_ip')
    if external_ip is not None:
        try:
            ipaddress.ip_address(external_ip)
        except ValueError as e:
            raise ValueError(f"Invalid 'external_ip' '{external_ip}': {e}")

    for key in ('refresh_interval', 'removed_fleet_grace'):
        if key in data:
            try:
                parse_duration(data[key])
            except ValueError as e:
                raise ValueError(f"Invalid '{key}': {e}")

    # Parse fleets
    fleets = {}
    for name, fleet_data in (data.get('fleets') or {}).items():
        # Fleet name becomes the interface name wg_<name> (Linux limit: 15 chars),
        # a config file path, and a DNS label
        if not isinstance(name, str) or not STATIC_FLEET_NAME.fullmatch(name):
            raise ValueError(
                f"Fleet name '{name}' must be 1-12 characters of lowercase letters, digits, or '-'"
            )

        # A static fleet may leave out external_ip and use the server-level one
        fleet_data = dict(fleet_data)
        if external_ip is not None:
            fleet_data.setdefault('external_ip', external_ip)

        required_fields = ['ip6', 'subnet', 'external_ip', 'port']
        for field_name in required_fields:
            if field_name not in fleet_data:
                raise ValueError(f"Fleet '{name}' missing required field: {field_name}")

        validate_fleet_addresses(name, fleet_data['ip6'], fleet_data['subnet'],
                                 fleet_data['port'], fleet_data['external_ip'])

        fleets[name] = FleetConfig(
            ip6=fleet_data['ip6'],
            subnet=fleet_data['subnet'],
            external_ip=fleet_data['external_ip'],
            port=fleet_data['port'],
            interface=interface_name(name, static=True),
        )

    return Config(
        domain=data['domain'],
        prune_timeout=data['prune_timeout'],
        fleets=dict(fleets),
        static_fleets=fleets,
        fleets_url=fleets_url,
        config_signers=signers,
        config_signer_id=data.get('config_signer_id', 'contestforge'),
        server_name=data.get('server_name'),
        external_ip=external_ip,
        refresh_interval=data.get('refresh_interval', '1m'),
        removed_fleet_grace=data.get('removed_fleet_grace', '24h'),
    )
