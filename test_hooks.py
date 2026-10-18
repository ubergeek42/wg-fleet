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
import yaml


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
    return type('Config', (), {
        'domain': 'test.internal',
        'fleets': {'testfleet': None, 'otherfleet': None, 'emptyfleet': None},
    })()


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


def test_prometheus_sd_hook_on_startup(test_db_with_clients, app_config, tmp_path):
    """Test that prometheus_sd hook runs on STARTUP events"""
    from hooks.prometheus_sd import prometheus_sd_hook

    # Override prometheus targets path for testing
    import hooks.prometheus_sd
    original_path = hooks.prometheus_sd.PROMETHEUS_TARGETS_PATH
    test_targets_path = tmp_path / "prometheus_targets.json"
    hooks.prometheus_sd.PROMETHEUS_TARGETS_PATH = str(test_targets_path)

    try:
        context = HookContext(
            event_type=EventType.STARTUP,
            config=app_config,
            session_factory=test_db_with_clients['session_factory']
        )

        # Should not raise any errors
        prometheus_sd_hook(context)

        # Verify it generated the file
        assert test_targets_path.exists()

    finally:
        hooks.prometheus_sd.PROMETHEUS_TARGETS_PATH = original_path


def test_ansible_inventory_hook_generates_yaml_inventory(test_db_with_clients, app_config, tmp_path):
    """Test that ansible_inventory hook generates correct YAML inventory file"""
    from hooks.ansible_inventory import ansible_inventory_hook

    # Override inventory path for testing
    import hooks.ansible_inventory
    original_path = hooks.ansible_inventory.ANSIBLE_INVENTORY_PATH
    test_inventory_path = tmp_path / "ansible_inventory.yml"
    hooks.ansible_inventory.ANSIBLE_INVENTORY_PATH = str(test_inventory_path)

    try:
        context = HookContext(
            event_type=EventType.CLIENT_HOSTNAME_CHANGED,
            config=app_config,
            session_factory=test_db_with_clients['session_factory']
        )

        ansible_inventory_hook(context)

        # Verify inventory file was created
        assert test_inventory_path.exists()

        # Parse and verify YAML content
        with open(test_inventory_path) as f:
            inventory = yaml.safe_load(f)

        # Should have 'all' top-level group
        assert 'all' in inventory
        assert 'children' in inventory['all']

        # Should have fleet groups as children
        children = inventory['all']['children']
        assert 'testfleet' in children
        assert 'otherfleet' in children

        # Check testfleet group
        testfleet = children['testfleet']
        assert 'hosts' in testfleet
        assert 'host1.testfleet.test.internal' in testfleet['hosts']
        assert 'host2.testfleet.test.internal' in testfleet['hosts']

        # Hosts should have empty dicts (no ansible_host needed - resolved via hosts file)
        assert testfleet['hosts']['host1.testfleet.test.internal'] == {}
        assert testfleet['hosts']['host2.testfleet.test.internal'] == {}

        # otherfleet should be empty (client has no hostname)
        otherfleet = children['otherfleet']
        assert 'hosts' in otherfleet
        assert len(otherfleet['hosts']) == 0

        # Configured fleet with no clients at all is still a group
        assert children['emptyfleet'] == {'hosts': {}}

    finally:
        hooks.ansible_inventory.ANSIBLE_INVENTORY_PATH = original_path


def test_ansible_inventory_hook_handles_startup(test_db_with_clients, app_config, tmp_path):
    """Test that ansible_inventory hook runs on STARTUP events"""
    from hooks.ansible_inventory import ansible_inventory_hook

    # Override inventory path for testing
    import hooks.ansible_inventory
    original_path = hooks.ansible_inventory.ANSIBLE_INVENTORY_PATH
    test_inventory_path = tmp_path / "ansible_inventory.yml"
    hooks.ansible_inventory.ANSIBLE_INVENTORY_PATH = str(test_inventory_path)

    try:
        context = HookContext(
            event_type=EventType.STARTUP,
            config=app_config,
            session_factory=test_db_with_clients['session_factory']
        )

        ansible_inventory_hook(context)

        # Verify inventory file was created on startup
        assert test_inventory_path.exists()

        # Parse and verify YAML has fleet groups
        with open(test_inventory_path) as f:
            inventory = yaml.safe_load(f)

        assert 'all' in inventory
        assert 'testfleet' in inventory['all']['children']
        assert 'host1.testfleet.test.internal' in inventory['all']['children']['testfleet']['hosts']

    finally:
        hooks.ansible_inventory.ANSIBLE_INVENTORY_PATH = original_path
