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
