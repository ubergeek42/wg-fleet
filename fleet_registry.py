"""
The set of fleets this server serves, and their WireGuard interfaces.

Static fleets come from the config file. Fleets from fleets_url come from the signed
fleets.json (fleet_source) and change at runtime: new ones get an interface, and ones
that leave the file or pass valid_until are marked removed, stop accepting
registrations, and are torn down once their clients are gone and a grace period has
passed. A fleet's addresses never move once it has an interface, since machines keep
their WireGuard configs; KnownFleet remembers them across restarts.

config.fleets is replaced, never mutated, so a reader always sees one consistent set.
"""
from dataclasses import replace
from datetime import datetime, UTC
from pathlib import Path
from typing import Dict, Optional
import asyncio
import ipaddress
import logging

from config import FleetConfig, interface_name, parse_duration
from fleet_source import FleetSource, FleetsDocument
from hook_manager import trigger_hooks, EventType, HookContext
from models import Client, KnownFleet
import wireguard

logger = logging.getLogger(__name__)

WIREGUARD_DIR = Path('/etc/wireguard')


def as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Database timestamps are naive but represent UTC"""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def interface_config_path(fleet_config: FleetConfig) -> Path:
    return WIREGUARD_DIR / f'{fleet_config.interface}.conf'


def setup_fleet_interface(fleet_name: str, fleet_config: FleetConfig):
    """
    Setup WireGuard interface for a fleet.

    Args:
        fleet_name: Name of the fleet
        fleet_config: Fleet configuration object
    """
    config_path = interface_config_path(fleet_config)
    interface = fleet_config.interface

    # Check if config exists
    if not config_path.exists():
        logger.info(f"Creating new interface config for fleet: {fleet_name}")
        # Generate server keypair
        private_key, public_key = wireguard.generate_keypair()

        # Create config file
        wireguard.create_interface_config(interface, fleet_config, private_key, str(config_path))
    else:
        # Keep the server key, but apply address/port changes from the fleet config
        private_key = wireguard.read_private_key(str(config_path))
        if config_path.read_text() != wireguard.render_interface_config(fleet_config, private_key):
            logger.warning(
                f"Config for fleet {fleet_name} changed; rewriting {config_path.name} and restarting "
                f"the interface. Existing clients will need to re-register."
            )
            wireguard.create_interface_config(interface, fleet_config, private_key, str(config_path))
            if wireguard.interface_exists(interface):
                wireguard.bring_down_interface(interface)
        else:
            logger.info(f"Using existing interface config for fleet: {fleet_name}")

    # Bring up interface
    try:
        wireguard.bring_up_interface(interface)
    except Exception as e:
        logger.error(f"Failed to bring up interface {interface}: {e}")
        raise


def reconcile_fleet_state(fleet_name: str, fleet_config: FleetConfig, session_factory):
    """
    Reconcile database and WireGuard state.

    - Remove WireGuard peers not in database
    - Remove database clients not in WireGuard

    Args:
        fleet_name: Name of the fleet
        fleet_config: Fleet configuration object
        session_factory: SQLAlchemy session factory
    """
    logger.info(f"Reconciling state for fleet: {fleet_name}")

    # Get WireGuard peers
    wg_peers = wireguard.list_peers(fleet_config.interface)
    wg_pubkeys = {peer['public_key'] for peer in wg_peers}

    # Get database clients
    with session_factory() as session:
        db_clients = session.query(Client).filter_by(fleet_id=fleet_name).all()
        db_pubkeys = {client.public_key for client in db_clients}

        # Remove WG peers not in DB
        for pubkey in wg_pubkeys - db_pubkeys:
            logger.info(f"Removing WireGuard peer not in database: {pubkey[:16]}...")
            wireguard.remove_peer(fleet_config.interface, pubkey)

        # Remove DB clients not in WG
        for client in db_clients:
            if client.public_key not in wg_pubkeys:
                logger.info(f"Removing database client not in WireGuard: {client.assigned_ip}")
                session.delete(client)

        session.commit()


def remove_unconfigured_fleet_clients(fleets: Dict[str, FleetConfig], session_factory):
    """
    Remove database clients of fleets that aren't served any more.

    Pruning and reconciliation only look at served fleets, so these would otherwise
    stay in the generated hosts/Prometheus/Ansible files forever. Removed fleets that
    still have an interface are served, so their clients are kept until pruned.

    Args:
        fleets: Fleets currently served
        session_factory: SQLAlchemy session factory
    """
    with session_factory() as session:
        stale = session.query(Client).filter(Client.fleet_id.notin_(list(fleets))).all()
        for client in stale:
            logger.info(f"Removing client of unconfigured fleet {client.fleet_id}: {client.assigned_ip}")
            session.delete(client)
        session.commit()


def addresses_collide(a: FleetConfig, b: FleetConfig) -> bool:
    """Whether two fleets would share a port or overlapping subnets"""
    return (a.port == b.port
            or ipaddress.IPv6Network(a.subnet).overlaps(ipaddress.IPv6Network(b.subnet)))


def fleet_from_known(row: KnownFleet, external_ip: str) -> FleetConfig:
    """A fleet served only from what KnownFleet remembers (no fleets.json entry)"""
    return FleetConfig(
        ip6=row.ip6,
        subnet=row.subnet,
        external_ip=external_ip,
        port=row.port,
        interface=interface_name(row.name, static=False),
        static=False,
        removed=row.removed_at is not None,
    )


def build_url_fleets(config, document: Optional[FleetsDocument], known: Dict[str, KnownFleet],
                     now: datetime) -> Dict[str, FleetConfig]:
    """
    Decide which fleets from fleets_url to serve.

    Args:
        config: Application configuration (for the static fleets and external_ip)
        document: The accepted fleets.json, or None if there's none yet
        known: KnownFleet rows by name
        now: Current time

    Returns:
        Fleets by name, with `removed` set for ones that left the file or expired
    """
    served: Dict[str, FleetConfig] = {}

    if document is None:
        # Nothing to compare against yet (no cache and the URL is down). Keep serving
        # what we had, without tokens, and don't treat anything as removed.
        for name, row in known.items():
            served[name] = fleet_from_known(row, config.external_ip)
        return served

    # Fleets with an interface that left the file: keep them until they're torn down.
    # They come first so a new fleet reusing one's port waits until it's gone.
    for name, row in known.items():
        if name not in document.fleets and name not in config.static_fleets:
            served[name] = replace(fleet_from_known(row, config.external_ip), removed=True)

    for name, fleet in document.fleets.items():
        if name in config.static_fleets:
            logger.error(f"Ignoring fleet '{name}' from fleets.json: a static fleet has that name")
            continue

        row = known.get(name)
        expired = fleet.valid_until is not None and fleet.valid_until <= now
        if expired and row is None:
            # Never served, or already torn down: don't bring it back
            continue

        if row is not None and (row.ip6, row.subnet, row.port) != (fleet.ip6, fleet.subnet, fleet.port):
            logger.error(
                f"fleets.json moves fleet '{name}' from {row.subnet} port {row.port} to "
                f"{fleet.subnet} port {fleet.port}; ignoring the change, since machines keep "
                f"their configs"
            )
            fleet = replace(fleet, ip6=row.ip6, subnet=row.subnet, port=row.port)

        if expired:
            fleet = replace(fleet, removed=True)

        clash = next((other_name for other_name, other in
                      list(config.static_fleets.items()) + list(served.items())
                      if addresses_collide(fleet, other)), None)
        if clash is not None:
            if clash in served and served[clash].removed:
                logger.warning(f"Fleet '{name}' from fleets.json waits for removed fleet '{clash}', "
                               f"which still has its port or subnet, to be torn down")
            else:
                logger.error(f"Ignoring fleet '{name}' from fleets.json: its port or subnet "
                             f"collides with fleet '{clash}'")
            continue

        served[name] = fleet

    return served


def apply_fleets(config, session_factory, document: Optional[FleetsDocument],
                 now: Optional[datetime] = None) -> bool:
    """
    Bring config.fleets in line with the static fleets and fleets.json.

    Sets up interfaces for fleets that are new, and records them in KnownFleet.
    Run whenever a new fleets.json is accepted, and periodically (fleets expire).

    Returns:
        True if the served fleets changed
    """
    now = now or datetime.now(UTC)
    old_fleets = config.fleets
    old_ca_keys = config.user_ca_keys

    with session_factory() as session:
        known = {row.name: row for row in session.query(KnownFleet).all()}
        url_fleets = build_url_fleets(config, document, known, now)

        new_fleets = dict(config.static_fleets)
        for name, fleet in url_fleets.items():
            if name not in old_fleets and name not in known:
                try:
                    setup_fleet_interface(name, fleet)
                except Exception as e:
                    logger.error(f"Could not set up fleet '{name}', skipping it: {e}")
                    continue
            new_fleets[name] = fleet

            row = known.get(name)
            if row is None:
                row = KnownFleet(name=name, ip6=fleet.ip6, subnet=fleet.subnet, port=fleet.port)
                session.add(row)
                logger.info(f"Added fleet '{name}' ({fleet.subnet}, port {fleet.port})")
            if fleet.removed and row.removed_at is None:
                row.removed_at = now
                logger.info(f"Fleet '{name}' was removed or expired; it no longer accepts "
                            f"registrations")
            elif not fleet.removed and row.removed_at is not None and document is not None:
                row.removed_at = None
                logger.info(f"Fleet '{name}' is back in fleets.json")
        session.commit()

    config.fleets = new_fleets
    if document is not None:
        config.user_ca_keys = document.user_ca_keys
    return new_fleets != old_fleets or config.user_ca_keys != old_ca_keys


def retire_removed_fleets(config, session_factory, now: Optional[datetime] = None) -> bool:
    """
    Tear down removed fleets once their clients are gone and the grace period passed.

    Returns:
        True if any fleet was torn down
    """
    now = now or datetime.now(UTC)
    grace = parse_duration(config.removed_fleet_grace)
    retired = []

    with session_factory() as session:
        for row in session.query(KnownFleet).filter(KnownFleet.removed_at.isnot(None)).all():
            fleet = config.fleets.get(row.name)
            if fleet is not None and not fleet.removed:
                continue
            if now - as_utc(row.removed_at) < grace:
                continue
            if session.query(Client).filter_by(fleet_id=row.name).first() is not None:
                continue

            interface = interface_name(row.name, static=False)
            try:
                if wireguard.interface_exists(interface):
                    if wireguard.list_peers(interface):
                        continue
                    wireguard.bring_down_interface(interface)
                (WIREGUARD_DIR / f'{interface}.conf').unlink(missing_ok=True)
            except Exception as e:
                logger.error(f"Could not tear down fleet '{row.name}': {e}")
                continue

            session.delete(row)
            retired.append(row.name)
            logger.info(f"Tore down removed fleet '{row.name}' ({interface})")
        session.commit()

    if retired:
        config.fleets = {name: fleet for name, fleet in config.fleets.items() if name not in retired}
    return bool(retired)


def fleets_changed(config, session_factory):
    """Run the hooks that depend on the set of fleets"""
    trigger_hooks(HookContext(
        event_type=EventType.FLEETS_CHANGED,
        config=config,
        session_factory=session_factory
    ))


def initialize_fleets(config, session_factory, source: Optional[FleetSource] = None):
    """
    Bring up WireGuard interfaces, reconcile state, and run startup hooks.

    With fleets_url, starts from the cached fleets.json and tries one fetch, so a
    first start has its fleets right away. If neither works, starts with the static
    fleets (and any remembered ones) and keeps retrying in refresh_fleets.

    Args:
        config: Application configuration
        session_factory: SQLAlchemy session factory
        source: fleets.json source, if fleets_url is set
    """
    for fleet_name, fleet_config in config.static_fleets.items():
        setup_fleet_interface(fleet_name, fleet_config)

    if source is not None:
        source.load_cached()
        source.fetch()
        # Remembered fleets already have interfaces; make sure they're up
        with session_factory() as session:
            known = {row.name for row in session.query(KnownFleet).all()}
        apply_fleets(config, session_factory, source.document)
        for name in known & set(config.fleets):
            try:
                setup_fleet_interface(name, config.fleets[name])
            except Exception as e:
                logger.error(f"Could not bring up fleet '{name}': {e}")

    remove_unconfigured_fleet_clients(config.fleets, session_factory)

    for fleet_name, fleet_config in config.fleets.items():
        reconcile_fleet_state(fleet_name, fleet_config, session_factory)

    # Generate initial hosts file etc. via hook system
    trigger_hooks(HookContext(
        event_type=EventType.STARTUP,
        config=config,
        session_factory=session_factory
    ))


def refresh_fleets_once(config, session_factory, source: FleetSource, document_changed: bool):
    """Apply the current fleets.json and retire empty removed fleets; run hooks on change"""
    changed = apply_fleets(config, session_factory, source.document)
    if source.document is not None:
        changed = retire_removed_fleets(config, session_factory) or changed
    if changed or document_changed:
        fleets_changed(config, session_factory)


async def refresh_fleets(config, session_factory, source: FleetSource):
    """
    Background task that fetches fleets.json every refresh_interval.

    Applies it even when it didn't change, since fleets expire and removed fleets
    become ready to tear down over time.
    """
    interval = parse_duration(config.refresh_interval).total_seconds()
    while True:
        try:
            await asyncio.sleep(interval)
            document = await asyncio.to_thread(source.fetch)
            refresh_fleets_once(config, session_factory, source, document is not None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Error refreshing fleets: {e}", exc_info=True)
