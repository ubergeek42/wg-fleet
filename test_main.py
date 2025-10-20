import pytest
from unittest.mock import patch, MagicMock, call
from main import setup_fleet_interface, reconcile_fleet_state
from models import Client, Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import tempfile
import os

@pytest.fixture
def test_db():
    """Create test database"""
    fd, db_path = tempfile.mkstemp(suffix='.db')
    os.close(fd)

    engine = create_engine(f'sqlite:///{db_path}')
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    yield session_factory, engine, db_path

    os.unlink(db_path)

@patch('main.wireguard')
@patch('main.Path')
def test_setup_fleet_interface_new(mock_path, mock_wg, test_db):
    """Test setting up a new fleet interface"""
    session_factory, engine, db_path = test_db

    # Mock config file doesn't exist
    mock_path.return_value.exists.return_value = False
    mock_wg.generate_keypair.return_value = ('priv_key', 'pub_key')

    fleet_config = MagicMock()
    fleet_config.ip6 = 'fd00::1'
    fleet_config.port = 51820

    setup_fleet_interface('testfleet', fleet_config)

    # Verify keypair generated
    mock_wg.generate_keypair.assert_called_once()

    # Verify config created
    mock_wg.create_interface_config.assert_called_once_with(
        'testfleet', fleet_config, 'priv_key'
    )

    # Verify interface brought up
    mock_wg.bring_up_interface.assert_called_once_with('testfleet')

@patch('main.wireguard')
def test_reconcile_fleet_state(mock_wg, test_db):
    """Test reconciliation removes mismatched entries"""
    session_factory, engine, db_path = test_db

    # Add test data to database
    with session_factory() as session:
        # Client in DB but not in WG
        orphan = Client(
            fleet_id="testfleet",
            public_key="orphan_key",
            assigned_ip="fd00::100",
            http_request_ip="1.2.3.4",
            timestamp=datetime.utcnow()
        )
        # Client in both DB and WG
        valid = Client(
            fleet_id="testfleet",
            public_key="valid_key",
            assigned_ip="fd00::101",
            http_request_ip="1.2.3.5",
            timestamp=datetime.utcnow()
        )
        session.add_all([orphan, valid])
        session.commit()

    # Mock WireGuard peers (includes valid + extra peer not in DB)
    mock_wg.list_peers.return_value = [
        {'public_key': 'valid_key', 'last_handshake': datetime.utcnow()},
        {'public_key': 'extra_key', 'last_handshake': datetime.utcnow()}
    ]

    reconcile_fleet_state('testfleet', session_factory)

    # Verify orphan removed from DB
    with session_factory() as session:
        clients = session.query(Client).all()
        assert len(clients) == 1
        assert clients[0].public_key == 'valid_key'

    # Verify extra peer removed from WireGuard
    mock_wg.remove_peer.assert_called_once_with('testfleet', 'extra_key')
