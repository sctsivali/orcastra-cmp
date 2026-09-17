"""A Vault LIST that is lost must answer with the fleet it last saw, not with nothing.

The create-instance form starts from GET /nodes/names, which reads the cluster registry from
Vault on every request. One lost LIST (a 2 s connect budget on a busy box, a token whose
policy denies the metadata LIST while every per-cluster read still works) used to empty the
node picker and, because every other control on the form waits for a node, the whole form.
The list came back a moment later, so the operator saw pickers that were "sometimes empty,
sometimes there", and a hard refresh did not help while the blip lasted.

The registry cannot change while Vault is unreachable, because registration and
deregistration both write to Vault first. So the last successful answer is the truth until
Vault answers again, and serving it is not a guess. The copy is kept twice: in Redis, shared
by every worker and surviving a restart, and in the process, for the case where Redis is
down as well. Only when neither holds anything does the read stay empty.

The filter is applied to the answer, never to the copy: the copy is the unfiltered fleet, so a
Partner's request cannot poison what an Admin is served later.
"""

import pytest

from app.core import vault_status
from app.core.vault_client import VaultClientError
from app.services.lxd_certs.storage import VaultStorageMixin

LIST_PATH = "secret/metadata/clusters"


class _Vault:
    """Stands in for the Vault client: the LIST plus the last-good memo it keeps."""

    def __init__(self, result=None, error: Exception | None = None, memo=None):
        self._result = result if result is not None else []
        self._error = error
        self.memo = dict(memo or {})
        self.calls = 0

    def vault_list(self, path, use_cache: bool = True):
        self.calls += 1
        if self._error:
            raise self._error
        self.memo[path] = list(self._result)
        return self._result

    def last_known_list(self, path):
        return self.memo.get(path)

    def forget_last_known_list(self, path):
        self.memo.pop(path, None)


class _Storage(VaultStorageMixin):
    def __init__(self, vault):
        self.vault = vault
        self.clusters_path = "secret/data/clusters"


@pytest.fixture(autouse=True)
def _clean_status():
    vault_status.reset()
    yield
    vault_status.reset()


@pytest.fixture
def unfiltered(monkeypatch):
    monkeypatch.setattr("app.core.org_scope.apply_cluster_filter", lambda ids: ids)


def test_a_lost_list_serves_the_last_known_copy_and_marks_it_unreadable(unfiltered):
    vault = _Vault(error=VaultClientError("Vault list failed: read timed out"), memo={LIST_PATH: ["i48/", "ebi-01/"]})
    storage = _Storage(vault)

    assert storage.list_clusters() == ["i48", "ebi-01"]
    assert vault_status.registry_readable() is False
    assert vault_status.snapshot()["registry_source"] == "last_known"


def test_a_successful_list_remembers_the_fleet_in_the_process_too(unfiltered):
    storage = _Storage(_Vault(result=["i48/", "ebi-01/"]))

    assert storage.list_clusters() == ["i48", "ebi-01"]
    assert vault_status.last_known_registry() == ["i48", "ebi-01"]
    assert vault_status.snapshot()["registry_source"] == "vault"


def test_the_process_copy_covers_a_vault_and_redis_that_are_both_down(unfiltered):
    good = _Storage(_Vault(result=["i48/"]))
    good.list_clusters()

    lost = _Storage(_Vault(error=VaultClientError("Vault list failed: connection refused")))
    assert lost.list_clusters() == ["i48"], "Redis memo absent, process memo answers"
    assert vault_status.registry_readable() is False


def test_the_stored_copy_is_the_unfiltered_fleet_and_the_answer_is_filtered(monkeypatch):
    monkeypatch.setattr(
        "app.core.org_scope.apply_cluster_filter", lambda ids: [i for i in ids if i == "i48"]
    )
    storage = _Storage(_Vault(result=["i48/", "ebi-01/"]))

    assert storage.list_clusters() == ["i48"]
    assert vault_status.last_known_registry() == ["i48", "ebi-01"], (
        "a Partner's request must not narrow what the memo holds for everyone"
    )

    lost = _Storage(_Vault(error=VaultClientError("lost")))
    assert lost.list_clusters() == ["i48"], "the memo is filtered on the way out too"


def test_nothing_known_still_returns_an_empty_list(unfiltered):
    storage = _Storage(_Vault(error=VaultClientError("Vault list failed: 503")))

    assert storage.list_clusters() == []
    assert vault_status.registry_readable() is False
    assert vault_status.snapshot()["registry_source"] is None


def _thread_read(storage):
    """What a worker thread (the metrics sweep, run_blocking, to_thread) sees."""
    from app.core.org_scope import apply_cluster_filter

    return apply_cluster_filter(storage.read_registry(True, on_loop=False).cluster_ids)


def test_an_unreadable_registry_is_not_re_probed_for_a_while(unfiltered, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(vault_status.time, "time", lambda: now[0])
    vault = _Vault(error=VaultClientError("lost"), memo={LIST_PATH: ["i48/"]})
    storage = _Storage(vault)

    assert _thread_read(storage) == ["i48"]
    assert vault.calls == 1
    for _ in range(5):
        assert _thread_read(storage) == ["i48"]
    assert vault.calls == 1, "five reads inside the hold cost no Vault round trips"

    now[0] += vault_status.RETRY_HOLD_SECONDS + 0.1
    _thread_read(storage)
    assert vault.calls == 2, "after the hold the next thread read probes Vault again"


def test_a_caller_on_the_event_loop_waits_longer_and_never_pays_the_budget_mid_outage(unfiltered, monkeypatch):
    """The sweep probes from a thread every 30 s and refreshes the failure time, so a request
    on the loop is served the memo for the whole outage; if the sweep were not running, the
    loop would probe at most once every LOOP_RETRY_HOLD_SECONDS."""
    now = [1000.0]
    monkeypatch.setattr(vault_status.time, "time", lambda: now[0])
    vault = _Vault(error=VaultClientError("lost"), memo={LIST_PATH: ["i48/"]})
    storage = _Storage(vault)

    assert _thread_read(storage) == ["i48"]          # the sweep records the outage
    assert vault.calls == 1
    now[0] += vault_status.RETRY_HOLD_SECONDS + 5     # past the thread hold
    assert storage.list_clusters() == ["i48"]        # a request in the main thread
    assert vault.calls == 1, "the loop caller did not probe"
    now[0] += vault_status.LOOP_RETRY_HOLD_SECONDS
    storage.list_clusters()
    assert vault.calls == 2, "with no sweep refreshing the failure, the loop probes after its own hold"


def test_a_recovered_vault_lifts_the_hold_and_refreshes_the_copy(unfiltered, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(vault_status.time, "time", lambda: now[0])
    vault = _Vault(error=VaultClientError("lost"), memo={LIST_PATH: ["i48/"]})
    storage = _Storage(vault)
    _thread_read(storage)

    now[0] += vault_status.RETRY_HOLD_SECONDS + 0.1
    vault._error = None
    vault._result = ["i48/", "new/"]
    assert _thread_read(storage) == ["i48", "new"]
    assert vault_status.registry_readable() is True
    assert vault_status.last_known_registry() == ["i48", "new"]
    assert vault_status.snapshot()["registry_source"] == "vault"


def test_a_fresh_read_ignores_the_hold(unfiltered, monkeypatch):
    """Registration flows read with use_cache=False right after a Vault write; they must reach
    Vault, not be answered from the memo because an unrelated request failed a moment ago."""
    now = [1000.0]
    monkeypatch.setattr(vault_status.time, "time", lambda: now[0])
    vault = _Vault(error=VaultClientError("lost"), memo={LIST_PATH: ["i48/"]})
    storage = _Storage(vault)
    storage.list_clusters()
    assert vault.calls == 1

    vault._error = None
    vault._result = ["i48/", "new/"]
    assert storage.list_clusters(use_cache=False) == ["i48", "new"]
    assert vault.calls == 2


def test_a_deregistration_clears_the_copy():
    """invalidate_cluster_cache runs on register, deregister and regenerate, all of which need
    Vault up, so clearing the memo there is what keeps a removed cluster from coming back
    during a later outage."""
    from app.core.cache import CacheManager, LRUCache

    class _Redis:
        def __init__(self):
            self.deleted = []

        def delete(self, *keys):
            self.deleted.extend(keys)
            return len(keys)

        def keys(self, pattern):
            return []

    cache = CacheManager.__new__(CacheManager)
    cache.redis_client = _Redis()
    cache.memory_cache = LRUCache()
    cache.default_ttl = 300
    cache.sensitive_namespaces = []

    cache.invalidate_cluster_cache("i48")

    deleted = cache.redis_client.deleted
    assert any("vault_list_last_good" in k and k.endswith("secret/metadata/clusters") for k in deleted), deleted
    assert any(k.endswith("vault_list:secret/metadata/clusters") for k in deleted)
