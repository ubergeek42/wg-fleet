"""
The management server's ssh host CA.

wg-fleet signs the host keys of machines in fleets from fleets_url, for the name it
assigned them (<hostname>.<fleet>.<domain>), so admins and the bastion can trust every
machine through one @cert-authority line. Static fleets never get certificates:
anyone can register in them, so a certificate would vouch for whoever registered
under a machine's name.

Back the CA up: if it's lost, a new one is created and every known_hosts entry and
every config's ssh_host_ca_keys has to change.
"""
from datetime import datetime, UTC
from pathlib import Path
import logging
import os
import tempfile

from command import run_command

logger = logging.getLogger(__name__)

HOST_CA_PATH = "/var/lib/wg-fleet/host_ca"


def ensure_host_ca(comment: str = "wg-fleet host CA", path: str = None) -> str:
    """
    Create the host CA on first start, unless one is already there (e.g. restored).

    Returns:
        The CA's public key line
    """
    path = Path(path or HOST_CA_PATH)
    pub_path = path.with_name(path.name + '.pub')
    if not path.exists():
        logger.info(f"Creating ssh host CA at {path}")
        run_command(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', comment, '-f', str(path)])
    elif not pub_path.exists():
        # A restored backup may have only the private key
        pub_path.write_text(run_command(['ssh-keygen', '-y', '-f', str(path)]) + '\n')
    os.chmod(path, 0o600)
    return public_key(path)


def public_key(path: str = None) -> str:
    """The host CA's public key line"""
    path = Path(path or HOST_CA_PATH)
    return path.with_name(path.name + '.pub').read_text().strip()


def sign_host_key(host_key: str, principal: str, key_id: str, valid_until: datetime,
                  path: str = None) -> str:
    """
    Sign a machine's host key.

    Args:
        host_key: "<type> <base64>" public key
        principal: The one name the certificate is valid for
        key_id: Certificate key id, shown in sshd logs
        valid_until: End of validity (the fleet's valid_until)

    Returns:
        The certificate, one line
    """
    path = path or HOST_CA_PATH
    end = valid_until.astimezone(UTC).strftime('%Y%m%d%H%M%SZ')
    with tempfile.TemporaryDirectory() as tmp:
        key_path = Path(tmp) / 'host_key.pub'
        key_path.write_text(host_key + '\n')
        run_command([
            'ssh-keygen', '-q', '-s', str(path), '-h',
            '-I', key_id,
            '-n', principal,
            # Allow for machines whose clock is a little behind
            '-V', f'-5m:{end}',
            '-z', str(int(datetime.now(UTC).timestamp())),
            str(key_path),
        ])
        return (Path(tmp) / 'host_key-cert.pub').read_text().strip()
