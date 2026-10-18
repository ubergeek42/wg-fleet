import pytest
from pathlib import Path
from config import load_config, Config, FleetConfig, parse_duration
from datetime import timedelta
import tempfile

def test_load_valid_config():
    """Test loading a valid configuration file"""
    config_content = """
domain: test.internal
prune_timeout: 30m
fleets:
  testfleet:
    ip6: "fd00::1"
    subnet: "fd00::/64"
    external_ip: "1.2.3.4"
    port: 51820
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(config_content)
        config_path = f.name

    try:
        config = load_config(config_path)
        assert config.domain == "test.internal"
        assert config.prune_timeout == "30m"
        assert "testfleet" in config.fleets
        assert config.fleets["testfleet"].ip6 == "fd00::1"
    finally:
        Path(config_path).unlink()

def test_load_missing_config():
    """Test that missing config raises FileNotFoundError"""
    with pytest.raises(FileNotFoundError):
        load_config("/nonexistent/config.yaml")

def test_parse_duration():
    """Test duration string parsing"""
    assert parse_duration("30m") == timedelta(minutes=30)
    assert parse_duration("1h") == timedelta(hours=1)
    assert parse_duration("2h30m") == timedelta(hours=2, minutes=30)

@pytest.mark.parametrize('duration', ['', '0m', '30s', '1d', '90', '1h30', '30m1h', 'x30m'])
def test_parse_duration_rejects_invalid(duration):
    """Test malformed or unsupported durations are rejected, not partially parsed"""
    with pytest.raises(ValueError):
        parse_duration(duration)

def test_invalid_prune_timeout_rejected_at_load():
    """Test a bad prune_timeout fails config loading instead of later pruning"""
    config_content = """
domain: test.internal
prune_timeout: 30s
fleets:
  testfleet:
    ip6: "fd00::1"
    subnet: "fd00::/64"
    external_ip: "1.2.3.4"
    port: 51820
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(config_content)
        config_path = f.name

    try:
        with pytest.raises(ValueError, match="prune_timeout"):
            load_config(config_path)
    finally:
        Path(config_path).unlink()

def test_invalid_port_type():
    """Test that non-integer port raises ValueError"""
    config_content = """
domain: test.internal
prune_timeout: 30m
fleets:
  testfleet:
    ip6: "fd00::1"
    subnet: "fd00::/64"
    external_ip: "1.2.3.4"
    port: "51820"
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(config_content)
        config_path = f.name

    try:
        with pytest.raises(ValueError, match="port must be an integer"):
            load_config(config_path)
    finally:
        Path(config_path).unlink()

def test_invalid_port_range_low():
    """Test that port < 1 raises ValueError"""
    config_content = """
domain: test.internal
prune_timeout: 30m
fleets:
  testfleet:
    ip6: "fd00::1"
    subnet: "fd00::/64"
    external_ip: "1.2.3.4"
    port: 0
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(config_content)
        config_path = f.name

    try:
        with pytest.raises(ValueError, match="port must be between 1 and 65535"):
            load_config(config_path)
    finally:
        Path(config_path).unlink()

def test_invalid_port_range_high():
    """Test that port > 65535 raises ValueError"""
    config_content = """
domain: test.internal
prune_timeout: 30m
fleets:
  testfleet:
    ip6: "fd00::1"
    subnet: "fd00::/64"
    external_ip: "1.2.3.4"
    port: 65536
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(config_content)
        config_path = f.name

    try:
        with pytest.raises(ValueError, match="port must be between 1 and 65535"):
            load_config(config_path)
    finally:
        Path(config_path).unlink()

def test_invalid_ipv6_address():
    """Test that invalid IPv6 address raises ValueError"""
    config_content = """
domain: test.internal
prune_timeout: 30m
fleets:
  testfleet:
    ip6: "192.168.1.1"
    subnet: "fd00::/64"
    external_ip: "1.2.3.4"
    port: 51820
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(config_content)
        config_path = f.name

    try:
        with pytest.raises(ValueError, match="invalid IPv6 address"):
            load_config(config_path)
    finally:
        Path(config_path).unlink()

def test_malformed_ipv6_address():
    """Test that malformed IPv6 address raises ValueError"""
    config_content = """
domain: test.internal
prune_timeout: 30m
fleets:
  testfleet:
    ip6: "not-an-ip"
    subnet: "fd00::/64"
    external_ip: "1.2.3.4"
    port: 51820
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(config_content)
        config_path = f.name

    try:
        with pytest.raises(ValueError, match="invalid IPv6 address"):
            load_config(config_path)
    finally:
        Path(config_path).unlink()

def test_invalid_ipv6_subnet():
    """Test that invalid IPv6 subnet raises ValueError"""
    config_content = """
domain: test.internal
prune_timeout: 30m
fleets:
  testfleet:
    ip6: "fd00::1"
    subnet: "192.168.1.0/24"
    external_ip: "1.2.3.4"
    port: 51820
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(config_content)
        config_path = f.name

    try:
        with pytest.raises(ValueError, match="invalid IPv6 subnet"):
            load_config(config_path)
    finally:
        Path(config_path).unlink()

def test_malformed_ipv6_subnet():
    """Test that malformed IPv6 subnet raises ValueError"""
    config_content = """
domain: test.internal
prune_timeout: 30m
fleets:
  testfleet:
    ip6: "fd00::1"
    subnet: "not-a-subnet"
    external_ip: "1.2.3.4"
    port: 51820
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(config_content)
        config_path = f.name

    try:
        with pytest.raises(ValueError, match="invalid IPv6 subnet"):
            load_config(config_path)
    finally:
        Path(config_path).unlink()

def test_valid_edge_case_ports():
    """Test that valid edge case ports (1 and 65535) are accepted"""
    # Test port 1
    config_content = """
domain: test.internal
prune_timeout: 30m
fleets:
  testfleet:
    ip6: "fd00::1"
    subnet: "fd00::/64"
    external_ip: "1.2.3.4"
    port: 1
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(config_content)
        config_path = f.name

    try:
        config = load_config(config_path)
        assert config.fleets["testfleet"].port == 1
    finally:
        Path(config_path).unlink()

    # Test port 65535
    config_content = """
domain: test.internal
prune_timeout: 30m
fleets:
  testfleet:
    ip6: "fd00::1"
    subnet: "fd00::/64"
    external_ip: "1.2.3.4"
    port: 65535
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(config_content)
        config_path = f.name

    try:
        config = load_config(config_path)
        assert config.fleets["testfleet"].port == 65535
    finally:
        Path(config_path).unlink()

@pytest.mark.parametrize('fleet_name', ['thirteenchars', 'Upper', 'has_underscore', '../etc', '123:'])
def test_invalid_fleet_name(fleet_name):
    """Test fleet names that can't be used as interface names are rejected"""
    config_content = f"""
domain: test.internal
prune_timeout: 30m
fleets:
  "{fleet_name}":
    ip6: "fd00::1"
    subnet: "fd00::/64"
    external_ip: "1.2.3.4"
    port: 51820
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(config_content)
        config_path = f.name

    try:
        with pytest.raises(ValueError, match="Fleet name"):
            load_config(config_path)
    finally:
        Path(config_path).unlink()

@pytest.mark.parametrize('ip6, subnet, error', [
    ('fd00::1', 'fd00::1/64', 'invalid IPv6 subnet'),     # host bits set
    ('fd00::1', 'fd00::/120', 'must be /64 or larger'),   # too small for random allocation
    ('fd01::1', 'fd00::/64', 'not in subnet'),            # server address outside subnet
])
def test_invalid_subnet_layout(ip6, subnet, error):
    """Test subnets that would break allocation or ping checks are rejected"""
    config_content = f"""
domain: test.internal
prune_timeout: 30m
fleets:
  testfleet:
    ip6: "{ip6}"
    subnet: "{subnet}"
    external_ip: "1.2.3.4"
    port: 51820
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(config_content)
        config_path = f.name

    try:
        with pytest.raises(ValueError, match=error):
            load_config(config_path)
    finally:
        Path(config_path).unlink()

def test_invalid_external_ip():
    """Test a non-IP external_ip is rejected"""
    config_content = """
domain: test.internal
prune_timeout: 30m
fleets:
  testfleet:
    ip6: "fd00::1"
    subnet: "fd00::/64"
    external_ip: "not-an-ip"
    port: 51820
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(config_content)
        config_path = f.name

    try:
        with pytest.raises(ValueError, match="invalid external_ip"):
            load_config(config_path)
    finally:
        Path(config_path).unlink()

def write_config(tmp_path, content):
    path = tmp_path / 'config.yaml'
    path.write_text(content)
    return str(path)

URL_CONFIG = """
domain: test.internal
prune_timeout: 30m
fleets_url: https://bucket.example.org/v2/fleets.json
server_name: mgmt.example.org
external_ip: 203.0.113.5
config_signers:
  - contestforge ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample
"""

def test_fleets_url_without_static_fleets(tmp_path):
    """Test a config with only fleets_url is valid, and its settings default sensibly"""
    config = load_config(write_config(tmp_path, URL_CONFIG))

    assert config.fleets == {} and config.static_fleets == {}
    assert config.fleets_url == 'https://bucket.example.org/v2/fleets.json'
    assert config.config_signers == ['contestforge ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample']
    assert config.config_signer_id == 'contestforge'
    assert config.server_name == 'mgmt.example.org'
    assert config.refresh_interval == '1m'
    assert config.removed_fleet_grace == '24h'

def test_config_signers_as_block_string(tmp_path):
    """Test allowed_signers lines can be given as one block, as they're usually pasted"""
    config = load_config(write_config(tmp_path, URL_CONFIG.replace(
        "config_signers:\n  - contestforge ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample\n",
        "config_signers: |\n  contestforge ssh-ed25519 AAAA1\n  contestforge ssh-ed25519 AAAA2\n")))

    assert config.config_signers == ['contestforge ssh-ed25519 AAAA1', 'contestforge ssh-ed25519 AAAA2']

@pytest.mark.parametrize('key', ['server_name', 'external_ip', 'config_signers'])
def test_fleets_url_requires(key, tmp_path):
    """Test fleets_url can't be used without knowing whose fleets are ours and who signs them"""
    content = '\n'.join(line for line in URL_CONFIG.splitlines()
                        if not line.startswith(key) and not (key == 'config_signers' and line.startswith('  -')))
    with pytest.raises(ValueError, match=key):
        load_config(write_config(tmp_path, content))

def test_no_fleets_at_all(tmp_path):
    """Test a config with neither static fleets nor fleets_url is rejected"""
    with pytest.raises(ValueError, match="No fleets configured"):
        load_config(write_config(tmp_path, "domain: x\nprune_timeout: 30m\n"))

def test_static_fleet_uses_server_external_ip(tmp_path):
    """Test a static fleet without external_ip takes the server-level one"""
    config = load_config(write_config(tmp_path, URL_CONFIG + """
fleets:
  setup:
    ip6: "fd00::1"
    subnet: "fd00::/64"
    port: 51920
"""))
    assert config.fleets['setup'].external_ip == '203.0.113.5'
    assert config.fleets['setup'].interface == 'wg_setup'
    assert config.fleets['setup'].static is True

@pytest.mark.parametrize('line, error', [
    ('refresh_interval: 30s', 'refresh_interval'),
    ('removed_fleet_grace: 1d', 'removed_fleet_grace'),
    ('register_rate_limit: 0', 'register_rate_limit'),
    ('fleets_url: ftp://example.org/fleets.json', 'fleets_url'),
])
def test_invalid_server_settings(line, error, tmp_path):
    """Test bad values for the new server-level settings are rejected at load"""
    key = line.split(':')[0]
    content = '\n'.join(l for l in URL_CONFIG.splitlines() if not l.startswith(key + ':')) + '\n' + line + '\n'
    with pytest.raises(ValueError, match=error):
        load_config(write_config(tmp_path, content))
