"""An unreadable cluster registry must not look like a registry with nothing in it.

A sealed Vault answers 503 to LIST; `vault_list` raises and `list_clusters` returns an empty
list, which reads exactly like "Found 0 registered clusters in Vault". Handed to
`sync_registered_clusters`, that list would empty the in-memory whitelist `get_node_names()`
intersects live clients against, so every cluster would vanish from the API until a restart.

`list_clusters` still returns a list, because many callers depend on that, but records the
failure, so startup can refuse to overwrite the whitelist with a list it never received and
`/health` can report the condition without changing its status code.
"""

import pytest

from app.core import vault_status
from app.core.vault_client import VaultClientError
from app.services.lxd_certs.storage import VaultStorageMixin


class _Vault:
    """Stands in for the Vault client. `vault_list` is the only method under test."""

    def __init__(self, result=None, error: Exception | None = None):
        self._result = result if result is not None else []
        self._error = error
        self.calls = 0

    def vault_list(self, path, use_cache: bool = True):
        self.calls += 1
        if self._error:
            raise self._error
        return self._result


class _Storage(VaultStorageMixin):
    def __init__(self, vault):
        self.vault = vault
        self.clusters_path = "secret/data/clusters"


@pytest.fixture(autouse=True)
def _clean_status(monkeypatch):
    vault_status.reset()
    # The org-scope filter reads a ContextVar only set inside a request.
    monkeypatch.setattr(
        "app.core.org_scope.apply_cluster_filter", lambda ids: ids, raising=False
    )
    yield
    vault_status.reset()


def test_nothing_attempted_yet_is_not_a_failure():
    """None until the first read. A worker that has not read the registry has not found it
    broken, and reporting it as broken would make every fresh process look degraded."""
    assert vault_status.registry_readable() is None
    assert vault_status.snapshot()["registry_readable"] is None


def test_a_real_empty_registry_reads_as_readable():
    storage = _Storage(_Vault(result=[]))

    assert storage.list_clusters() == []
    assert vault_status.registry_readable() is True, (
        "Vault answered and said zero, which is a legitimate fresh install"
    )


def test_a_sealed_vault_is_recorded_as_unreadable():
    storage = _Storage(_Vault(error=VaultClientError("Vault list failed: 503 Server Error")))

    assert storage.list_clusters() == [], "the contract still returns a list, not a raise"
    assert vault_status.registry_readable() is False, (
        "without this the empty list above is indistinguishable from an empty registry"
    )


def test_the_recorded_error_names_the_cause_without_leaking_the_address():
    storage = _Storage(_Vault(error=VaultClientError("Vault list failed at https://vault:8200")))
    storage.list_clusters()

    error = vault_status.snapshot()["error"] or ""
    assert "VaultClientError" in error, "the class name is what tells an operator what broke"
    assert len(error) <= 200, "this reaches an unauthenticated endpoint, so it stays short"


def test_a_later_success_clears_the_condition():
    """A condition, never a latch: one failed read must not mark a healthy deployment degraded
    until restart."""
    vault = _Vault(error=VaultClientError("sealed"))
    storage = _Storage(vault)
    storage.list_clusters()
    assert vault_status.registry_readable() is False

    storage.vault = _Vault(result=["nuc2", "central2"])
    assert storage.list_clusters() == ["nuc2", "central2"]
    assert vault_status.registry_readable() is True


def test_trailing_slashes_are_stripped_on_the_success_path():
    """Vault LIST returns keys with a trailing slash; the whitelist compares exact ids."""
    storage = _Storage(_Vault(result=["nuc2/", "central2/"]))
    assert storage.list_clusters() == ["nuc2", "central2"]


def test_the_snapshot_shape_is_what_health_renders():
    storage = _Storage(_Vault(error=VaultClientError("sealed")))
    storage.list_clusters()

    snap = vault_status.snapshot()
    assert set(snap) == {"registry_readable", "registry_source", "error", "checked_at"}
    assert snap["checked_at"] is not None


def test_startup_refuses_to_sync_a_whitelist_it_never_received():
    """An unreadable registry must leave the whitelist alone, because emptying it hides every
    cluster instead of reporting the fault. Reproduces the startup branch, since importing the
    lifespan would need a live app."""
    from app.services.lxd.connection import LXDConnectionManager

    manager = LXDConnectionManager()
    manager.sync_registered_clusters(["nuc2", "central2"])
    assert manager._registered_clusters == {"nuc2", "central2"}

    storage = _Storage(_Vault(error=VaultClientError("sealed")))
    cluster_ids = storage.list_clusters()

    # This is the guard main.py applies.
    if vault_status.registry_readable() is not False:
        manager.sync_registered_clusters(cluster_ids)

    assert manager._registered_clusters == {"nuc2", "central2"}, (
        "an unreadable registry emptied the whitelist, so get_node_names() intersected against "
        "the empty set and every cluster vanished from the API"
    )


def test_the_health_body_never_carries_the_vault_address():
    """/health is unauthenticated. A requests exception string names the URL it failed on and
    the pool's host and port; neither belongs in a body anyone can read."""
    for raw in (
        "VaultClientError: Vault list failed: 503 Server Error: Service Unavailable for url: http://vault.prod.internal:8200/v1/secret/metadata/clusters",
        "VaultClientError: Vault list failed: HTTPConnectionPool(host='10.0.1.4', port=8200): Read timed out. (read timeout=10)",
        "VaultClientError: Vault list failed: 403 Client Error: Forbidden for url: https://vault:8200/v1/secret/metadata/clusters",
    ):
        vault_status.mark_unreadable(raw)
        shown = vault_status.snapshot()["error"]
        assert "8200" not in shown and "http" not in shown and "10.0.1.4" not in shown, shown
        assert shown.startswith("VaultClientError"), shown
        assert "Vault list failed" in shown
    vault_status.mark_unreadable("")
    assert vault_status.snapshot()["error"] == "unknown"
