"""
Hosts file generation hook.

Regenerates /run/wg_fleet_hosts when clients are added/changed/removed.
"""
from hook_manager import register_hook, HookContext, EventType
from models import Client
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

HOSTS_FILE_PATH = "/run/wg_fleet_hosts"


@register_hook
def regenerate_hosts_file_hook(context: HookContext):
    """
    Regenerate /run/wg_fleet_hosts from database.

    Format: <ipv6> <hostname>.<fleet>.<domain>
    Only includes clients with hostnames set.
    """
    # Filter events - regenerate on startup and client changes
    if context.event_type not in [
        EventType.STARTUP,
        EventType.CLIENT_ADDED,
        EventType.CLIENT_HOSTNAME_CHANGED,
        EventType.CLIENT_REMOVED
    ]:
        return

    try:
        lines = []

        with context.session_factory() as session:
            # Query all clients with hostnames
            clients = session.query(Client).filter(
                Client.hostname.isnot(None)
            ).all()

            for client in clients:
                fqdn = f"{client.hostname}.{client.fleet_id}.{context.config.domain}"
                lines.append(f"{client.assigned_ip} {fqdn}")

        # Write atomically (write to temp, then rename)
        temp_path = Path(f"{HOSTS_FILE_PATH}.tmp")
        with temp_path.open('w') as f:
            f.write('\n'.join(lines) + '\n')

        temp_path.rename(HOSTS_FILE_PATH)
        logger.info(f"Regenerated hosts file with {len(lines)} entries")

    except Exception as e:
        logger.error(f"Failed to regenerate hosts file: {e}", exc_info=True)
        raise
