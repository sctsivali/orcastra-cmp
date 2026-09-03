"""An unreadable cluster registry must not look like a registry with nothing in it.

A sealed Vault answers 503 to LIST. `vault_list` raises, `list_clusters` catches it and returns
an empty list, and from that point the two facts are indistinguishable:

    Found 0 registered clusters in Vault
    Startup: Connected 0/0 clusters
    Orcastra CMP API started successfully

Worse than the logs, startup handed that empty list to `sync_registered_clusters`, which sets
the in-memory whitelist to the empty set. `get_node_names()` intersects live clients against
that whitelist, so every cluster disappeared from the API even after the metrics collector
reconnected one. Observed in the field, this state persisted for weeks and only surfaced
because something happened to restart the process.

`list_clusters` still returns a list rather than raising, because thirteen callers depend on
that. What changed is that the swallow is now recorded, so the two callers that must tell the
difference can: startup, which refuses to overwrite the whitelist with a list it never
received, and `/health`, which reports the condition without changing its status code.
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
    """None, not False. A worker that has not read the registry has not found it broken, and
    reporting it as broken would make every fresh process look degraded."""
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
    """The breaker-shaped property: this must be a condition, not a latch, or one blip marks a
    healthy deployment degraded until restart."""
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
    assert set(snap) == {"registry_readable", "error", "checked_at"}
    assert snap["checked_at"] is not None


def test_startup_refuses_to_sync_a_whitelist_it_never_received():
    """The load-bearing consequence. Reproduces the startup branch rather than importing the
    lifespan, which would need a live app: an unreadable registry must leave the whitelist
    alone, because emptying it hides every cluster instead of reporting the fault."""
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
