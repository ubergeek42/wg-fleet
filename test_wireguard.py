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

        create_interface_config(
            'test',
            {'ip6': 'fd00::1', 'port': 51820},
            'test_private_key',
            str(config_path)
        )

        assert config_path.exists()
        content = config_path.read_text()
        assert '[Interface]' in content
        assert 'Address = fd00::1' in content
        assert 'ListenPort = 51820' in content
        assert 'PrivateKey = test_private_key' in content

@patch('wireguard.run_command')
def test_list_peers(mock_run_command):
    """Test listing peers from WireGuard"""
    # Mock output format: header line + peer data
    # wg show dump format: pubkey preshared-key endpoint allowed-ips last-handshake rx tx persistent-keepalive
    mock_run_command.return_value = "private-key\tpublic-key\tlisten-port\tfwmark\npubkey1\tfd00::100/128\t\t1697740800\t1024\t2048\t0"

    peers = list_peers('testfleet')
    assert len(peers) == 1
    assert peers[0]['public_key'] == 'pubkey1'
    assert peers[0]['allowed_ips'] == 'fd00::100/128'
