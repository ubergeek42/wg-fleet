import pytest
from hosts import regenerate_hosts_file
from models import Client, Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from pathlib import Path
from datetime import datetime
import tempfile
import os

@pytest.fixture
def test_db_with_clients():
    """Create test database with sample clients"""
    fd, db_path = tempfile.mkstemp(suffix='.db')
    os.close(fd)

    engine = create_engine(f'sqlite:///{db_path}')
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    # Add test clients
    with session_factory() as session:
        clients = [
            Client(
                fleet_id="testfleet",
                public_key="key1",
                assigned_ip="fd00::100",
                http_request_ip="192.168.1.1",
                hostname="host1",
                timestamp=datetime.utcnow()
            ),
            Client(
                fleet_id="testfleet",
                public_key="key2",
                assigned_ip="fd00::101",
                http_request_ip="192.168.1.2",
                hostname="host2",
                timestamp=datetime.utcnow()
            ),
            Client(
                fleet_id="otherfleet",
                public_key="key3",
                assigned_ip="fd01::200",
                http_request_ip="192.168.1.3",
                hostname=None,  # No hostname
                timestamp=datetime.utcnow()
            )
        ]
        session.add_all(clients)
        session.commit()

    yield session_factory, db_path

    os.unlink(db_path)

def test_regenerate_hosts_file(test_db_with_clients):
    """Test hosts file generation from database"""
    session_factory, db_path = test_db_with_clients

    with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
        hosts_path = f.name

    try:
        config = type('Config', (), {'domain': 'test.internal'})()
        regenerate_hosts_file(config, session_factory, hosts_path)

        content = Path(hosts_path).read_text()
        lines = content.strip().split('\n')

        # Should have 2 entries (third client has no hostname)
        assert len(lines) == 2
        assert 'fd00::100 host1.testfleet.test.internal' in content
        assert 'fd00::101 host2.testfleet.test.internal' in content
        assert 'fd01::200' not in content  # No hostname = not in file
    finally:
        os.unlink(hosts_path)
