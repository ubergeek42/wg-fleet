from pathlib import Path
from models import Client
import logging

logger = logging.getLogger(__name__)

HOSTS_FILE_PATH = "/run/fleet_hosts"

def regenerate_hosts_file(config, session_factory, hosts_path: str = None):
    """
    Regenerate /run/fleet_hosts from database.

    Format: <ipv6> <hostname>.<fleet>.<domain>
    Only includes clients with hostnames set.

    Args:
        config: Config object with domain field
        session_factory: SQLAlchemy session factory
        hosts_path: Optional custom path for hosts file (for testing)
    """
    if hosts_path is None:
        hosts_path = HOSTS_FILE_PATH

    try:
        lines = []

        with session_factory() as session:
            # Query all clients with hostnames
            clients = session.query(Client).filter(
                Client.hostname.isnot(None)
            ).all()

            for client in clients:
                fqdn = f"{client.hostname}.{client.fleet_id}.{config.domain}"
                lines.append(f"{client.assigned_ip} {fqdn}")

        # Write atomically (write to temp, then rename)
        temp_path = Path(f"{hosts_path}.tmp")
        with temp_path.open('w') as f:
            f.write('\n'.join(lines) + '\n')

        temp_path.rename(hosts_path)
        logger.info(f"Regenerated hosts file with {len(lines)} entries")

    except Exception as e:
        logger.error(f"Failed to regenerate hosts file: {e}", exc_info=True)
        raise
