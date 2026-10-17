import pytest
from unittest.mock import patch, MagicMock, call
from main import setup_fleet_interface, reconcile_fleet_state
from models import Client, Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from datetime import datetime, UTC
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
            timestamp=datetime.now(UTC)
        )
        # Client in both DB and WG
        valid = Client(
            fleet_id="testfleet",
            public_key="valid_key",
            assigned_ip="fd00::101",
            http_request_ip="1.2.3.5",
            timestamp=datetime.now(UTC)
        )
        session.add_all([orphan, valid])
        session.commit()

    # Mock WireGuard peers (includes valid + extra peer not in DB)
    mock_wg.list_peers.return_value = [
        {'public_key': 'valid_key', 'last_handshake': datetime.now(UTC)},
        {'public_key': 'extra_key', 'last_handshake': datetime.now(UTC)}
    ]

    reconcile_fleet_state('testfleet', session_factory)

    # Verify orphan removed from DB
    with session_factory() as session:
        clients = session.query(Client).all()
        assert len(clients) == 1
        assert clients[0].public_key == 'valid_key'

    # Verify extra peer removed from WireGuard
    mock_wg.remove_peer.assert_called_once_with('testfleet', 'extra_key')

def test_main_initializes_once_and_runs_pruning(test_db, tmp_path):
    """Test main() initializes fleets once and the app lifespan runs the pruner"""
    from unittest.mock import AsyncMock
    from fastapi.testclient import TestClient
    import main

    session_factory, engine, db_path = test_db
    config_path = tmp_path / 'config.yaml'
    config_path.write_text("""
domain: test.internal
prune_timeout: 30m
fleets:
  testfleet:
    ip6: fd00::1
    subnet: fd00::/64
    external_ip: 1.2.3.4
    port: 51820
""")

    with patch('sys.argv', ['main.py', '--config', str(config_path)]), \
         patch('main.init_db', return_value=engine) as mock_init_db, \
         patch('main.uvicorn') as mock_uvicorn, \
         patch('main.initialize_fleets') as mock_initialize, \
         patch('main.prune_stale_clients', new_callable=AsyncMock) as mock_prune:
        main.main()

        mock_init_db.assert_called_once()
        mock_initialize.assert_called_once()

        app = mock_uvicorn.run.call_args[0][0]
        with TestClient(app) as client:
            assert client.get('/').status_code == 200
        mock_prune.assert_awaited_once()
        assert mock_prune.call_args[0][1] is mock_initialize.call_args[0][1]

def test_main_exits_on_startup_failure(tmp_path):
    """Test main() exits non-zero when startup fails"""
    import main

    with patch('sys.argv', ['main.py', '--config', str(tmp_path / 'missing.yaml')]), \
         patch('main.uvicorn') as mock_uvicorn:
        with pytest.raises(SystemExit) as exc_info:
            main.main()

    assert exc_info.value.code == 1
    mock_uvicorn.run.assert_not_called()
