import pytest
from unittest.mock import patch, MagicMock
from pruning import prune_stale_clients_once
from models import Client, Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from datetime import datetime, timedelta, UTC
import tempfile
import os

@pytest.fixture
def test_db_with_stale_clients():
    """Create test database with stale and active clients"""
    fd, db_path = tempfile.mkstemp(suffix='.db')
    os.close(fd)

    engine = create_engine(f'sqlite:///{db_path}')
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    # Add test clients
    with session_factory() as session:
        # Stale client (old handshake)
        stale = Client(
            fleet_id="testfleet",
            public_key="stale_key",
            assigned_ip="fd00::100",
            http_request_ip="1.2.3.4",
            hostname="stale",
            timestamp=datetime.now(UTC)
        )
        # Active client (recent handshake)
        active = Client(
            fleet_id="testfleet",
            public_key="active_key",
            assigned_ip="fd00::101",
            http_request_ip="1.2.3.5",
            hostname="active",
            timestamp=datetime.now(UTC)
        )
        session.add_all([stale, active])
        session.commit()

    yield session_factory, db_path

    os.unlink(db_path)

@patch('pruning.wireguard')
@patch('pruning.hosts')
def test_prune_stale_clients(mock_hosts, mock_wg, test_db_with_stale_clients):
    """Test pruning removes stale clients"""
    session_factory, db_path = test_db_with_stale_clients

    # Mock WireGuard peer list
    old_handshake = datetime.now(UTC) - timedelta(hours=2)
    recent_handshake = datetime.now(UTC) - timedelta(minutes=5)

    mock_wg.list_peers.return_value = [
        {'public_key': 'stale_key', 'last_handshake': old_handshake},
        {'public_key': 'active_key', 'last_handshake': recent_handshake}
    ]

    # Mock config
    config = MagicMock()
    config.prune_timeout = "1h"
    config.fleets = {'testfleet': MagicMock()}

    # Run pruning
    pruned_count = prune_stale_clients_once(config, session_factory)

    assert pruned_count == 1

    # Verify stale client removed from WireGuard
    mock_wg.remove_peer.assert_called_once_with('testfleet', 'stale_key')

    # Verify stale client removed from database
    with session_factory() as session:
        remaining = session.query(Client).all()
        assert len(remaining) == 1
        assert remaining[0].public_key == 'active_key'

    # Verify hosts file regenerated
    mock_hosts.regenerate_hosts_file.assert_called_once()

@patch('pruning.wireguard')
@patch('pruning.hosts')
def test_prune_never_connected_stale_clients(mock_hosts, mock_wg, test_db_with_stale_clients):
    """Test pruning removes clients that never connected but are stale"""
    session_factory, db_path = test_db_with_stale_clients

    # Add a client that registered 2 hours ago but never connected
    with session_factory() as session:
        never_connected = Client(
            fleet_id="testfleet",
            public_key="never_connected_key",
            assigned_ip="fd00::102",
            http_request_ip="1.2.3.6",
            hostname="never_connected",
            timestamp=datetime.now(UTC) - timedelta(hours=2)
        )
        session.add(never_connected)
        session.commit()

    # Mock WireGuard peer list - never_connected has no handshake
    recent_handshake = datetime.now(UTC) - timedelta(minutes=5)

    mock_wg.list_peers.return_value = [
        {'public_key': 'active_key', 'last_handshake': recent_handshake},
        {'public_key': 'never_connected_key', 'last_handshake': None}
    ]

    # Mock config with 1 hour timeout
    config = MagicMock()
    config.prune_timeout = "1h"
    config.fleets = {'testfleet': MagicMock()}

    # Run pruning
    pruned_count = prune_stale_clients_once(config, session_factory)

    # Should prune the never-connected client (2 hours old > 1 hour timeout)
    assert pruned_count == 1

    # Verify never-connected client removed from WireGuard
    mock_wg.remove_peer.assert_called_once_with('testfleet', 'never_connected_key')

    # Verify only active clients remain in database
    with session_factory() as session:
        remaining = session.query(Client).all()
        assert len(remaining) == 2  # stale_key and active_key remain
        remaining_keys = {c.public_key for c in remaining}
        assert 'active_key' in remaining_keys
        assert 'stale_key' in remaining_keys
        assert 'never_connected_key' not in remaining_keys

    # Verify hosts file regenerated
    mock_hosts.regenerate_hosts_file.assert_called_once()
