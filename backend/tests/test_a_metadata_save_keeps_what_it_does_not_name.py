"""A cluster metadata save must not delete the keys it does not name.

`save_cluster_metadata` writes the whole record with one `vault_kv_put`. It used to build that
record from its six parameters alone, so every save (a location edit, a rename, the region
backfill rewriting every catalogued cluster) deleted any other key stored there. Newer builds
store keys this one does not name, such as `clustered` and the pinned server certificate
fingerprint, and a rollback or a backfill on this build must leave them in place.
"""

import pytest

from app.core.vault_client import VaultClientError
from app.services.lxd_certs.storage import VaultStorageMixin
from app.services.lxd_certs.types import LXDCertManagerError

PATH = "secret/data/clusters/c1/metadata"


class _Vault:
    """Stands in for the Vault client: one stored record, every read and write recorded."""

    def __init__(self, stored=None, read_error=None, put_error=None):
        self.stored = stored
        self.read_error = read_error
        self.put_error = put_error
        self.reads = []
        self.puts = []

    def vault_read(self, path, use_cache=True):
        self.reads.append((path, use_cache))
        if self.read_error:
            raise self.read_error
        return dict(self.stored) if self.stored is not None else {}

    def vault_kv_put(self, path, data, cas=None):
        if self.put_error:
            raise self.put_error
        self.puts.append((path, dict(data)))
        return {}


class _Storage(VaultStorageMixin):
    def __init__(self, vault):
        self.vault = vault
        self.clusters_path = "secret/data/clusters"


def _save(storage, **overrides):
    args = dict(
        cluster_id="c1",
        display_name="Rack A",
        endpoint="https://192.0.2.10:8443",
        zone="Bandung",
        cluster_type="LXD",
        merchant_id="m-1",
        location={"region_id": "id-3273", "city": "Bandung"},
    )
    args.update(overrides)
    return storage.save_cluster_metadata(**args)


def test_keys_the_save_does_not_name_survive():
    vault = _Vault(stored={
        "display_name": "old",
        "endpoint": "https://192.0.2.9:8443",
        "zone": "Jakarta",
        "type": "LXD",
        "merchant_id": "m-0",
        "location": {"region_id": "id-jkb", "facility_name": "Old DC", "admin_code": "31.73"},
        "clustered": True,
        "server_cert_fingerprint": "ab" * 32,
    })

    assert _save(_Storage(vault)) is True

    (path, written), = vault.puts
    assert path == PATH
    assert written["clustered"] is True
    assert written["server_cert_fingerprint"] == "ab" * 32
    assert written["display_name"] == "Rack A"
    assert written["endpoint"] == "https://192.0.2.10:8443"
    assert written["zone"] == "Bandung"
    assert written["merchant_id"] == "m-1"
    assert written["location"] == {"region_id": "id-3273", "city": "Bandung"}, (
        "the location is replaced whole, never merged key by key: a stale admin_code or "
        "facility from the previous place must not survive a move"
    )


def test_the_read_half_bypasses_the_cache():
    vault = _Vault(stored={"display_name": "old"})

    _save(_Storage(vault))

    assert vault.reads == [(PATH, False)], (
        "a cached body merged over a fresher record would drop the very keys the merge keeps"
    )


def test_a_first_save_writes_the_named_keys():
    vault = _Vault(read_error=VaultClientError("Vault read failed: 404 Client Error"))

    _save(_Storage(vault), location=None)

    (_, written), = vault.puts
    assert written == {
        "display_name": "Rack A",
        "endpoint": "https://192.0.2.10:8443",
        "zone": "Bandung",
        "type": "LXD",
        "merchant_id": "m-1",
        "location": {},
    }


def test_a_failed_write_still_raises():
    vault = _Vault(stored={}, put_error=VaultClientError("Vault write failed: 503"))

    with pytest.raises(LXDCertManagerError):
        _save(_Storage(vault))
