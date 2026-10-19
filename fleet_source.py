"""
Fleets from a signed fleets.json.

contestforge publishes fleets.json with a detached `ssh-keygen -Y sign` signature
(fleets.json.sig) to a static bucket. Neither the bucket nor the network is trusted:
every file is verified against the configured allowed_signers before use, and a file
whose revision is lower than the last accepted one is refused, so an old signed file
can't be replayed to bring back a revoked token or a removed fleet.

The last accepted file and its signature are kept on disk, so wg-fleet starts with
the same fleets when the URL is unreachable.
"""
from dataclasses import dataclass, field
from datetime import datetime, UTC
from pathlib import Path
from typing import Dict, List, Optional
import json
import logging
import os
import re
import tempfile
import urllib.error
import urllib.request

from command import run_command, CommandError
from config import FleetConfig, URL_FLEET_NAME, interface_name, validate_fleet_addresses

logger = logging.getLogger(__name__)

CACHE_PATH = "/var/lib/wg-fleet/fleets.json"
SIGNATURE_NAMESPACE = "contestforge"
MAX_DOCUMENT_SIZE = 16 * 1024 * 1024
TOKEN_HASH = re.compile(r'[0-9a-f]{64}')


class FleetsDocumentError(ValueError):
    """fleets.json is malformed as a whole (individual bad fleets are skipped instead)"""


@dataclass
class FleetsDocument:
    """A verified fleets.json, reduced to this server's fleets"""
    revision: int
    user_ca_keys: List[str] = field(default_factory=list)
    fleets: Dict[str, FleetConfig] = field(default_factory=dict)


def verify_signature(data: bytes, signature: bytes, signers: List[str], signer_id: str,
                     namespace: str = SIGNATURE_NAMESPACE) -> bool:
    """
    Check a detached `ssh-keygen -Y sign` signature over data.

    Args:
        data: The exact bytes that were signed
        signature: The armored signature
        signers: allowed_signers lines
        signer_id: Principal the signature must be from
        namespace: Signature namespace (contestforge for published files)

    Returns:
        True if the signature is valid and from an allowed signer
    """
    if not signers:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        signers_path = Path(tmp) / 'allowed_signers'
        signers_path.write_text('\n'.join(signers) + '\n')
        sig_path = Path(tmp) / 'fleets.json.sig'
        sig_path.write_bytes(signature)
        try:
            run_command([
                'ssh-keygen', '-Y', 'verify',
                '-f', str(signers_path),
                '-I', signer_id,
                '-n', namespace,
                '-s', str(sig_path),
            ], input_data=data, quiet=True)
            return True
        except CommandError:
            return False


def parse_timestamp(value) -> datetime:
    """Parse an ISO 8601 timestamp such as 2026-11-09T00:00:00Z as an aware UTC datetime"""
    if not isinstance(value, str):
        raise ValueError(f"not a timestamp: {value!r}")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp has no timezone: {value}")
    return parsed.astimezone(UTC)


def parse_fleet(name: str, entry: dict, external_ip: str) -> FleetConfig:
    """
    Build a FleetConfig from one fleets.json entry.

    Raises:
        ValueError: If the entry is invalid
    """
    if not URL_FLEET_NAME.fullmatch(name):
        raise ValueError(f"Fleet name '{name}' is not a valid fleet name")
    if not isinstance(entry, dict):
        raise ValueError(f"Fleet '{name}' is not an object")

    for key in ('ip6', 'subnet', 'port', 'node', 'valid_until'):
        if key not in entry:
            raise ValueError(f"Fleet '{name}' missing required field: {key}")
    validate_fleet_addresses(name, entry['ip6'], entry['subnet'], entry['port'], external_ip)

    if not isinstance(entry['node'], str) or not entry['node']:
        raise ValueError(f"Fleet '{name}': node must be a string")
    try:
        valid_until = parse_timestamp(entry['valid_until'])
    except ValueError as e:
        raise ValueError(f"Fleet '{name}': invalid valid_until: {e}")

    sites = {}
    for site, site_entry in (entry.get('sites') or {}).items():
        token_hash = site_entry.get('token_sha256') if isinstance(site_entry, dict) else None
        if not isinstance(token_hash, str) or not TOKEN_HASH.fullmatch(token_hash):
            logger.error(f"Fleet '{name}': ignoring site '{site}' without a valid token_sha256")
            continue
        sites[site] = token_hash

    principals = entry.get('admin_principals') or []
    if not isinstance(principals, list) or not all(isinstance(p, str) for p in principals):
        raise ValueError(f"Fleet '{name}': admin_principals must be a list of strings")

    return FleetConfig(
        ip6=entry['ip6'],
        subnet=entry['subnet'],
        external_ip=external_ip,
        port=entry['port'],
        interface=interface_name(name, static=False),
        static=False,
        node=entry['node'],
        valid_until=valid_until,
        sites=sites,
        admin_principals=principals,
    )


def parse_fleets_document(data: bytes, server_name: str, external_ip: str) -> FleetsDocument:
    """
    Parse a verified fleets.json, keeping only fleets whose server is ours.

    A fleet entry that doesn't validate is logged and skipped, so one bad entry
    doesn't take the other fleets down.

    Raises:
        FleetsDocumentError: If the document as a whole is unusable
    """
    try:
        doc = json.loads(data)
    except ValueError as e:
        raise FleetsDocumentError(f"fleets.json is not valid JSON: {e}")
    if not isinstance(doc, dict):
        raise FleetsDocumentError("fleets.json is not a JSON object")
    if doc.get('version') != 1:
        raise FleetsDocumentError(f"Unsupported fleets.json version: {doc.get('version')!r}")

    revision = doc.get('revision')
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise FleetsDocumentError(f"fleets.json has no valid revision: {revision!r}")

    user_ca_keys = doc.get('user_ca_keys') or []
    if not isinstance(user_ca_keys, list) or not all(isinstance(k, str) for k in user_ca_keys):
        raise FleetsDocumentError("fleets.json user_ca_keys must be a list of strings")

    entries = doc.get('fleets') or {}
    if not isinstance(entries, dict):
        raise FleetsDocumentError("fleets.json fleets must be an object")

    fleets = {}
    for name, entry in entries.items():
        if not isinstance(entry, dict) or entry.get('server') != server_name:
            continue
        try:
            fleets[name] = parse_fleet(name, entry, external_ip)
        except ValueError as e:
            logger.error(f"Ignoring fleet from fleets.json: {e}")

    return FleetsDocument(revision=revision, user_ca_keys=user_ca_keys, fleets=fleets)


def read_limited(response) -> bytes:
    """Read an HTTP response body, refusing anything implausibly large"""
    data = response.read(MAX_DOCUMENT_SIZE + 1)
    if len(data) > MAX_DOCUMENT_SIZE:
        raise ValueError(f"Response larger than {MAX_DOCUMENT_SIZE} bytes")
    return data


def write_atomic(path: Path, data: bytes) -> None:
    """Write a file via a temporary file and rename, so readers never see half of it"""
    temp_path = path.with_name(path.name + '.tmp')
    temp_path.write_bytes(data)
    os.replace(temp_path, path)


class FleetSource:
    """
    Fetches, verifies and caches fleets.json.

    Attributes:
        document: The last accepted document, or None if there's none yet
    """

    def __init__(self, config, cache_path: str = CACHE_PATH, timeout: float = 10):
        self.config = config
        self.cache_path = Path(cache_path)
        self.sig_path = Path(cache_path + '.sig')
        self.timeout = timeout
        self.document: Optional[FleetsDocument] = None
        self.data: Optional[bytes] = None
        self.etag: Optional[str] = None

    def _verify_and_parse(self, data: bytes, signature: bytes) -> FleetsDocument:
        if not verify_signature(data, signature, self.config.config_signers,
                                self.config.config_signer_id):
            raise FleetsDocumentError("fleets.json signature is not valid for config_signers")
        return parse_fleets_document(data, self.config.server_name, self.config.external_ip)

    def load_cached(self) -> Optional[FleetsDocument]:
        """
        Load the last accepted fleets.json from disk.

        It's verified again, since config_signers may have changed since it was saved.

        Returns:
            The document, or None if there's no usable cached copy
        """
        if not self.cache_path.exists() or not self.sig_path.exists():
            logger.info(f"No cached fleets.json at {self.cache_path}")
            return None
        data = self.cache_path.read_bytes()
        try:
            document = self._verify_and_parse(data, self.sig_path.read_bytes())
        except FleetsDocumentError as e:
            logger.error(f"Ignoring cached {self.cache_path}: {e}")
            return None
        self.document = document
        self.data = data
        logger.info(f"Loaded cached fleets.json revision {document.revision} "
                    f"with {len(document.fleets)} fleet(s) for this server")
        return document

    def _get(self, url: str, etag: Optional[str] = None):
        headers = {'User-Agent': 'wg-fleet'}
        if etag:
            headers['If-None-Match'] = etag
        request = urllib.request.Request(url, headers=headers)
        return urllib.request.urlopen(request, timeout=self.timeout)

    def fetch(self) -> Optional[FleetsDocument]:
        """
        Fetch fleets.json and its signature, and accept it if it's valid and newer.

        Blocking; run it off the event loop. Never raises for network or validation
        problems: those are logged, and the previous document stays in use.

        Returns:
            The newly accepted document, or None if nothing changed
        """
        url = self.config.fleets_url
        try:
            try:
                with self._get(url, self.etag) as response:
                    data = read_limited(response)
                    etag = response.headers.get('ETag')
            except urllib.error.HTTPError as e:
                if e.code == 304:
                    return None
                raise
            if data == self.data:
                self.etag = etag
                return None
            with self._get(url + '.sig') as response:
                signature = read_limited(response)
        except (OSError, ValueError) as e:
            # URLError and timeouts are OSErrors
            logger.warning(f"Could not fetch {url}: {e}")
            return None

        try:
            document = self._verify_and_parse(data, signature)
        except FleetsDocumentError as e:
            # The .sig may simply not be uploaded yet, so try this file again next time
            logger.error(f"Refusing fleets.json from {url}: {e}")
            return None

        if self.document is not None and document.revision < self.document.revision:
            logger.error(
                f"Refusing fleets.json from {url}: revision {document.revision} is older than "
                f"the accepted revision {self.document.revision}. If contestforge's database was "
                f"reset, delete {self.cache_path} and restart wg-fleet."
            )
            # The same bytes will be refused again, so don't download them every time
            self.etag = etag
            return None

        try:
            write_atomic(self.cache_path, data)
            write_atomic(self.sig_path, signature)
        except OSError as e:
            logger.error(f"Could not save fleets.json to {self.cache_path}: {e}")

        self.document = document
        self.data = data
        self.etag = etag
        logger.info(f"Accepted fleets.json revision {document.revision} "
                    f"with {len(document.fleets)} fleet(s) for this server")
        return document
