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

# Registration to fleets from fleets_url (enrollment tokens)

import hashlib
from datetime import datetime, timedelta, UTC
from enrollment import normalize_host_key, token_matches

SITE = 'BigSouth2026/MidAtlantic/VirginiaTech'
TOKEN = 'dGhpcy1pcy1hLXRlc3QtZW5yb2xsbWVudC10b2tlbg'

@pytest.fixture
def url_fleet(test_app):
    test_client, config, session_factory = test_app
    config.fleets['bs26-ma'] = FleetConfig(
        ip6='fd01::1', subnet='fd01::/64', external_ip='1.2.3.4', port=51830,
        interface='wgf12345678', static=False, node='BigSouth2026/MidAtlantic',
        valid_until=datetime.now(UTC) + timedelta(days=30),
        sites={SITE: hashlib.sha256(TOKEN.encode()).hexdigest()},
        admin_principals=['node:BigSouth2026'],
    )
    return test_app

@pytest.fixture
def mock_wg():
    with patch('routes.wireguard') as mock_wg, patch('routes.allocate_random_ip', return_value='fd01::1234'):
        mock_wg.generate_keypair.return_value = ('client_priv', 'client_pub')
        mock_wg.get_server_public_key.return_value = 'server_pub'
        mock_wg.build_client_config.return_value = '[Interface]\nPrivateKey = client_priv'
        yield mock_wg

def register(test_client, fleet='bs26-ma', **body):
    return test_client.post(f'/fleet/{fleet}/register', json=body or None)

def test_register_with_enrollment_token(url_fleet, mock_wg, host_key):
    """Test a machine with its site's token is registered, with its site and host key stored"""
    test_client, config, session_factory = url_fleet

    response = register(test_client, site=SITE, token=TOKEN, host_key=host_key[1])

    assert response.status_code == 200
    mock_wg.add_peer.assert_called_once_with('wgf12345678', 'client_pub', 'fd01::1234')
    with session_factory() as session:
        client = session.query(DBClient).one()
        assert client.site == SITE
        # The comment is dropped
        assert client.host_key == ' '.join(host_key[1].split()[:2])

@pytest.mark.parametrize('body', [
    {},
    {'site': SITE},
    {'site': SITE, 'token': 'wrong'},
    {'site': 'BigSouth2026/MidAtlantic/Other', 'token': TOKEN},
    {'token': TOKEN},
])
def test_register_refuses_without_valid_token(body, url_fleet, mock_wg, host_key):
    """Test registration to a fleet from fleets_url needs a known site and its token"""
    test_client, config, session_factory = url_fleet

    response = register(test_client, host_key=host_key[1], **body)

    assert response.status_code == 403
    mock_wg.add_peer.assert_not_called()

def test_register_refuses_without_body(url_fleet, mock_wg):
    """Test a bare POST, as old images send, can't join a fleet from fleets_url"""
    test_client, config, session_factory = url_fleet
    assert test_client.post('/fleet/bs26-ma/register').status_code == 403

@pytest.mark.parametrize('bad_key', [None, '', 'ssh-ed25519', 'ssh-dss AAAA', 'ssh-ed25519 !!!notbase64',
                                     'ssh-ed25519 AAAAB3NzaC1yc2EAAAADAQAB'])
def test_register_refuses_bad_host_key(bad_key, url_fleet, mock_wg):
    """Test a missing or malformed host key is refused before anything is set up"""
    test_client, config, session_factory = url_fleet

    response = register(test_client, site=SITE, token=TOKEN, host_key=bad_key)

    assert response.status_code == 400
    mock_wg.add_peer.assert_not_called()

def test_register_refuses_removed_fleet(url_fleet, mock_wg, host_key):
    """Test a removed or expired fleet takes no new registrations, even with a valid token"""
    test_client, config, session_factory = url_fleet
    config.fleets['bs26-ma'].removed = True

    response = register(test_client, site=SITE, token=TOKEN, host_key=host_key[1])

    assert response.status_code == 403

def test_static_fleet_ignores_enrollment_fields(test_app, mock_wg, host_key):
    """Test static fleets register anyone, store no site, and keep the host key only for matching"""
    test_client, config, session_factory = test_app

    response = register(test_client, fleet='testfleet', site=SITE, token='anything', host_key=host_key[1])
    assert register(test_client, fleet='testfleet', host_key='not a key').status_code == 200

    assert response.status_code == 200
    with session_factory() as session:
        clients = session.query(DBClient).order_by(DBClient.id).all()
        assert [(c.site, c.host_key) for c in clients] == [
            (None, ' '.join(host_key[1].split()[:2])), (None, None)]

def test_token_matches():
    """Test tokens are compared by their sha256"""
    assert token_matches(TOKEN, hashlib.sha256(TOKEN.encode()).hexdigest())
    assert not token_matches(TOKEN + 'x', hashlib.sha256(TOKEN.encode()).hexdigest())
    assert not token_matches(TOKEN, None)

def test_normalize_host_key(host_key):
    """Test host keys are reduced to type and blob, and a blob of another type is refused"""
    key_type, blob = host_key[1].split()[:2]
    assert normalize_host_key(host_key[1]) == f'{key_type} {blob}'
    assert normalize_host_key(f'ecdsa-sha2-nistp256 {blob}') is None

def test_dashboard_shows_site_and_token_warning(url_fleet, mock_wg, host_key):
    """Test the fleet page shows each client's site and warns that rotating a token revokes it"""
    test_client, config, session_factory = url_fleet
    register(test_client, site=SITE, token=TOKEN, host_key=host_key[1])
    mock_wg.list_peers.return_value = []

    page = test_client.get('/fleet/bs26-ma').text

    assert SITE in page
    assert 'BigSouth2026/MidAtlantic' in page
    assert 'Rotating a site' in page
    assert 'Rotating a site' not in test_client.get('/fleet/testfleet').text

# Host certificates (W3)

import subprocess

def certificate_info(tmp_path, certificate):
    path = tmp_path / 'cert-cert.pub'
    path.write_text(certificate + '\n')
    return subprocess.run(['ssh-keygen', '-L', '-f', str(path)], capture_output=True, text=True, check=True).stdout

def sign_registration(key_path, fleet, site, host_key_line):
    """host_key_signature as wg_setup makes it: the host key signs fleet, site and host_key"""
    from enrollment import registration_message
    key = ' '.join(host_key_line.split()[:2])
    return subprocess.run(['ssh-keygen', '-q', '-Y', 'sign', '-n', 'wg-fleet-register', '-f', str(key_path), '-'],
                          input=registration_message(fleet, site, key), capture_output=True,
                          check=True).stdout.decode()

def enroll(test_client, key, ip='fd01::1234', fleet='bs26-ma', site=SITE, token=TOKEN, signed=True):
    """Register with a valid token as the machine at ip; key is (private key path, public key line)"""
    extra = {'host_key_signature': sign_registration(key[0], fleet, site, key[1])} if signed else {}
    with patch('routes.allocate_random_ip', return_value=ip):
        response = register(test_client, fleet=fleet, site=site, token=token, host_key=key[1], **extra)
    assert response.status_code == 200
    return ip

def test_ping_returns_host_certificate(url_fleet, mock_wg, host_key, tmp_path, host_ca_path):
    """Test a machine in a fleet from fleets_url gets a certificate for its FQDN, valid until the fleet's valid_until"""
    import host_ca
    test_client, config, session_factory = url_fleet
    host_ca.ensure_host_ca()
    ip = enroll(test_client, host_key)

    response = ping(test_client, ip, 't101', fleet='bs26-ma')

    assert response.status_code == 200
    certificate = response.json()['host_certificate']
    info = certificate_info(tmp_path, certificate)
    assert 'host certificate' in info
    assert 't101.bs26-ma.test.internal' in info
    principals = info.split('Principals:')[1].split('Critical Options:')[0].split()
    assert principals == ['t101.bs26-ma.test.internal']
    assert 'Key ID: "bs26-ma/t101/1"' in info
    # It certifies the machine's own key, signed by the CA served at /ssh/host_ca.pub
    assert certificate.split()[0] == 'ssh-ed25519-cert-v01@openssh.com'
    ca_fingerprint = subprocess.run(['ssh-keygen', '-l', '-f', str(host_ca_path) + '.pub'],
                                    capture_output=True, text=True, check=True).stdout.split()[1]
    assert ca_fingerprint in info
    assert test_client.get('/ssh/host_ca.pub').text.strip() == host_ca.public_key()

def test_host_certificate_reissued_only_on_change(url_fleet, mock_wg, host_key):
    """Test every ping returns the certificate, re-signed only when the name or validity changes"""
    import host_ca
    test_client, config, session_factory = url_fleet
    host_ca.ensure_host_ca()
    ip = enroll(test_client, host_key)

    with patch('routes.host_ca.sign_host_key', wraps=host_ca.sign_host_key) as mock_sign:
        first = ping(test_client, ip, 't101', fleet='bs26-ma').json()['host_certificate']
        # A reboot pings with the same name, or with none at all
        assert ping(test_client, ip, 't101', fleet='bs26-ma').json()['host_certificate'] == first
        assert ping(test_client, ip, fleet='bs26-ma').json()['host_certificate'] == first
        assert mock_sign.call_count == 1

        # setup renames the machine
        renamed = ping(test_client, ip, 'icpc-team-17', fleet='bs26-ma').json()['host_certificate']
        assert renamed != first
        assert mock_sign.call_count == 2
        assert mock_sign.call_args.kwargs['principal'] == 'icpc-team-17.bs26-ma.test.internal'

        # contestforge extends the fleet
        config.fleets['bs26-ma'].valid_until += timedelta(days=365)
        ping(test_client, ip, 'icpc-team-17', fleet='bs26-ma')
        assert mock_sign.call_count == 3
        assert mock_sign.call_args.kwargs['valid_until'] == config.fleets['bs26-ma'].valid_until

@pytest.mark.parametrize('case', ['static', 'removed', 'expired', 'no-hostname'])
def test_no_host_certificate(case, url_fleet, mock_wg, host_key):
    """Test static fleets, removed or expired fleets, and unnamed machines get no certificate"""
    import host_ca
    test_client, config, session_factory = url_fleet
    host_ca.ensure_host_ca()

    if case == 'static':
        with patch('routes.allocate_random_ip', return_value='fd00::77'):
            register(test_client, fleet='testfleet', host_key=host_key[1])
        response = ping(test_client, 'fd00::77', 't101')
    else:
        ip = enroll(test_client, host_key)
        if case == 'removed':
            config.fleets['bs26-ma'].removed = True
        if case == 'expired':
            config.fleets['bs26-ma'].valid_until = datetime.now(UTC) - timedelta(minutes=1)
        response = ping(test_client, ip, None if case == 'no-hostname' else 't101', fleet='bs26-ma')

    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}

def test_reregistration_reclaims_hostname(url_fleet, mock_wg, host_key):
    """Test a machine registering again with the same host key takes over its name and drops its old record"""
    import host_ca
    test_client, config, session_factory = url_fleet
    host_ca.ensure_host_ca()
    old_ip = enroll(test_client, host_key, ip='fd01::1111')
    ping(test_client, old_ip, 't101', fleet='bs26-ma')
    mock_wg.generate_keypair.return_value = ('new_priv', 'new_pub')

    with patch('routes.trigger_hooks') as mock_hooks:
        new_ip = enroll(test_client, host_key, ip='fd01::2222')

    with session_factory() as session:
        clients = session.query(DBClient).all()
        assert [(c.assigned_ip, c.hostname, c.public_key) for c in clients] == [(new_ip, 't101', 'new_pub')]
    mock_wg.remove_peer.assert_called_once_with('wgf12345678', 'client_pub')
    assert mock_hooks.call_args[0][0].client_data['ip'] == new_ip
    # And it keeps its name on its next ping, rather than becoming t101--2
    assert ping(test_client, new_ip, 't101', fleet='bs26-ma').json()['host_certificate']
    assert hostname_of(session_factory, new_ip) == 't101'

def test_other_host_key_cant_reclaim_hostname(url_fleet, mock_wg, host_key, tmp_path):
    """Test a different machine at the same site gets a deduplicated name, not another's"""
    test_client, config, session_factory = url_fleet
    other = tmp_path / 'other_key'
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(other)], check=True)
    first_ip = enroll(test_client, host_key, ip='fd01::1111')
    ping(test_client, first_ip, 't101', fleet='bs26-ma')
    mock_wg.generate_keypair.return_value = ('other_priv', 'other_pub')

    other_ip = enroll(test_client, (other, other.with_suffix('.pub').read_text()), ip='fd01::3333')
    ping(test_client, other_ip, 't101', fleet='bs26-ma')

    assert hostname_of(session_factory, first_ip) == 't101'
    assert hostname_of(session_factory, other_ip) == 't101--2'
    mock_wg.remove_peer.assert_not_called()

# Moving between fleets (plan X1.3)

def clients_by_fleet(session_factory):
    with session_factory() as session:
        return sorted((c.fleet_id, c.assigned_ip) for c in session.query(DBClient).all())

def test_moving_from_static_fleet_removes_old_record(url_fleet, mock_wg, host_key):
    """Test a machine that joins its contest's fleet after setup leaves the setup fleet at once"""
    test_client, config, session_factory = url_fleet
    with patch('routes.allocate_random_ip', return_value='fd00::77'):
        register(test_client, fleet='testfleet', host_key=host_key[1])
    ping(test_client, 'fd00::77', 'icpc-abc123')
    mock_wg.generate_keypair.return_value = ('new_priv', 'new_pub')

    with patch('routes.trigger_hooks') as mock_hooks:
        new_ip = enroll(test_client, host_key)

    assert clients_by_fleet(session_factory) == [('bs26-ma', new_ip)]
    mock_wg.remove_peer.assert_called_once_with('wg_testfleet', 'client_pub')
    mock_hooks.assert_called_once()
    # It doesn't take the setup fleet's name with it
    with session_factory() as session:
        assert session.query(DBClient).one().hostname is None

def test_moving_between_contest_fleets(url_fleet, mock_wg, host_key):
    """Test re-running setup for another contest moves the machine out of the first contest's fleet"""
    test_client, config, session_factory = url_fleet
    other_site, other_token = 'BigSouth2026/SouthEast/Clemson', 'c2Vjb25kLXRva2Vu'
    config.fleets['bs26-se'] = FleetConfig(
        ip6='fd02::1', subnet='fd02::/64', external_ip='1.2.3.4', port=51831,
        interface='wgf87654321', static=False, node='BigSouth2026/SouthEast',
        valid_until=datetime.now(UTC) + timedelta(days=30),
        sites={other_site: hashlib.sha256(other_token.encode()).hexdigest()},
    )
    old_ip = enroll(test_client, host_key)
    mock_wg.generate_keypair.return_value = ('new_priv', 'new_pub')

    enroll(test_client, host_key, ip='fd02::55', fleet='bs26-se', site=other_site, token=other_token)

    assert clients_by_fleet(session_factory) == [('bs26-se', 'fd02::55')]
    mock_wg.remove_peer.assert_called_once_with('wgf12345678', 'client_pub')

def test_static_registration_never_evicts(url_fleet, mock_wg, host_key):
    """Test an unauthenticated registration with a machine's host key can't remove that machine"""
    test_client, config, session_factory = url_fleet
    contest_ip = enroll(test_client, host_key)

    with patch('routes.allocate_random_ip', return_value='fd00::99'):
        assert register(test_client, fleet='testfleet', host_key=host_key[1]).status_code == 200

    assert clients_by_fleet(session_factory) == [('bs26-ma', contest_ip), ('testfleet', 'fd00::99')]
    mock_wg.remove_peer.assert_not_called()

def test_no_certificate_after_expiry(url_fleet, mock_wg, host_key):
    """Test a certificate issued before valid_until isn't handed out (or renewed) after it"""
    import host_ca
    test_client, config, session_factory = url_fleet
    host_ca.ensure_host_ca()
    ip = enroll(test_client, host_key)
    assert ping(test_client, ip, 't101', fleet='bs26-ma').json()['host_certificate']

    config.fleets['bs26-ma'].valid_until = datetime.now(UTC) - timedelta(seconds=1)

    assert ping(test_client, ip, 't101', fleet='bs26-ma').json() == {'status': 'ok'}

# Proving the host key (W2.5)

def test_forged_host_key_signature_is_refused(url_fleet, mock_wg, host_key, tmp_path):
    """Test a signature by some other key, or over other fields, is refused"""
    test_client, config, session_factory = url_fleet
    impostor = tmp_path / 'impostor'
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(impostor)], check=True)

    forged = sign_registration(impostor, 'bs26-ma', SITE, host_key[1])
    other_fleet = sign_registration(host_key[0], 'bs26-se', SITE, host_key[1])
    for signature in (forged, other_fleet, 'garbage'):
        response = register(test_client, site=SITE, token=TOKEN, host_key=host_key[1],
                            host_key_signature=signature)
        assert response.status_code == 403
    mock_wg.add_peer.assert_not_called()

def test_unsigned_registration_cant_evict(url_fleet, mock_wg, host_key):
    """Test a token holder who sends another machine's public key without its signature can't
    take its name or remove it, but still gets in (old clients don't sign yet)"""
    test_client, config, session_factory = url_fleet
    victim_ip = enroll(test_client, host_key, ip='fd01::1111')
    ping(test_client, victim_ip, 't101', fleet='bs26-ma')
    mock_wg.generate_keypair.return_value = ('attacker_priv', 'attacker_pub')

    attacker_ip = enroll(test_client, host_key, ip='fd01::6666', signed=False)
    ping(test_client, attacker_ip, 't101', fleet='bs26-ma')

    assert hostname_of(session_factory, victim_ip) == 't101'
    assert hostname_of(session_factory, attacker_ip) == 't101--2'
    mock_wg.remove_peer.assert_not_called()

def test_registration_message_format():
    """Test the signed message is the line format wg_setup builds with printf"""
    from enrollment import registration_message
    assert registration_message('bs26-ma', SITE, 'ssh-ed25519 AAAA') == (
        b'fleet=bs26-ma\nsite=BigSouth2026/MidAtlantic/VirginiaTech\nhost_key=ssh-ed25519 AAAA\n')

def test_shell_signature_verifies(host_key):
    """Test a signature made with the documented printf | ssh-keygen pipeline verifies"""
    from enrollment import host_key_signature_valid
    key = ' '.join(host_key[1].split()[:2])
    signature = subprocess.run(
        ['sh', '-c', 'printf "fleet=%s\\nsite=%s\\nhost_key=%s\\n" "$1" "$2" "$3" | '
                     'ssh-keygen -q -Y sign -n wg-fleet-register -f "$4" -',
         'sh', 'bs26-ma', SITE, key, str(host_key[0])],
        capture_output=True, text=True, check=True).stdout

    assert host_key_signature_valid('bs26-ma', SITE, key, signature)
    assert not host_key_signature_valid('bs26-ma', 'Other/Site', key, signature)

def test_host_certificate_reissued_when_ca_changes(url_fleet, mock_wg, host_key, host_ca_path, tmp_path):
    """Test replacing the host CA (e.g. a fresh one for production) re-signs every certificate on the next ping"""
    import host_ca
    test_client, config, session_factory = url_fleet
    host_ca.ensure_host_ca()
    ip = enroll(test_client, host_key)
    first = ping(test_client, ip, 't101', fleet='bs26-ma').json()['host_certificate']

    # The old CA is backed up and removed, and wg-fleet restarts with a new one
    host_ca_path.unlink()
    host_ca_path.with_name('host_ca.pub').unlink()
    new_ca = host_ca.ensure_host_ca()

    second = ping(test_client, ip, 't101', fleet='bs26-ma').json()['host_certificate']
    assert second != first
    new_fingerprint = subprocess.run(['ssh-keygen', '-l', '-f', str(host_ca_path) + '.pub'],
                                     capture_output=True, text=True, check=True).stdout.split()[1]
    assert new_fingerprint in certificate_info(tmp_path, second)
    # And it's stable again after that
    assert ping(test_client, ip, 't101', fleet='bs26-ma').json()['host_certificate'] == second

def test_dashboard_shows_connection_type(url_fleet, mock_wg, host_key):
    """Test the fleet page tells a direct peer from one tunneled through wstunnel"""
    test_client, config, session_factory = url_fleet
    enroll(test_client, host_key)

    mock_wg.list_peers.return_value = [
        {'public_key': 'client_pub', 'endpoint': '23.117.92.130:53796', 'last_handshake': None}]
    page = test_client.get('/fleet/bs26-ma').text
    assert 'class="conn conn-direct"' in page and '23.117.92.130:53796' in page
    assert 'class="conn conn-wstunnel"' not in page

    mock_wg.list_peers.return_value = [
        {'public_key': 'client_pub', 'endpoint': '127.0.0.1:40122', 'last_handshake': None}]
    page = test_client.get('/fleet/bs26-ma').text
    assert 'class="conn conn-wstunnel"' in page
    assert '127.0.0.1:40122' not in page
