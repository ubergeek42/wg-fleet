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
