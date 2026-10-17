import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from routes import create_app
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
        'testfleet': MagicMock(
            ip6='fd00::1',
            subnet='fd00::/64',
            external_ip='1.2.3.4',
            port=51820
        )
    }

    app = create_app(config, session_factory, engine)
    test_client = TestClient(app)

    yield test_client, config, session_factory

    os.unlink(db_path)

@patch('routes.wireguard')
@patch('routes.allocate_random_ip')
def test_register_client(mock_allocate_ip, mock_wg, test_app):
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

def test_register_nonexistent_fleet(test_app):
    """Test registration with invalid fleet returns 404"""
    test_client, config, session_factory = test_app

    response = test_client.post('/fleet/nonexistent/register')
    assert response.status_code == 404

@patch('routes.trigger_hooks')
def test_ping_client_without_hostname(mock_trigger_hooks, test_app):
    """Test ping without hostname updates timestamp"""
    from datetime import datetime, UTC
    import asyncio
    from routes import ping_client, PingRequest
    test_client, config, session_factory = test_app

    # Pre-create a client
    with session_factory() as session:
        existing = DBClient(
            fleet_id="testfleet",
            public_key="test_key",
            assigned_ip="fd00::100",
            http_request_ip="1.2.3.4",
            hostname=None,
            timestamp=datetime(2020, 1, 1)
        )
        session.add(existing)
        session.commit()
        assigned_ip = existing.assigned_ip

    # Create mock request with correct client IP
    mock_request = MagicMock()
    mock_request.client.host = assigned_ip
    mock_request.headers.get.return_value = None  # No X-Forwarded-For header

    with session_factory() as db:
        result = asyncio.run(ping_client(
            fleet_name="testfleet",
            ping_req=PingRequest(hostname=None),
            request=mock_request,
            db=db
        ))

        assert result.status == 'ok'

    # Verify timestamp updated
    with session_factory() as session:
        updated = session.query(DBClient).filter_by(assigned_ip=assigned_ip).first()
        assert updated.timestamp > datetime(2020, 1, 1)

@patch('routes.trigger_hooks')
def test_ping_with_hostname(mock_trigger_hooks, test_app):
    """Test ping with hostname assignment"""
    from datetime import datetime, UTC
    import asyncio
    from routes import ping_client, PingRequest
    from hook_manager import EventType
    test_client, config, session_factory = test_app

    # Pre-create a client
    with session_factory() as session:
        existing = DBClient(
            fleet_id="testfleet",
            public_key="test_key",
            assigned_ip="fd00::100",
            http_request_ip="1.2.3.4",
            hostname=None,
            timestamp=datetime.now(UTC)
        )
        session.add(existing)
        session.commit()
        assigned_ip = existing.assigned_ip

    # Create mock request with correct client IP
    mock_request = MagicMock()
    mock_request.client.host = assigned_ip
    mock_request.headers.get.return_value = None  # No X-Forwarded-For header

    with session_factory() as db:
        result = asyncio.run(ping_client(
            fleet_name="testfleet",
            ping_req=PingRequest(hostname='testhost'),
            request=mock_request,
            db=db
        ))

        assert result.status == 'ok'

    # Verify hostname set
    with session_factory() as session:
        updated = session.query(DBClient).filter_by(assigned_ip=assigned_ip).first()
        assert updated.hostname == 'testhost'

    # Verify hooks triggered
    mock_trigger_hooks.assert_called_once()
    assert mock_trigger_hooks.call_args[0][0] == EventType.CLIENT_HOSTNAME_CHANGED

@patch('routes.trigger_hooks')
def test_ping_hostname_deduplication(mock_trigger_hooks, test_app):
    """Test duplicate hostname gets numbered"""
    from datetime import datetime, UTC
    import asyncio
    from routes import ping_client, PingRequest
    from hook_manager import EventType
    test_client, config, session_factory = test_app

    # Pre-create two clients
    with session_factory() as session:
        client1 = DBClient(
            fleet_id="testfleet",
            public_key="key1",
            assigned_ip="fd00::100",
            http_request_ip="1.2.3.4",
            hostname="myhost",
            timestamp=datetime.now(UTC)
        )
        client2 = DBClient(
            fleet_id="testfleet",
            public_key="key2",
            assigned_ip="fd00::101",
            http_request_ip="1.2.3.5",
            hostname=None,
            timestamp=datetime.now(UTC)
        )
        session.add_all([client1, client2])
        session.commit()

    # Create mock request with correct client IP
    mock_request = MagicMock()
    mock_request.client.host = "fd00::101"
    mock_request.headers.get.return_value = None  # No X-Forwarded-For header

    with session_factory() as db:
        result = asyncio.run(ping_client(
            fleet_name="testfleet",
            ping_req=PingRequest(hostname='myhost'),
            request=mock_request,
            db=db
        ))

        assert result.status == 'ok'

    # Verify hostname got numbered
    with session_factory() as session:
        updated = session.query(DBClient).filter_by(assigned_ip="fd00::101").first()
        assert updated.hostname == 'myhost--2'

    # Verify hooks triggered
    mock_trigger_hooks.assert_called_once()
    assert mock_trigger_hooks.call_args[0][0] == EventType.CLIENT_HOSTNAME_CHANGED

@patch('routes.trigger_hooks')
def test_ping_deduplicated_hostname_is_stable(mock_trigger_hooks, test_app):
    """Test repeated pings keep a deduplicated hostname instead of renaming it"""
    from datetime import datetime, UTC
    import asyncio
    from routes import ping_client, PingRequest
    test_client, config, session_factory = test_app

    with session_factory() as session:
        session.add_all([
            DBClient(fleet_id="testfleet", public_key="key1", assigned_ip="fd00::100",
                     http_request_ip="1.2.3.4", hostname="myhost", timestamp=datetime.now(UTC)),
            DBClient(fleet_id="testfleet", public_key="key2", assigned_ip="fd00::101",
                     http_request_ip="1.2.3.5", hostname=None, timestamp=datetime.now(UTC)),
        ])
        session.commit()

    mock_request = MagicMock()
    mock_request.client.host = "fd00::101"

    for _ in range(3):
        with session_factory() as db:
            asyncio.run(ping_client(
                fleet_name="testfleet",
                ping_req=PingRequest(hostname='myhost'),
                request=mock_request,
                db=db
            ))
        with session_factory() as session:
            updated = session.query(DBClient).filter_by(assigned_ip="fd00::101").first()
            assert updated.hostname == 'myhost--2'

    # Only the first ping changed the hostname
    mock_trigger_hooks.assert_called_once()

@pytest.mark.parametrize('hostname', ['bad\n', 'UPPER', 'has.dot', 'a' * 64])
def test_ping_rejects_invalid_hostname(hostname, test_app):
    """Test hostnames with invalid characters or length are rejected"""
    from datetime import datetime, UTC
    import asyncio
    from fastapi import HTTPException
    from routes import ping_client, PingRequest
    test_client, config, session_factory = test_app

    with session_factory() as session:
        session.add(DBClient(fleet_id="testfleet", public_key="key1", assigned_ip="fd00::100",
                             http_request_ip="1.2.3.4", hostname=None, timestamp=datetime.now(UTC)))
        session.commit()

    mock_request = MagicMock()
    mock_request.client.host = "fd00::100"

    with session_factory() as db:
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(ping_client(
                fleet_name="testfleet",
                ping_req=PingRequest(hostname=hostname),
                request=mock_request,
                db=db
            ))
    assert exc_info.value.status_code == 400

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
    mock_wg.add_peer.assert_called_once_with('testfleet', 'client_pub', 'fd00::1234')
    mock_wg.remove_peer.assert_called_once_with('testfleet', 'client_pub')
    with session_factory() as session:
        assert session.query(DBClient).count() == 0
