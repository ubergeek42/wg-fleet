import pytest
from unittest.mock import patch, MagicMock
from wireguard import (
    generate_keypair,
    create_interface_config,
    add_peer,
    remove_peer,
    list_peers,
    get_server_public_key
)
from pathlib import Path
import tempfile
import os

@patch('wireguard.run_command')
def test_generate_keypair(mock_run_command):
    """Test WireGuard keypair generation"""
    mock_run_command.side_effect = [
        'private_key_abc123',  # wg genkey
        'public_key_xyz789'    # wg pubkey
    ]

    private, public = generate_keypair()
    assert private == 'private_key_abc123'
    assert public == 'public_key_xyz789'
    assert mock_run_command.call_count == 2

@patch('wireguard.run_command')
def test_add_peer(mock_run_command):
    """Test adding a peer to WireGuard interface"""
    add_peer('testfleet', 'pubkey123', 'fd00::100')

    mock_run_command.assert_called_once_with([
        'wg', 'set', 'wg_testfleet',
        'peer', 'pubkey123',
        'allowed-ips', 'fd00::100'
    ])

@patch('wireguard.run_command')
def test_remove_peer(mock_run_command):
    """Test removing a peer from WireGuard interface"""
    remove_peer('testfleet', 'pubkey123')

    mock_run_command.assert_called_once_with([
        'wg', 'set', 'wg_testfleet',
        'peer', 'pubkey123',
        'remove'
    ])

def test_create_interface_config():
    """Test creating WireGuard interface config file"""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / 'wg_test.conf'

        # create_interface_config expects a config object with subnet, ip6, and port attributes
        fleet_config = MagicMock()
        fleet_config.subnet = 'fd00::/64'
        fleet_config.ip6 = 'fd00::1'
        fleet_config.port = 51820

        create_interface_config(
            'test',
            fleet_config,
            'test_private_key',
            str(config_path)
        )

        assert config_path.exists()
        content = config_path.read_text()
        assert '[Interface]' in content
        assert 'Address = fd00::1/64' in content  # Should include prefix length from subnet
        assert 'ListenPort = 51820' in content
        assert 'PrivateKey = test_private_key' in content
        assert config_path.stat().st_mode & 0o777 == 0o600

@patch('wireguard.run_command')
def test_list_peers(mock_run_command):
    """Test listing peers from WireGuard"""
    # Mock output format: header line + peer data
    # wg show dump format: 8 tab-separated fields per peer:
    # public_key, preshared_key, endpoint, allowed_ips, last_handshake, rx_bytes, tx_bytes, persistent_keepalive
    mock_run_command.return_value = "private-key\tpublic-key\tlisten-port\tfwmark\npubkey1\t(none)\t192.168.1.100:51820\tfd00::100/128\t1697740800\t1024\t2048\t25"

    peers = list_peers('testfleet')
    assert len(peers) == 1
    assert peers[0]['public_key'] == 'pubkey1'
    assert peers[0]['allowed_ips'] == 'fd00::100/128'
    assert peers[0]['endpoint'] == '192.168.1.100:51820'
    assert peers[0]['rx_bytes'] == 1024
    assert peers[0]['tx_bytes'] == 2048

@pytest.mark.parametrize('endpoint_ip, expected', [
    ('1.2.3.4', 'Endpoint = 1.2.3.4:51820'),
    ('2001:db8::1', 'Endpoint = [2001:db8::1]:51820'),
])
def test_build_client_config_endpoint(endpoint_ip, expected):
    """Test client config endpoint formatting for IPv4 and IPv6 server addresses"""
    from wireguard import build_client_config
    config = build_client_config(
        client_private_key='client_priv',
        client_ip='fd00::1234',
        server_public_key='server_pub',
        endpoint_ip=endpoint_ip,
        endpoint_port=51820,
        server_ip='fd00::1',
        subnet='fd00::/64'
    )
    assert expected in config
    assert 'Address = fd00::1234/64' in config
    assert 'AllowedIPs = fd00::1/128' in config
