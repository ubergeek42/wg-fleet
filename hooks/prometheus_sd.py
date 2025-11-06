"""
Prometheus service discovery hook.

Generates Prometheus file-based service discovery JSON when clients change.
"""
from hook_manager import register_hook, HookContext, EventType
from models import Client
from pathlib import Path
import json
import logging

logger = logging.getLogger(__name__)

PROMETHEUS_TARGETS_PATH = "/run/prometheus_targets.json"


@register_hook
def prometheus_sd_hook(context: HookContext):
    """
    Generate Prometheus file-based service discovery targets.

    Creates a JSON file with targets for all clients with hostnames.
    Format follows Prometheus file_sd_config specification.

    Target format: [<ipv6>]:9100 (node_exporter port)
    Labels: hostname, fleet, job
    """
    # Filter events - only regenerate on client changes
    if context.event_type not in [
        EventType.STARTUP,
        EventType.CLIENT_ADDED,
        EventType.CLIENT_HOSTNAME_CHANGED,
        EventType.CLIENT_REMOVED
    ]:
        return

    try:
        targets = []

        with context.session_factory() as session:
            # Query all clients with hostnames
            clients = session.query(Client).filter(
                Client.hostname.isnot(None)
            ).all()

            for client in clients:
                targets.append({
                    'targets': [f'[{client.assigned_ip}]:9100'],
                    'labels': {
                        'job': 'node_exporter',
                        'hostname': client.hostname,
                        'fleet': client.fleet_id
                    }
                })

        # Write atomically (write to temp, then rename)
        temp_path = Path(f"{PROMETHEUS_TARGETS_PATH}.tmp")
        with temp_path.open('w') as f:
            json.dump(targets, f, indent=2)

        temp_path.rename(PROMETHEUS_TARGETS_PATH)
        logger.info(f"Updated Prometheus SD targets with {len(targets)} entries")

    except Exception as e:
        logger.error(f"Failed to update Prometheus SD targets: {e}", exc_info=True)
        raise
