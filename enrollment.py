"""
Authenticated registration for fleets from fleets_url.

A machine registers with its contestforge site id, the site's enrollment token (from
the site's encrypted config) and its ssh host public key. fleets.json carries only the
token's sha256, so wg-fleet never holds a token.

The token proves the machine belongs to the site, not that it owns the host key it
sends: anyone at the site could send another machine's public key. So the request
also carries a signature by the host key (host_key_signature) over
registration_message(). Only a proven host key may take over a previous record's
hostname or remove the machine's records in other fleets.
"""
from typing import Optional
import base64
import binascii
import hashlib
import hmac
import struct

from fleet_source import verify_signature

# Host key types wg-fleet will sign certificates for
HOST_KEY_TYPES = {
    'ssh-ed25519',
    'ecdsa-sha2-nistp256',
    'ecdsa-sha2-nistp384',
    'ecdsa-sha2-nistp521',
    'ssh-rsa',
}


def token_matches(token: str, token_sha256: Optional[str]) -> bool:
    """Whether a token hashes to the published sha256 (constant time)"""
    if not token_sha256:
        return False
    actual = hashlib.sha256(token.encode()).hexdigest()
    return hmac.compare_digest(actual, token_sha256)


def normalize_host_key(host_key: str) -> Optional[str]:
    """
    Check an ssh public key line and reduce it to "<type> <base64>".

    The comment is dropped. The key blob must decode and start with its own type, so
    garbage can't be stored and later handed to ssh-keygen.

    Returns:
        The normalized key, or None if it isn't a usable host key
    """
    parts = host_key.split()
    if len(parts) < 2 or parts[0] not in HOST_KEY_TYPES:
        return None
    key_type, blob_b64 = parts[0], parts[1]
    try:
        blob = base64.b64decode(blob_b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(blob) < 4:
        return None
    (type_len,) = struct.unpack('>I', blob[:4])
    if blob[4:4 + type_len] != key_type.encode():
        return None
    return f'{key_type} {blob_b64}'



HOST_KEY_SIGNATURE_NAMESPACE = 'wg-fleet-register'


def registration_message(fleet: str, site: str, host_key: str) -> bytes:
    """
    What a client signs with its host key to prove it owns host_key.

    Clients make the signature with:

        printf 'fleet=%s\nsite=%s\nhost_key=%s\n' "$FLEET" "$SITE" "<type> <base64>" |
            ssh-keygen -q -Y sign -n wg-fleet-register -f /etc/ssh/ssh_host_ed25519_key -

    host_key is the key without its comment, as sent in the request.
    """
    return f'fleet={fleet}\nsite={site}\nhost_key={host_key}\n'.encode()


def host_key_signature_valid(fleet: str, site: str, host_key: str, signature: str) -> bool:
    """Whether signature is host_key's signature over registration_message()"""
    allowed = [f'host namespaces="{HOST_KEY_SIGNATURE_NAMESPACE}" {host_key}']
    return verify_signature(registration_message(fleet, site, host_key), signature.encode(),
                            allowed, 'host', namespace=HOST_KEY_SIGNATURE_NAMESPACE)
