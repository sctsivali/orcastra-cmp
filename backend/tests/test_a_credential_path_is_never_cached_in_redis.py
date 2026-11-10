"""No Vault path holding credential material may be mirrored into Redis.

`vault_read` caches only the paths an allowlist names. A substring denylist (`/certs`, `/keys`,
`/secrets`, ...) misses ordinary names: the API key credential path
``secret/data/integrations/api_keys/<id>/secret`` contains ``_keys`` rather than ``/keys`` and ends
in ``secret``, singular. Cached, its value would sit in Redis with ``"raw": "oas_..."`` in clear
for the cache TTL, readable through an unauthenticated Redis on the container network, a backup or
an SSRF.

As a second layer, `CacheManager` encrypts the `vault` namespace per field name, so the `raw`
field must be in its sensitive-field list.
"""

import pytest

from app.core.cache import CacheManager
from app.core.vault_client import VaultClient

CLIENT = VaultClient.__new__(VaultClient)  # no network, only the pure path predicate

# The API key credential path, plus every sibling suffix in use across the codebase.
CREDENTIAL_PATHS = [
    "secret/data/integrations/api_keys/oak_abc123/secret",
    "secret/data/lxd_keys/key-1/credentials",
    "secret/data/clusters/nuc2/certs",
    "secret/data/clusters/nuc2/secrets",
    "pki_int/certs",
]


@pytest.mark.parametrize("path", CREDENTIAL_PATHS)
def test_a_credential_path_is_not_cacheable(path: str) -> None:
    assert not CLIENT._is_cacheable_path(path), (
        f"{path} would be mirrored into Redis; only descriptor paths may be cached"
    )


def test_the_two_spellings_that_beat_the_denylist() -> None:
    """`api_keys` is not `/keys`, and `/secret` is not `/secrets`: a denylist misses both."""
    assert not CLIENT._is_cacheable_path("secret/data/integrations/api_keys/oak_x/secret")
    assert not CLIENT._is_cacheable_path("secret/data/anything/api_keys/oak_x/secret")


def test_descriptor_paths_are_still_cacheable() -> None:
    """Descriptor paths stay cached, so not every Vault read is a round trip.

    These two carry no credential.
    """
    assert CLIENT._is_cacheable_path("secret/data/clusters/nuc2/metadata")
    assert CLIENT._is_cacheable_path("secret/data/integrations/api_keys/oak_x/metadata")


def test_an_unrecognised_path_is_not_cached() -> None:
    """Fail closed: an unclassified path costs a slower read, never a leaked credential."""
    assert not CLIENT._is_cacheable_path("secret/data/something/nobody/has/seen")
    assert not CLIENT._is_cacheable_path("")


def test_a_trailing_slash_cannot_smuggle_a_credential_path_past_the_check() -> None:
    assert not CLIENT._is_cacheable_path("secret/data/integrations/api_keys/oak_x/secret/")


def test_case_is_not_a_bypass() -> None:
    assert CLIENT._is_cacheable_path("secret/data/clusters/nuc2/METADATA")
    assert not CLIENT._is_cacheable_path("secret/data/clusters/nuc2/SECRET")


# ── the second layer ───────────────────────────────────────────────────────────────────────────

def test_the_raw_secret_field_is_recognised_as_sensitive() -> None:
    """Defence in depth: if a credential path is ever cached, the value must not be in clear."""
    manager = CacheManager.__new__(CacheManager)
    assert manager._is_sensitive_field("raw"), (
        "the plaintext API key secret is stored under the field name `raw`"
    )


def test_the_vault_namespace_is_still_marked_for_encryption() -> None:
    """The field check only runs for namespaces flagged sensitive, so that flag is load-bearing."""
    manager = CacheManager(redis_url=None)
    assert "vault" in manager.sensitive_namespaces
