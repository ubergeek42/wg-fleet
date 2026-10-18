"""
Fleets file hook.

Writes /run/wg_fleets.json, the fleets this server serves, for the management
server's host-side script (per-fleet users, last-minute repos, wstunnel units,
firewall rules and bastion principals). wg-fleet runs in a container and can't do
those itself; a systemd .path unit on this file runs the script instead.

Removed fleets are left out, so their users lose access even while their interface
waits for its last clients to be pruned.
"""
from hook_manager import register_hook, HookContext, EventType
from pathlib import Path
import json
import logging

logger = logging.getLogger(__name__)

FLEETS_FILE_PATH = "/run/wg_fleets.json"

# Static fleets have no node in contestforge, so only global admins reach them
STATIC_ADMIN_PRINCIPALS = ["node:*"]


def render_fleets_file(config) -> dict:
    fleets = {}
    for name, fleet in sorted(config.fleets.items()):
        if fleet.removed:
            continue
        entry = {
            'static': fleet.static,
            'interface': fleet.interface,
            'port': fleet.port,
            'subnet': fleet.subnet,
            'ip6': fleet.ip6,
        }
        if fleet.static:
            entry['admin_principals'] = STATIC_ADMIN_PRINCIPALS
        else:
            entry['node'] = fleet.node
            entry['admin_principals'] = fleet.admin_principals
        fleets[name] = entry
    return {
        'version': 1,
        'user_ca_keys': config.user_ca_keys,
        'fleets': fleets,
    }


@register_hook
def fleets_file_hook(context: HookContext):
    """
    Regenerate /run/wg_fleets.json.

    Only on startup (/run is a tmpfs) and when the fleets change, so client events
    don't wake the host-side script.
    """
    if context.event_type not in (EventType.STARTUP, EventType.FLEETS_CHANGED):
        return

    try:
        content = json.dumps(render_fleets_file(context.config), indent=2) + '\n'

        # Write atomically (write to temp, then rename)
        temp_path = Path(f"{FLEETS_FILE_PATH}.tmp")
        temp_path.write_text(content)
        temp_path.rename(FLEETS_FILE_PATH)

        logger.info(f"Updated {FLEETS_FILE_PATH}")
    except Exception as e:
        logger.error(f"Failed to update {FLEETS_FILE_PATH}: {e}", exc_info=True)
        raise
