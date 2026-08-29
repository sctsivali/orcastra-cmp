"""No Vault path holding credential material may be mirrored into Redis.

`vault_read` decided this with a substring denylist: `/certs`, `/certificates`, `/keys`,
`/private`, `/secrets`. The API key credential path is
``secret/data/integrations/api_keys/<id>/secret``, and it defeated that list twice over. The path
contains ``api_keys``, which is ``_keys`` and not ``/keys``. And it ends in ``secret``, singular,
where the list says ``secrets``. Neither miss is exotic; both are ordinary naming.

The consequence was not theoretical. On a running deployment,
``orcastra:vault:secret/data/integrations/api_keys/<id>/secret`` was present in Redis with its
value containing ``"raw": "oas_..."`` in clear, with no Fernet prefix, for the cache TTL. Anyone
who could read Redis, whether through an unauthenticated instance on the container network, a
backup, or an SSRF, could recover every live integration's plaintext API key secret.

The second layer missed too, and for an unrelated reason: `CacheManager` encrypts the `vault`
namespace per FIELD name, and the cached dict's fields are `hash` and `raw`, neither of which
appeared in the sensitive-field list. Two independent defences, two different blind spots.

The denylist is now an allowlist. These tests pin both the shape and the specific spellings that
beat the old one, because those spellings are the evidence that a denylist was the wrong choice.
"""

import pytest

from app.core.cache import CacheManager
from app.core.vault_client import VaultClient

CLIENT = VaultClient.__new__(VaultClient)  # no network, only the pure path predicate

# The exact path that leaked, plus every sibling suffix in use across the codebase.
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
    """Named individually because they are the argument for the allowlist.

    `api_keys` is not `/keys`, and `/secret` is not `/secrets`. A denylist has to guess every
    spelling in advance; an allowlist does not have to guess at all.
    """
    assert not CLIENT._is_cacheable_path("secret/data/integrations/api_keys/oak_x/secret")
    assert not CLIENT._is_cacheable_path("secret/data/anything/api_keys/oak_x/secret")


def test_descriptor_paths_are_still_cacheable() -> None:
    """The guard must not be so blunt that every Vault read becomes a round trip.

    These two are what the running deployment actually caches, and they carry no credential.
    """
    assert CLIENT._is_cacheable_path("secret/data/clusters/nuc2/metadata")
    assert CLIENT._is_cacheable_path("secret/data/integrations/api_keys/oak_x/metadata")


def test_an_unrecognised_path_is_not_cached() -> None:
    """Fail closed: a path nobody has classified is a slower read, never a leaked credential."""
    assert not CLIENT._is_cacheable_path("secret/data/something/nobody/has/seen")
    assert not CLIENT._is_cacheable_path("")


def test_a_trailing_slash_cannot_smuggle_a_credential_path_past_the_check() -> None:
    assert not CLIENT._is_cacheable_path("secret/data/integrations/api_keys/oak_x/secret/")


def test_case_is_not_a_bypass() -> None:
    assert CLIENT._is_cacheable_path("secret/data/clusters/nuc2/METADATA")
    assert not CLIENT._is_cacheable_path("secret/data/clusters/nuc2/SECRET")


# ── the second layer ───────────────────────────────────────────────────────────────────────────

def test_the_raw_secret_field_is_recognised_as_sensitive() -> None:
    """Defence in depth: if a credential path ever is cached, the value must not be in clear.

    The field is named `raw`, which is why the original list missed it.
    """
    manager = CacheManager.__new__(CacheManager)
    assert manager._is_sensitive_field("raw"), (
        "the plaintext API key secret is stored under the field name `raw`"
    )


def test_the_vault_namespace_is_still_marked_for_encryption() -> None:
    """The field check only runs for namespaces flagged sensitive, so that flag is load-bearing."""
    manager = CacheManager(redis_url=None)
    assert "vault" in manager.sensitive_namespaces
