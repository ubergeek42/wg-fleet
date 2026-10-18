import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from routes import create_app
from config import FleetConfig
from models import Base, Client as DBClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import tempfile
import os

@pytest.fixture
def test_app():
    """Create test FastAPI app with test database"""
    fd, db_path = tempfile.mkstemp(suffix='.db')
    os.close(fd)

    engine = create_engine(f'sqlite:///{db_path}')
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    # Mock config
    config = MagicMock()
    config.domain = "test.internal"
    config.prune_timeout = "30m"
    config.fleets = {
        'testfleet': FleetConfig(
            ip6='fd00::1',
            subnet='fd00::/64',
            external_ip='1.2.3.4',
            port=51820,
            interface='wg_testfleet'
        )
    }

    app = create_app(config, session_factory, engine)
    test_client = TestClient(app)

    yield test_client, config, session_factory

    os.unlink(db_path)

@patch('routes.trigger_hooks')
@patch('routes.wireguard')
@patch('routes.allocate_random_ip')
def test_register_client(mock_allocate_ip, mock_wg, mock_trigger_hooks, test_app):
    """Test client registration endpoint"""
    test_client, config, session_factory = test_app

    # Mock functions
    mock_allocate_ip.return_value = 'fd00::1234'
    mock_wg.generate_keypair.return_value = ('client_priv', 'client_pub')
    mock_wg.get_server_public_key.return_value = 'server_pub'
    mock_wg.build_client_config.return_value = '[Interface]\nPrivateKey = client_priv'

    response = test_client.post('/fleet/testfleet/register')

    assert response.status_code == 200
    data = response.json()
    assert data['status'] == 'success'
    assert '[Interface]' in data['config']

    # Verify database record created
    with session_factory() as session:
        db_client = session.query(DBClient).first()
        assert db_client is not None
        assert db_client.fleet_id == 'testfleet'
        assert db_client.public_key == 'client_pub'
        assert db_client.assigned_ip == 'fd00::1234'

    # New clients have no hostname yet, so nothing the hooks export changes
    mock_trigger_hooks.assert_not_called()

def test_register_nonexistent_fleet(test_app):
    """Test registration with invalid fleet returns 404"""
    test_client, config, session_factory = test_app

    response = test_client.post('/fleet/nonexistent/register')
    assert response.status_code == 404

@patch('routes.wireguard')
@patch('routes.allocate_random_ip')
def test_register_removes_peer_when_db_save_fails(mock_allocate_ip, mock_wg, test_app):
    """Test a failed registration doesn't leave an orphaned WireGuard peer"""
    test_client, config, session_factory = test_app

    mock_allocate_ip.return_value = 'fd00::1234'
    mock_wg.generate_keypair.return_value = ('client_priv', 'client_pub')
    mock_wg.get_server_public_key.return_value = 'server_pub'
    mock_wg.build_client_config.return_value = '[Interface]'

    with patch('sqlalchemy.orm.Session.commit', side_effect=RuntimeError('db locked')):
        response = test_client.post('/fleet/testfleet/register')

    assert response.status_code == 500
    mock_wg.add_peer.assert_called_once_with('wg_testfleet', 'client_pub', 'fd00::1234')
    mock_wg.remove_peer.assert_called_once_with('wg_testfleet', 'client_pub')
    with session_factory() as session:
        assert session.query(DBClient).count() == 0

def add_client(session_factory, public_key, ip, hostname=None):
    """Insert a client record directly"""
    from datetime import datetime, UTC
    with session_factory() as session:
        session.add(DBClient(fleet_id="testfleet", public_key=public_key, assigned_ip=ip,
                             http_request_ip="1.2.3.4", hostname=hostname,
                             timestamp=datetime(2020, 1, 1)))
        session.commit()

def ping(test_client, ip, hostname=None, fleet='testfleet'):
    """POST to the ping endpoint as if from the given source IP"""
    client = TestClient(test_client.app, client=(ip, 12345))
    body = {'hostname': hostname} if hostname is not None else {}
    return client.post(f'/fleet/{fleet}/ping', json=body)

def hostname_of(session_factory, ip):
    with session_factory() as session:
        return session.query(DBClient).filter_by(assigned_ip=ip).one().hostname

@patch('routes.trigger_hooks')
def test_ping_client_without_hostname(mock_trigger_hooks, test_app):
    """Test ping without hostname updates timestamp"""
    from datetime import datetime
    test_client, config, session_factory = test_app
    add_client(session_factory, "test_key", "fd00::100")

    response = ping(test_client, "fd00::100")
    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}

    with session_factory() as session:
        updated = session.query(DBClient).filter_by(assigned_ip="fd00::100").one()
        assert updated.timestamp > datetime(2020, 1, 1)
        assert updated.hostname is None
    mock_trigger_hooks.assert_not_called()

@patch('routes.trigger_hooks')
def test_ping_with_hostname(mock_trigger_hooks, test_app):
    """Test ping with hostname assignment"""
    from hook_manager import EventType
    test_client, config, session_factory = test_app
    add_client(session_factory, "test_key", "fd00::100")

    assert ping(test_client, "fd00::100", 'testhost').status_code == 200
    assert hostname_of(session_factory, "fd00::100") == 'testhost'

    mock_trigger_hooks.assert_called_once()
    assert mock_trigger_hooks.call_args[0][0].event_type == EventType.CLIENT_HOSTNAME_CHANGED

def test_ping_from_outside_fleet_subnet(test_app):
    """Test pings from outside the fleet subnet are rejected"""
    test_client, config, session_factory = test_app
    assert ping(test_client, "fd01::100").status_code == 403
    assert ping(test_client, "1.2.3.4").status_code == 403

def test_ping_unregistered_client(test_app):
    """Test pings from an unknown address in the subnet return 404"""
    test_client, config, session_factory = test_app
    assert ping(test_client, "fd00::999").status_code == 404

def test_ping_nonexistent_fleet(test_app):
    """Test pings to an unknown fleet return 404"""
    test_client, config, session_factory = test_app
    assert ping(test_client, "fd00::100", fleet='nope').status_code == 404

@patch('routes.trigger_hooks')
def test_ping_hostname_deduplication(mock_trigger_hooks, test_app):
    """Test duplicate hostname gets numbered"""
    from hook_manager import EventType
    test_client, config, session_factory = test_app
    add_client(session_factory, "key1", "fd00::100", hostname="myhost")
    add_client(session_factory, "key2", "fd00::101")

    assert ping(test_client, "fd00::101", 'myhost').status_code == 200
    assert hostname_of(session_factory, "fd00::101") == 'myhost--2'

    mock_trigger_hooks.assert_called_once()
    assert mock_trigger_hooks.call_args[0][0].event_type == EventType.CLIENT_HOSTNAME_CHANGED

@patch('routes.trigger_hooks')
def test_ping_deduplicated_hostname_is_stable(mock_trigger_hooks, test_app):
    """Test repeated pings keep a deduplicated hostname instead of renaming it"""
    test_client, config, session_factory = test_app
    add_client(session_factory, "key1", "fd00::100", hostname="myhost")
    add_client(session_factory, "key2", "fd00::101")

    for _ in range(3):
        assert ping(test_client, "fd00::101", 'myhost').status_code == 200
        assert hostname_of(session_factory, "fd00::101") == 'myhost--2'

    # Only the first ping changed the hostname
    mock_trigger_hooks.assert_called_once()

@patch('routes.trigger_hooks')
def test_ping_reclaims_plain_hostname_once_free(mock_trigger_hooks, test_app):
    """Test a deduplicated client takes the plain hostname after its holder is gone"""
    test_client, config, session_factory = test_app
    # Stale record of a re-registered machine still holds "myhost"
    add_client(session_factory, "old", "fd00::100", hostname="myhost")
    add_client(session_factory, "new", "fd00::101", hostname="myhost--2")

    # While the stale record exists, keep the deduplicated name
    ping(test_client, "fd00::101", 'myhost')
    assert hostname_of(session_factory, "fd00::101") == 'myhost--2'
    mock_trigger_hooks.assert_not_called()

    # Stale record pruned: take over the plain name, once
    with session_factory() as session:
        session.query(DBClient).filter_by(public_key="old").delete()
        session.commit()
    for _ in range(2):
        ping(test_client, "fd00::101", 'myhost')
        assert hostname_of(session_factory, "fd00::101") == 'myhost'
    mock_trigger_hooks.assert_called_once()

@pytest.mark.parametrize('hostname', ['bad\n', 'UPPER', 'has.dot', 'a' * 60, '-oProxyCommand', 'trailing-'])
def test_ping_rejects_invalid_hostname(hostname, test_app):
    """Test hostnames with invalid characters or length are rejected"""
    test_client, config, session_factory = test_app
    add_client(session_factory, "key1", "fd00::100")

    assert ping(test_client, "fd00::100", hostname).status_code == 400
    assert hostname_of(session_factory, "fd00::100") is None

@pytest.mark.parametrize('hostname', ['a', 'web-01', 'lab_pc3', 'a' * 59])
@patch('routes.trigger_hooks')
def test_ping_accepts_valid_hostname(mock_trigger_hooks, hostname, test_app):
    """Test valid hostnames, including the maximum length, are accepted"""
    test_client, config, session_factory = test_app
    add_client(session_factory, "key1", "fd00::100")

    assert ping(test_client, "fd00::100", hostname).status_code == 200
    assert hostname_of(session_factory, "fd00::100") == hostname
