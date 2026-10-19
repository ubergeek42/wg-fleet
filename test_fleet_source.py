import json
import urllib.error
from datetime import datetime, UTC
from unittest.mock import MagicMock

import pytest

from config import Config
from fleet_source import (FleetSource, FleetsDocumentError, parse_fleets_document,
                          verify_signature)

SERVER = 'mgmt.example.org'
TOKEN_HASH = '9f2c' + '0' * 60


def fleet_entry(**overrides):
    entry = {
        'server': SERVER,
        'node': 'BigSouth2026/MidAtlantic',
        'subnet': 'fd00:a0a8:34d:4210::/64',
        'ip6': 'fd00:a0a8:34d:4210::1',
        'port': 51830,
        'valid_until': '2026-11-09T00:00:00Z',
        'sites': {'BigSouth2026/MidAtlantic/VirginiaTech': {'token_sha256': TOKEN_HASH}},
        'admin_principals': ['node:BigSouth2026', 'node:BigSouth2026/MidAtlantic'],
    }
    entry.update(overrides)
    return entry


def document(revision=1, fleets=None, **extra):
    doc = {
        'version': 1,
        'generated_by': 'contestforge',
        'revision': revision,
        'user_ca_keys': ['ssh-ed25519 AAAAuserca'],
        'fleets': fleets if fleets is not None else {'bs26-midatlantic': fleet_entry()},
    }
    doc.update(extra)
    return json.dumps(doc, indent=2).encode()


def make_config(signer):
    return Config(domain='icpcnet.internal', prune_timeout='30m', fleets={},
                  fleets_url='https://bucket.example.org/v2/fleets.json',
                  config_signers=signer.allowed_signers, server_name=SERVER,
                  external_ip='203.0.113.5')


class FakeBucket:
    """Stands in for urlopen: serves fleets.json and fleets.json.sig with ETags"""

    def __init__(self, data=None, signature=None):
        self.data = data
        self.signature = signature
        self.requests = []

    def publish(self, data, signer):
        self.data = data
        self.signature = signer.sign(data)

    def get(self, url, etag=None):
        self.requests.append(url)
        if self.data is None:
            raise urllib.error.URLError('connection refused')
        body = self.signature if url.endswith('.sig') else self.data
        tag = f'"{hash(body)}"'
        if etag == tag:
            raise urllib.error.HTTPError(url, 304, 'Not Modified', {}, None)
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = body
        response.headers = {'ETag': tag}
        return response


@pytest.fixture
def source(signer, tmp_path):
    bucket = FakeBucket()
    fleet_source = FleetSource(make_config(signer), cache_path=str(tmp_path / 'fleets.json'))
    fleet_source._get = bucket.get
    return fleet_source, bucket


def test_verify_signature(signer):
    """Test a signature verifies only for the exact bytes it was made over"""
    data = document()
    signature = signer.sign(data)

    assert verify_signature(data, signature, signer.allowed_signers, 'contestforge')
    assert not verify_signature(data + b' ', signature, signer.allowed_signers, 'contestforge')
    assert not verify_signature(data, signature, signer.allowed_signers, 'someone-else')
    assert not verify_signature(data, signature, [], 'contestforge')


def test_verify_signature_rejects_other_key(signer, tmp_path):
    """Test a signature by a key that isn't in allowed_signers is refused"""
    import subprocess
    other = tmp_path / 'other'
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(other)], check=True)
    data = document()
    signature = subprocess.run(['ssh-keygen', '-q', '-Y', 'sign', '-n', 'contestforge', '-f', str(other)],
                               input=data, capture_output=True, check=True).stdout

    assert not verify_signature(data, signature, signer.allowed_signers, 'contestforge')


def test_parse_keeps_only_our_fleets():
    """Test fleets for other servers are ignored"""
    doc = parse_fleets_document(document(fleets={
        'ours': fleet_entry(),
        'theirs': fleet_entry(server='other.example.org', port=51831,
                              subnet='fd00:a0a8:34d:4211::/64', ip6='fd00:a0a8:34d:4211::1'),
    }), SERVER, '203.0.113.5')

    assert list(doc.fleets) == ['ours']
    fleet = doc.fleets['ours']
    assert fleet.static is False
    assert fleet.interface.startswith('wgf') and len(fleet.interface) == 11
    assert fleet.external_ip == '203.0.113.5'
    assert fleet.valid_until == datetime(2026, 11, 9, tzinfo=UTC)
    assert fleet.sites == {'BigSouth2026/MidAtlantic/VirginiaTech': TOKEN_HASH}
    assert fleet.admin_principals == ['node:BigSouth2026', 'node:BigSouth2026/MidAtlantic']
    assert doc.revision == 1
    assert doc.user_ca_keys == ['ssh-ed25519 AAAAuserca']


@pytest.mark.parametrize('name, overrides', [
    ('1starts-with-digit', {}),
    ('Upper', {}),
    ('a' * 33, {}),
    ('ok', {'port': 'x'}),
    ('ok', {'subnet': 'fd00::1/64'}),
    ('ok', {'valid_until': '2026-11-09T00:00:00'}),   # no timezone
    ('ok', {'node': None}),
    ('ok', {'admin_principals': 'node:*'}),
])
def test_parse_skips_invalid_fleets(name, overrides):
    """Test an invalid fleet entry is skipped without failing the whole document"""
    doc = parse_fleets_document(document(fleets={
        name: fleet_entry(**overrides),
        'good': fleet_entry(port=51831, subnet='fd00:a0a8:34d:4211::/64', ip6='fd00:a0a8:34d:4211::1'),
    }), SERVER, '203.0.113.5')

    assert list(doc.fleets) == ['good']


def test_parse_skips_sites_without_valid_token_hash():
    """Test a site without a usable token hash can't be registered to"""
    doc = parse_fleets_document(document(fleets={'f': fleet_entry(sites={
        'A': {'token_sha256': 'short'},
        'B': {},
        'C': {'token_sha256': TOKEN_HASH},
    })}), SERVER, '203.0.113.5')

    assert doc.fleets['f'].sites == {'C': TOKEN_HASH}


@pytest.mark.parametrize('data', [
    b'not json',
    b'[]',
    document().replace(b'"version": 1', b'"version": 2'),
    document().replace(b'"revision": 1', b'"revision": "1"'),
    document().replace(b'"revision": 1', b'"revision": true'),
    document(user_ca_keys='ssh-ed25519 AAAA'),
])
def test_parse_rejects_bad_documents(data):
    """Test documents that are unusable as a whole are rejected"""
    with pytest.raises(FleetsDocumentError):
        parse_fleets_document(data, SERVER, '203.0.113.5')


def test_fetch_accepts_and_caches(source, signer, tmp_path):
    """Test a valid file is accepted and saved with its signature"""
    fleet_source, bucket = source
    bucket.publish(document(revision=5), signer)

    doc = fleet_source.fetch()

    assert doc is not None and doc.revision == 5
    assert fleet_source.document is doc
    assert (tmp_path / 'fleets.json').read_bytes() == bucket.data
    assert (tmp_path / 'fleets.json.sig').read_bytes() == bucket.signature


def test_fetch_unchanged_returns_none(source, signer):
    """Test an unchanged file (304 or identical bytes) is not reprocessed"""
    fleet_source, bucket = source
    bucket.publish(document(), signer)
    assert fleet_source.fetch() is not None

    assert fleet_source.fetch() is None
    # The second fetch got a 304 and never asked for the signature
    assert bucket.requests[-1].endswith('fleets.json')


def test_fetch_refuses_bad_signature(source, signer):
    """Test a tampered file is refused and the previous document stays"""
    fleet_source, bucket = source
    bucket.publish(document(revision=1), signer)
    first = fleet_source.fetch()

    bucket.data = document(revision=2)   # signature is still the old file's
    assert fleet_source.fetch() is None
    assert fleet_source.document is first

    # Once the matching signature is uploaded, the same file is accepted
    bucket.signature = signer.sign(bucket.data)
    assert fleet_source.fetch().revision == 2


def test_fetch_refuses_older_revision(source, signer, caplog):
    """Test a replayed older file is refused, so revoked tokens can't come back"""
    fleet_source, bucket = source
    bucket.publish(document(revision=10), signer)
    fleet_source.fetch()

    bucket.publish(document(revision=9, fleets={}), signer)
    assert fleet_source.fetch() is None
    assert fleet_source.document.revision == 10
    assert 'older than the accepted revision 10' in caplog.text


def test_fetch_network_failure_keeps_document(source, signer):
    """Test an unreachable URL is logged, not raised"""
    fleet_source, bucket = source
    bucket.publish(document(), signer)
    first = fleet_source.fetch()

    bucket.data = None
    assert fleet_source.fetch() is None
    assert fleet_source.document is first


def test_load_cached(source, signer, tmp_path):
    """Test the cached copy is used on start, and its revision guards later fetches"""
    fleet_source, bucket = source
    bucket.publish(document(revision=7), signer)
    fleet_source.fetch()

    restarted = FleetSource(fleet_source.config, cache_path=str(tmp_path / 'fleets.json'))
    restarted._get = bucket.get
    assert restarted.load_cached().revision == 7

    bucket.publish(document(revision=6), signer)
    assert restarted.fetch() is None


def test_load_cached_reverifies(source, signer, tmp_path):
    """Test a cached copy no longer signed by an allowed signer is ignored"""
    fleet_source, bucket = source
    bucket.publish(document(), signer)
    fleet_source.fetch()

    fleet_source.config.config_signers = ['contestforge ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOther']
    restarted = FleetSource(fleet_source.config, cache_path=str(tmp_path / 'fleets.json'))
    assert restarted.load_cached() is None


def test_load_cached_missing(source):
    """Test a first start with no cached copy"""
    fleet_source, bucket = source
    assert fleet_source.load_cached() is None
    assert fleet_source.document is None


def test_database_reset_revision_is_accepted(source, signer, caplog):
    """Test contestforge's revision after a database reset (max(stored + 1, unix time)) is accepted"""
    import logging, time
    fleet_source, bucket = source
    bucket.publish(document(revision=int(time.time()) - 3600), signer)
    fleet_source.fetch()

    # The reset loses the stored revision; the next one is the current time, still higher
    bucket.publish(document(revision=int(time.time())), signer)
    with caplog.at_level(logging.INFO):
        assert fleet_source.fetch() is not None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
