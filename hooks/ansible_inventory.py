"""
Ansible inventory hook.

Generates Ansible YAML inventory file when clients change.
Each fleet becomes a group, hosts are named hostname.fleetname.
"""
from hook_manager import register_hook, HookContext
from models import Client
from pathlib import Path
from collections import defaultdict
import yaml
import logging

logger = logging.getLogger(__name__)

ANSIBLE_INVENTORY_PATH = "/run/ansible_inventory.yml"


@register_hook
def ansible_inventory_hook(context: HookContext):
    """
    Generate Ansible YAML inventory file.

    Creates inventory with:
    - Each fleet as a group under 'all.children'
    - Hosts named as hostname.fleetname (resolvable via hosts file)

    Only includes clients with hostnames set.
    No ansible_host needed since hosts file hook makes names resolvable.
    """
    # Every event can change the client set, so always regenerate
    try:
        # Build inventory structure
        # Group clients by fleet
        fleets = defaultdict(list)

        # Include every configured fleet, even ones with no clients yet
        for fleet_name in context.config.fleets:
            fleets[fleet_name] = []

        with context.session_factory() as session:
            # Query all clients with hostnames
            clients = session.query(Client).filter(
                Client.hostname.isnot(None)
            ).all()

            for client in clients:
                fleets[client.fleet_id].append(client)

        # Build YAML inventory structure
        inventory = {
            'all': {
                'children': {}
            }
        }

        for fleet_name, clients in fleets.items():
            fleet_group = {
                'hosts': {}
            }

            for client in clients:
                host_name = f"{client.hostname}.{fleet_name}"
                # No need for ansible_host - hosts file makes names resolvable
                fleet_group['hosts'][host_name] = {
                    #'ansible_host': client.assigned_ip
                }

            inventory['all']['children'][fleet_name] = fleet_group

        # Write atomically (write to temp, then rename)
        temp_path = Path(f"{ANSIBLE_INVENTORY_PATH}.tmp")
        with temp_path.open('w') as f:
            yaml.safe_dump(inventory, f, default_flow_style=False, sort_keys=False)

        temp_path.rename(ANSIBLE_INVENTORY_PATH)

        total_hosts = sum(len(clients) for clients in fleets.values())
        logger.info(f"Updated Ansible inventory with {len(fleets)} fleet(s), {total_hosts} host(s)")

    except Exception as e:
        logger.error(f"Failed to update Ansible inventory: {e}", exc_info=True)
        raise
