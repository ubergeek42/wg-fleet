import pytest
from fastapi.testclient import TestClient
from routes import create_app
from models import Base, Client
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import patch, MagicMock
import tempfile
import os

@pytest.fixture
def integration_app():
    """Create full application for integration testing"""
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
    client = TestClient(app)

    yield client, config, session_factory

    os.unlink(db_path)

@patch('routes.hosts')
@patch('routes.wireguard')
@patch('routes.allocate_random_ip')
def test_full_client_lifecycle(mock_allocate_ip, mock_wg, mock_hosts, integration_app):
    """Test full client lifecycle: register -> ping -> verify dashboard"""
    client, config, session_factory = integration_app

    # Mock WireGuard operations
    mock_allocate_ip.return_value = 'fd00::1234'
    mock_wg.generate_keypair.return_value = ('client_priv', 'client_pub')
    mock_wg.get_server_public_key.return_value = 'server_pub'
    mock_wg.build_client_config.return_value = '[Interface]\nPrivateKey = client_priv'
    mock_wg.list_peers.return_value = []  # Mock empty WireGuard peer list for dashboard

    # 1. Register client
    response = client.post('/fleet/testfleet/register')
    assert response.status_code == 200
    data = response.json()
    assert data['status'] == 'success'

    # 2. Ping from client
    response = client.post(
        '/fleet/testfleet/ping',
        json={'hostname': 'testmachine'},
        headers={'X-Forwarded-For': 'fd00::1234'}
    )
    assert response.status_code == 200

    # Verify hosts file regeneration was called
    mock_hosts.regenerate_hosts_file.assert_called_once()

    # 3. Check dashboard shows client
    response = client.get('/fleet/testfleet')
    assert response.status_code == 200
    assert 'testmachine' in response.text
    assert 'fd00::1234' in response.text
