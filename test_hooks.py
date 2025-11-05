import pytest
from pathlib import Path
from hook_manager import HookContext, EventType
from hooks.hosts_file import regenerate_hosts_file_hook
from models import Client, Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from datetime import datetime, UTC
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
                timestamp=datetime.now(UTC)
            ),
            Client(
                fleet_id="testfleet",
                public_key="key2",
                assigned_ip="fd00::101",
                http_request_ip="192.168.1.2",
                hostname="host2",
                timestamp=datetime.now(UTC)
            ),
            Client(
                fleet_id="otherfleet",
                public_key="key3",
                assigned_ip="fd01::200",
                http_request_ip="192.168.1.3",
                hostname=None,  # No hostname
                timestamp=datetime.now(UTC)
            )
        ]
        session.add_all(clients)
        session.commit()

    yield {'session_factory': session_factory, 'db_path': db_path}

    os.unlink(db_path)


@pytest.fixture
def app_config():
    """Create a mock app config object"""
    return type('Config', (), {'domain': 'test.internal'})()


def test_hosts_file_hook_generates_entries(test_db_with_clients, app_config, tmp_path):
    """Test that hosts file hook generates correct entries"""
    # Override hosts file path for testing
    import hooks.hosts_file
    original_path = hooks.hosts_file.HOSTS_FILE_PATH
    test_hosts_path = tmp_path / "hosts"
    hooks.hosts_file.HOSTS_FILE_PATH = str(test_hosts_path)

    try:
        context = HookContext(
            event_type=EventType.CLIENT_HOSTNAME_CHANGED,
            config=app_config,
            session_factory=test_db_with_clients['session_factory']
        )

        regenerate_hosts_file_hook(context)

        # Verify hosts file was created
        assert test_hosts_path.exists()

        # Verify content
        content = test_hosts_path.read_text()
        lines = content.strip().split('\n')

        # Should have 2 entries (third client has no hostname)
        assert len(lines) == 2

        # Verify format: <ip> <hostname>.<fleet>.<domain>
        assert 'fd00::100 host1.testfleet.test.internal' in content
        assert 'fd00::101 host2.testfleet.test.internal' in content
        assert 'fd01::200' not in content  # No hostname = not in file

    finally:
        hooks.hosts_file.HOSTS_FILE_PATH = original_path


def test_hosts_file_hook_filters_events(tmp_path):
    """Test that hook only runs on relevant events"""
    # Hook should do nothing for STARTUP events
    context = HookContext(
        event_type=EventType.STARTUP,
        config={},
        session_factory=lambda: None
    )

    # Should not raise any errors or try to generate hosts
    regenerate_hosts_file_hook(context)
