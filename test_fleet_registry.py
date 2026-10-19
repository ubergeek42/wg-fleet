import json
import os
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, UTC
from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import fleet_registry
from config import Config, FleetConfig, interface_name
from fleet_registry import (apply_fleets, build_url_fleets, initialize_fleets,
                            reconcile_fleet_state, refresh_fleets_once,
                            remove_unconfigured_fleet_clients, retire_removed_fleets,
                            setup_fleet_interface)
from fleet_source import FleetsDocument
from hook_manager import EventType
from models import Base, Client, KnownFleet

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def test_db():
    """Create test database"""
    fd, db_path = tempfile.mkstemp(suffix='.db')
    os.close(fd)

    engine = create_engine(f'sqlite:///{db_path}')
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    yield session_factory

    os.unlink(db_path)


@pytest.fixture(autouse=True)
def wireguard_dir(tmp_path, monkeypatch):
    path = tmp_path / 'wireguard'
    path.mkdir()
    monkeypatch.setattr(fleet_registry, 'WIREGUARD_DIR', path)
    return path


def static_fleet(port=51820, subnet='fd00:a0a8:34d:9900::/64', name='setup'):
    return FleetConfig(ip6=subnet.replace('::/64', '::1'), subnet=subnet, external_ip='203.0.113.5',
                       port=port, interface=interface_name(name, static=True))


def url_fleet(name='bs26-midatlantic', port=51830, n=0x4210, valid_until=NOW + timedelta(days=30), **kw):
    subnet = f'fd00:a0a8:34d:{n:x}::/64'
    return FleetConfig(ip6=f'fd00:a0a8:34d:{n:x}::1', subnet=subnet, external_ip='203.0.113.5',
                       port=port, interface=interface_name(name, static=False), static=False,
                       node='BigSouth2026/MidAtlantic', valid_until=valid_until,
                       sites={'site': 'a' * 64}, admin_principals=['node:BigSouth2026'], **kw)


def make_config(static=None):
    static = static if static is not None else {'setup': static_fleet()}
    return Config(domain='icpcnet.internal', prune_timeout='30m', fleets=dict(static),
                  static_fleets=static, fleets_url='https://bucket.example.org/fleets.json',
                  server_name='mgmt.example.org', external_ip='203.0.113.5')


def doc(*fleets, revision=1):
    return FleetsDocument(revision=revision, user_ca_keys=['ssh-ed25519 AAAAca'],
                          fleets={name: fleet for name, fleet in fleets})


def known_rows(session_factory):
    with session_factory() as session:
        return {row.name: (row.subnet, row.port, row.removed_at) for row in session.query(KnownFleet)}


# Interface setup and reconciliation

@patch('fleet_registry.wireguard')
def test_setup_fleet_interface_new(mock_wg, wireguard_dir):
    """Test setting up a new fleet interface"""
    mock_wg.generate_keypair.return_value = ('priv_key', 'pub_key')
    fleet = static_fleet(name='testfleet')

    setup_fleet_interface('testfleet', fleet)

    mock_wg.generate_keypair.assert_called_once()
    mock_wg.create_interface_config.assert_called_once_with(
        'wg_testfleet', fleet, 'priv_key', str(wireguard_dir / 'wg_testfleet.conf')
    )
    mock_wg.bring_up_interface.assert_called_once_with('wg_testfleet')


@pytest.mark.parametrize('port, expect_restart', [(51820, False), (51830, True)])
def test_setup_fleet_interface_existing_config(port, expect_restart, wireguard_dir):
    """Test an existing config is kept as-is, or rewritten and restarted when the fleet config changed"""
    import wireguard
    config_path = wireguard_dir / 'wg_testfleet.conf'
    wireguard.create_interface_config('wg_testfleet', static_fleet(name='testfleet'),
                                      'c2VydmVyX2tleQ==', str(config_path))

    with patch('fleet_registry.wireguard.generate_keypair') as mock_genkey, \
         patch('fleet_registry.wireguard.interface_exists', return_value=True), \
         patch('fleet_registry.wireguard.bring_down_interface') as mock_down, \
         patch('fleet_registry.wireguard.bring_up_interface') as mock_up:
        setup_fleet_interface('testfleet', static_fleet(port=port, name='testfleet'))

    mock_genkey.assert_not_called()
    content = config_path.read_text()
    assert 'PrivateKey = c2VydmVyX2tleQ==' in content
    assert f'ListenPort = {port}' in content
    assert mock_down.called == expect_restart
    mock_up.assert_called_once_with('wg_testfleet')


@patch('fleet_registry.wireguard')
def test_reconcile_fleet_state(mock_wg, test_db):
    """Test reconciliation removes mismatched entries"""
    with test_db() as session:
        session.add_all([
            # Client in DB but not in WG
            Client(fleet_id="testfleet", public_key="orphan_key", assigned_ip="fd00::100",
                   http_request_ip="1.2.3.4", timestamp=datetime.now(UTC)),
            # Client in both DB and WG
            Client(fleet_id="testfleet", public_key="valid_key", assigned_ip="fd00::101",
                   http_request_ip="1.2.3.5", timestamp=datetime.now(UTC)),
        ])
        session.commit()

    # WireGuard peers: valid + extra peer not in DB
    mock_wg.list_peers.return_value = [
        {'public_key': 'valid_key', 'last_handshake': datetime.now(UTC)},
        {'public_key': 'extra_key', 'last_handshake': datetime.now(UTC)}
    ]

    reconcile_fleet_state('testfleet', static_fleet(name='testfleet'), test_db)

    with test_db() as session:
        assert [c.public_key for c in session.query(Client).all()] == ['valid_key']
    mock_wg.list_peers.assert_called_once_with('wg_testfleet')
    mock_wg.remove_peer.assert_called_once_with('wg_testfleet', 'extra_key')


def test_remove_unconfigured_fleet_clients(test_db):
    """Test clients of fleets that aren't served any more are deleted"""
    with test_db() as session:
        session.add_all([
            Client(fleet_id="kept", public_key="k1", assigned_ip="fd00::1",
                   http_request_ip="1.2.3.4", timestamp=datetime.now(UTC)),
            Client(fleet_id="dropped", public_key="k2", assigned_ip="fd01::1",
                   http_request_ip="1.2.3.4", timestamp=datetime.now(UTC)),
        ])
        session.commit()

    remove_unconfigured_fleet_clients({'kept': static_fleet()}, test_db)

    with test_db() as session:
        assert [c.fleet_id for c in session.query(Client).all()] == ['kept']


def test_url_fleet_interface_names():
    """Test fleets from fleets_url get short hashed interface names, static ones keep wg_<fleet>"""
    assert interface_name('southeast', static=True) == 'wg_southeast'
    name = interface_name('bigsouth2026-midatlantic-with-a-long-name', static=False)
    assert name.startswith('wgf') and len(name) == 11
    assert name == interface_name('bigsouth2026-midatlantic-with-a-long-name', static=False)
    assert name != interface_name('bigsouth2026-southeast', static=False)


# Merging fleets.json into the served fleets

@pytest.fixture
def mock_setup():
    with patch('fleet_registry.setup_fleet_interface') as mock:
        yield mock


def test_apply_adds_new_fleet(test_db, mock_setup):
    """Test a new fleet gets an interface, is remembered, and is served next to static fleets"""
    config = make_config()
    fleet = url_fleet()

    assert apply_fleets(config, test_db, doc(('bs26-midatlantic', fleet)), NOW) is True

    mock_setup.assert_called_once_with('bs26-midatlantic', fleet)
    assert set(config.fleets) == {'setup', 'bs26-midatlantic'}
    assert config.user_ca_keys == ['ssh-ed25519 AAAAca']
    assert known_rows(test_db) == {'bs26-midatlantic': ('fd00:a0a8:34d:4210::/64', 51830, None)}

    # Applying the same document again changes nothing and doesn't touch the interface
    assert apply_fleets(config, test_db, doc(('bs26-midatlantic', fleet)), NOW) is False
    assert mock_setup.call_count == 1


def test_apply_skips_fleet_whose_interface_fails(test_db, mock_setup):
    """Test a fleet whose interface can't be set up isn't served or remembered"""
    mock_setup.side_effect = RuntimeError('port in use')
    config = make_config()

    apply_fleets(config, test_db, doc(('bs26-midatlantic', url_fleet())), NOW)

    assert set(config.fleets) == {'setup'}
    assert known_rows(test_db) == {}


def test_apply_never_moves_a_fleet(test_db, mock_setup, caplog):
    """Test a changed subnet or port for a known fleet is ignored"""
    config = make_config()
    apply_fleets(config, test_db, doc(('f', url_fleet(name='f'))), NOW)

    moved = url_fleet(name='f', port=51899, n=0x4299)
    apply_fleets(config, test_db, doc(('f', moved), revision=2), NOW)

    assert config.fleets['f'].port == 51830
    assert config.fleets['f'].subnet == 'fd00:a0a8:34d:4210::/64'
    # Everything else from the new entry is still taken
    assert config.fleets['f'].sites == moved.sites
    assert 'ignoring the change' in caplog.text


def test_apply_marks_removed_fleets_and_brings_them_back(test_db, mock_setup):
    """Test a fleet that leaves fleets.json is kept, marked removed, and restored if it returns"""
    config = make_config()
    apply_fleets(config, test_db, doc(('f', url_fleet(name='f'))), NOW)

    assert apply_fleets(config, test_db, doc(revision=2), NOW) is True
    assert config.fleets['f'].removed is True
    assert config.fleets['f'].sites == {}
    assert known_rows(test_db)['f'][2] is not None

    apply_fleets(config, test_db, doc(('f', url_fleet(name='f')), revision=3), NOW)
    assert config.fleets['f'].removed is False
    assert known_rows(test_db)['f'][2] is None
    assert mock_setup.call_count == 1


def test_apply_expired_fleet(test_db, mock_setup):
    """Test a fleet past valid_until stops accepting registrations, and is never created"""
    config = make_config()
    apply_fleets(config, test_db, doc(('f', url_fleet(name='f', valid_until=NOW + timedelta(hours=1)))), NOW)

    later = NOW + timedelta(hours=2)
    apply_fleets(config, test_db, doc(('f', url_fleet(name='f', valid_until=NOW + timedelta(hours=1)))), later)
    assert config.fleets['f'].removed is True

    # An expired fleet that was never served (or already torn down) isn't brought up
    apply_fleets(config, test_db, doc(('g', url_fleet(name='g', port=51831, n=0x4211,
                                                      valid_until=NOW))), later)
    assert 'g' not in config.fleets


def test_apply_ignores_collisions(test_db, mock_setup, caplog):
    """Test fleets that clash with a static fleet's name, port or subnet are ignored"""
    config = make_config()

    apply_fleets(config, test_db, doc(
        ('setup', url_fleet(name='setup', port=51840, n=0x4240)),
        ('same-port', url_fleet(name='same-port', port=51820, n=0x4241)),
        ('same-subnet', url_fleet(name='same-subnet', port=51842, n=0x9900)),
        ('fine', url_fleet(name='fine', port=51843, n=0x4243)),
    ), NOW)

    assert set(config.fleets) == {'setup', 'fine'}
    assert config.fleets['setup'].static is True
    assert "a static fleet has that name" in caplog.text


def test_apply_new_fleet_waits_for_removed_fleets_port(test_db, mock_setup, caplog):
    """Test a fleet reusing a removed fleet's port is set up only once that one is torn down"""
    config = make_config()
    apply_fleets(config, test_db, doc(('old', url_fleet(name='old'))), NOW)

    apply_fleets(config, test_db, doc(('new', url_fleet(name='new', n=0x4211)), revision=2), NOW)
    assert 'new' not in config.fleets
    assert 'waits for removed fleet' in caplog.text

    with patch('fleet_registry.wireguard') as mock_wg:
        mock_wg.interface_exists.return_value = False
        retire_removed_fleets(config, test_db, NOW + timedelta(days=2))
    apply_fleets(config, test_db, doc(('new', url_fleet(name='new', n=0x4211)), revision=2),
                 NOW + timedelta(days=2))
    assert set(config.fleets) == {'setup', 'new'}


def test_apply_without_document_keeps_known_fleets(test_db, mock_setup):
    """Test that with no fleets.json at all, remembered fleets are served and not treated as removed"""
    config = make_config()
    with test_db() as session:
        session.add(KnownFleet(name='f', ip6='fd00:a0a8:34d:4210::1', subnet='fd00:a0a8:34d:4210::/64',
                               port=51830))
        session.commit()

    apply_fleets(config, test_db, None, NOW)

    assert config.fleets['f'].removed is False
    assert config.fleets['f'].sites == {}
    assert known_rows(test_db)['f'][2] is None
    mock_setup.assert_not_called()


# Tearing down removed fleets

def removed_fleet(config, test_db, removed_at):
    apply_fleets(config, test_db, doc(('f', url_fleet(name='f'))), removed_at)
    apply_fleets(config, test_db, doc(revision=2), removed_at)


@pytest.mark.parametrize('age, clients, peers, expect_retired', [
    (timedelta(hours=25), False, False, True),
    (timedelta(hours=23), False, False, False),   # grace period not over
    (timedelta(hours=25), True, False, False),    # still has clients
    (timedelta(hours=25), False, True, False),    # still has peers
])
def test_retire_removed_fleets(age, clients, peers, expect_retired, test_db, mock_setup, wireguard_dir):
    """Test a removed fleet is torn down only once it's empty and the grace period has passed"""
    config = make_config()
    removed_fleet(config, test_db, NOW)
    conf = wireguard_dir / f"{interface_name('f', static=False)}.conf"
    conf.write_text('[Interface]\n')
    if clients:
        with test_db() as session:
            session.add(Client(fleet_id='f', public_key='k', assigned_ip='fd00:a0a8:34d:4210::5',
                               http_request_ip='1.2.3.4', timestamp=NOW))
            session.commit()

    with patch('fleet_registry.wireguard') as mock_wg:
        mock_wg.interface_exists.return_value = True
        mock_wg.list_peers.return_value = [{'public_key': 'k'}] if peers else []
        assert retire_removed_fleets(config, test_db, NOW + age) is expect_retired

    assert ('f' in config.fleets) is not expect_retired
    assert ('f' in known_rows(test_db)) is not expect_retired
    assert conf.exists() is not expect_retired
    assert mock_wg.bring_down_interface.called is expect_retired


def test_retire_leaves_served_fleets_alone(test_db, mock_setup):
    """Test only removed fleets are torn down"""
    config = make_config()
    apply_fleets(config, test_db, doc(('f', url_fleet(name='f'))), NOW)

    with patch('fleet_registry.wireguard') as mock_wg:
        assert retire_removed_fleets(config, test_db, NOW + timedelta(days=30)) is False
        mock_wg.bring_down_interface.assert_not_called()


# Startup and refresh

def test_initialize_without_any_fleets_json(test_db):
    """Test a first start with an unreachable URL serves the static fleets and starts anyway"""
    config = make_config()
    source = MagicMock(document=None)

    with patch('fleet_registry.setup_fleet_interface') as mock_setup, \
         patch('fleet_registry.reconcile_fleet_state'), \
         patch('fleet_registry.trigger_hooks') as mock_hooks:
        initialize_fleets(config, test_db, source)

    source.load_cached.assert_called_once()
    source.fetch.assert_called_once()
    mock_setup.assert_called_once_with('setup', config.static_fleets['setup'])
    assert set(config.fleets) == {'setup'}
    assert mock_hooks.call_args[0][0].event_type == EventType.STARTUP


def test_initialize_brings_up_known_fleets(test_db):
    """Test fleets remembered from before a restart get their interfaces back"""
    config = make_config()
    fleet = url_fleet(name='f')
    with test_db() as session:
        session.add(KnownFleet(name='f', ip6=fleet.ip6, subnet=fleet.subnet, port=fleet.port))
        session.commit()
    source = MagicMock(document=doc(('f', fleet)))

    with patch('fleet_registry.setup_fleet_interface') as mock_setup, \
         patch('fleet_registry.reconcile_fleet_state') as mock_reconcile, \
         patch('fleet_registry.trigger_hooks'):
        initialize_fleets(config, test_db, source)

    assert [c[0][0] for c in mock_setup.call_args_list] == ['setup', 'f']
    assert {c[0][0] for c in mock_reconcile.call_args_list} == {'setup', 'f'}


def test_refresh_runs_hooks_only_on_change(test_db, mock_setup):
    """Test FLEETS_CHANGED fires when the served fleets change, not on every refresh"""
    config = make_config()
    source = MagicMock(document=doc(('f', url_fleet(name='f'))))

    with patch('fleet_registry.trigger_hooks') as mock_hooks:
        refresh_fleets_once(config, test_db, source, document_changed=True)
        assert mock_hooks.call_count == 1
        assert mock_hooks.call_args[0][0].event_type == EventType.FLEETS_CHANGED

        refresh_fleets_once(config, test_db, source, document_changed=False)
        assert mock_hooks.call_count == 1


# /run/wg_fleets.json

def test_fleets_file(test_db, mock_setup, fleets_file_path):
    """Test the fleets file lists served fleets for the host-side script, without removed ones"""
    from hook_manager import trigger_hooks, HookContext
    config = make_config()
    apply_fleets(config, test_db, doc(('f', url_fleet(name='f')),
                                      ('gone', url_fleet(name='gone', port=51831, n=0x4211))), NOW)
    apply_fleets(config, test_db, doc(('f', url_fleet(name='f')), revision=2), NOW)

    trigger_hooks(HookContext(event_type=EventType.FLEETS_CHANGED, config=config,
                              session_factory=test_db))

    data = json.loads(fleets_file_path.read_text())
    assert data['user_ca_keys'] == ['ssh-ed25519 AAAAca']
    assert set(data['fleets']) == {'setup', 'f'}
    assert data['fleets']['setup'] == {
        'static': True, 'interface': 'wg_setup', 'port': 51820,
        'subnet': 'fd00:a0a8:34d:9900::/64', 'ip6': 'fd00:a0a8:34d:9900::1',
        'admin_principals': ['node:*'],
    }
    assert data['fleets']['f'] == {
        'static': False, 'interface': interface_name('f', static=False), 'port': 51830,
        'subnet': 'fd00:a0a8:34d:4210::/64', 'ip6': 'fd00:a0a8:34d:4210::1',
        'node': 'BigSouth2026/MidAtlantic', 'admin_principals': ['node:BigSouth2026'],
    }


def test_fleets_file_ignores_client_events(test_db, fleets_file_path):
    """Test client events don't rewrite the fleets file (and wake the host-side script)"""
    from hook_manager import trigger_hooks, HookContext
    config = make_config()

    trigger_hooks(HookContext(event_type=EventType.CLIENT_REMOVED, config=config,
                              session_factory=test_db))

    assert not fleets_file_path.exists()


# End-to-end scenarios (contestforge's NEXT-STEPS checks)

def test_fleet_back_before_teardown_keeps_its_addresses(test_db, mock_setup, fleets_file_path):
    """Test a contest made a draft and published again keeps its interface, subnet and port"""
    from hook_manager import trigger_hooks, HookContext
    config = make_config()
    apply_fleets(config, test_db, doc(('f', url_fleet(name='f'))), NOW)

    apply_fleets(config, test_db, doc(revision=2), NOW)
    trigger_hooks(HookContext(event_type=EventType.FLEETS_CHANGED, config=config, session_factory=test_db))
    assert 'f' not in json.loads(fleets_file_path.read_text())['fleets']

    # contestforge republishes it; even if its entry now said otherwise, the old addresses win
    apply_fleets(config, test_db, doc(('f', url_fleet(name='f', port=51899, n=0x4299)), revision=3), NOW)
    trigger_hooks(HookContext(event_type=EventType.FLEETS_CHANGED, config=config, session_factory=test_db))
    assert (config.fleets['f'].subnet, config.fleets['f'].port) == ('fd00:a0a8:34d:4210::/64', 51830)
    assert config.fleets['f'].removed is False
    assert 'f' in json.loads(fleets_file_path.read_text())['fleets']
    assert mock_setup.call_count == 1


def test_fleet_back_after_teardown_is_set_up_again(test_db, mock_setup):
    """Test a fleet that returns after it was torn down gets a fresh interface with contestforge's addresses"""
    config = make_config()
    removed_fleet(config, test_db, NOW)
    with patch('fleet_registry.wireguard') as mock_wg:
        mock_wg.interface_exists.return_value = False
        assert retire_removed_fleets(config, test_db, NOW + timedelta(days=2))

    apply_fleets(config, test_db, doc(('f', url_fleet(name='f')), revision=3), NOW + timedelta(days=2))

    assert config.fleets['f'].removed is False
    assert known_rows(test_db)['f'] == ('fd00:a0a8:34d:4210::/64', 51830, None)
    assert mock_setup.call_count == 2


def test_expired_fleet_keeps_existing_peers(test_db, mock_setup):
    """Test expiry refuses newcomers but never cuts off a machine that's still connected"""
    from pruning import prune_stale_clients_once
    config = make_config()
    apply_fleets(config, test_db, doc(('f', url_fleet(name='f', valid_until=NOW + timedelta(hours=1)))), NOW)
    with test_db() as session:
        session.add(Client(fleet_id='f', public_key='k', assigned_ip='fd00:a0a8:34d:4210::5',
                           http_request_ip='1.2.3.4', hostname='t1', timestamp=datetime.now(UTC)))
        session.commit()

    later = NOW + timedelta(days=3)
    apply_fleets(config, test_db, doc(('f', url_fleet(name='f', valid_until=NOW + timedelta(hours=1)))), later)
    with patch('fleet_registry.wireguard') as mock_wg, patch('pruning.wireguard') as mock_prune_wg:
        mock_wg.interface_exists.return_value = True
        mock_wg.list_peers.return_value = [{'public_key': 'k'}]
        mock_prune_wg.list_peers.return_value = [{'public_key': 'k', 'last_handshake': datetime.now(UTC)}]
        prune_stale_clients_once(config, test_db)
        assert retire_removed_fleets(config, test_db, later) is False

    assert config.fleets['f'].removed is True
    with test_db() as session:
        assert session.query(Client).count() == 1
