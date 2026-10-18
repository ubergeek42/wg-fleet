import subprocess
from datetime import datetime, UTC

import host_ca


def test_ensure_host_ca_creates_once(host_ca_path):
    """Test the CA is created on first start and kept after that"""
    first = host_ca.ensure_host_ca(comment='wg-fleet host CA mgmt.example.org')
    assert first.startswith('ssh-ed25519 ') and first.endswith('mgmt.example.org')
    assert oct(host_ca_path.stat().st_mode & 0o777) == '0o600'
    assert host_ca.ensure_host_ca() == first


def test_ensure_host_ca_restored_private_key_only(host_ca_path):
    """Test a backup of just the private key is used, and its public key recreated"""
    first = host_ca.ensure_host_ca()
    host_ca_path.with_name('host_ca.pub').unlink()

    assert host_ca.ensure_host_ca().split()[:2] == first.split()[:2]


def test_sign_host_key_validity(host_ca_path, host_key, tmp_path):
    """Test certificates end exactly at valid_until"""
    host_ca.ensure_host_ca()
    key = ' '.join(host_key[1].split()[:2])

    certificate = host_ca.sign_host_key(key, 't1.f.icpcnet.internal', 'f/t1/1',
                                        datetime(2027, 1, 1, tzinfo=UTC))

    path = tmp_path / 'c-cert.pub'
    path.write_text(certificate + '\n')
    info = subprocess.run(['ssh-keygen', '-L', '-f', str(path)], capture_output=True, text=True,
                          check=True, env={'TZ': 'UTC'}).stdout
    assert 'to 2027-01-01T00:00:00' in info
