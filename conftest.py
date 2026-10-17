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
