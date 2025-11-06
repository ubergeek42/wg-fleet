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
import json


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


def test_hosts_file_hook_handles_startup(test_db_with_clients, app_config, tmp_path):
    """Test that hook runs on STARTUP events"""
    # Override hosts file path for testing
    import hooks.hosts_file
    original_path = hooks.hosts_file.HOSTS_FILE_PATH
    test_hosts_path = tmp_path / "hosts"
    hooks.hosts_file.HOSTS_FILE_PATH = str(test_hosts_path)

    try:
        context = HookContext(
            event_type=EventType.STARTUP,
            config=app_config,
            session_factory=test_db_with_clients['session_factory']
        )

        regenerate_hosts_file_hook(context)

        # Verify hosts file was created on startup
        assert test_hosts_path.exists()

        # Verify content
        content = test_hosts_path.read_text()
        assert 'host1.testfleet.test.internal' in content
        assert 'host2.testfleet.test.internal' in content

    finally:
        hooks.hosts_file.HOSTS_FILE_PATH = original_path


def test_prometheus_sd_hook_generates_targets(test_db_with_clients, app_config, tmp_path):
    """Test that prometheus_sd hook generates correct service discovery targets"""
    from hooks.prometheus_sd import prometheus_sd_hook

    # Override prometheus targets path for testing
    import hooks.prometheus_sd
    original_path = hooks.prometheus_sd.PROMETHEUS_TARGETS_PATH
    test_targets_path = tmp_path / "prometheus_targets.json"
    hooks.prometheus_sd.PROMETHEUS_TARGETS_PATH = str(test_targets_path)

    try:
        context = HookContext(
            event_type=EventType.CLIENT_HOSTNAME_CHANGED,
            config=app_config,
            session_factory=test_db_with_clients['session_factory']
        )

        prometheus_sd_hook(context)

        # Verify targets file was created
        assert test_targets_path.exists()

        # Verify content
        with open(test_targets_path) as f:
            targets = json.load(f)

        # Should have 2 targets (third client has no hostname)
        assert len(targets) == 2

        # Verify format
        target_ips = [t['targets'][0] for t in targets]
        assert '[fd00::100]:9100' in target_ips
        assert '[fd00::101]:9100' in target_ips

        # Verify labels
        for target in targets:
            assert 'labels' in target
            assert 'hostname' in target['labels']
            assert 'fleet' in target['labels']
            assert target['labels']['job'] == 'node_exporter'

        # Find specific target and check labels
        host1_target = next(t for t in targets if t['labels']['hostname'] == 'host1')
        assert host1_target['labels']['fleet'] == 'testfleet'

    finally:
        hooks.prometheus_sd.PROMETHEUS_TARGETS_PATH = original_path


def test_prometheus_sd_hook_filters_events():
    """Test that prometheus_sd hook only runs on relevant events"""
    from hooks.prometheus_sd import prometheus_sd_hook

    # Hook should do nothing for irrelevant events - just ensure it doesn't crash
    context = HookContext(
        event_type=EventType.STARTUP,  # STARTUP is not in the filter list
        config=type('Config', (), {})(),
        session_factory=lambda: None
    )

    # Should not raise any errors
    prometheus_sd_hook(context)
