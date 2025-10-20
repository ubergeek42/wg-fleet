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
