import pytest
import hooks.hosts_file
import hooks.prometheus_sd
import hooks.ansible_inventory


@pytest.fixture(autouse=True)
def hook_output_paths(tmp_path, monkeypatch):
    """Redirect hook output files so tests never touch the real files in /run"""
    paths = {
        'hosts': tmp_path / 'wg_fleet_hosts',
        'prometheus': tmp_path / 'prometheus_targets.json',
        'ansible': tmp_path / 'ansible_inventory.yml',
    }
    monkeypatch.setattr(hooks.hosts_file, 'HOSTS_FILE_PATH', str(paths['hosts']))
    monkeypatch.setattr(hooks.prometheus_sd, 'PROMETHEUS_TARGETS_PATH', str(paths['prometheus']))
    monkeypatch.setattr(hooks.ansible_inventory, 'ANSIBLE_INVENTORY_PATH', str(paths['ansible']))
    return paths


@pytest.fixture
def signer(tmp_path):
    """
    A publisher signing key, as contestforge's `flask signing-key init` makes.

    Returns an object with `allowed_signers` (lines for config_signers) and
    `sign(data) -> bytes` (a detached `ssh-keygen -Y sign -n contestforge` signature).
    """
    import subprocess

    key_path = tmp_path / 'publisher_signing_key'
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'test',
                    '-f', str(key_path)], check=True)
    public_key = key_path.with_suffix('.pub').read_text().strip()

    class Signer:
        allowed_signers = [f'contestforge namespaces="contestforge" {public_key}']

        @staticmethod
        def sign(data: bytes) -> bytes:
            result = subprocess.run(['ssh-keygen', '-q', '-Y', 'sign', '-n', 'contestforge',
                                     '-f', str(key_path)],
                                    input=data, capture_output=True, check=True)
            return result.stdout

    return Signer()


@pytest.fixture(autouse=True)
def fleets_file_path(tmp_path, monkeypatch):
    """Redirect /run/wg_fleets.json like the other hook outputs"""
    import hooks.fleets_file
    path = tmp_path / 'wg_fleets.json'
    monkeypatch.setattr(hooks.fleets_file, 'FLEETS_FILE_PATH', str(path))
    return path


@pytest.fixture(scope='session')
def host_key(tmp_path_factory):
    """A machine's ed25519 host key pair: (private key path, public key line)"""
    import subprocess
    path = tmp_path_factory.mktemp('hostkey') / 'ssh_host_ed25519_key'
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'root@icpc',
                    '-f', str(path)], check=True)
    return path, path.with_suffix('.pub').read_text().strip()
