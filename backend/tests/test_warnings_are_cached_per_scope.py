"""Both warning feeds are cached per scope, dropped by a dismissal, and never cache "no warnings"
when no cluster answered.

The dashboard polls both every 60 s, and an uncached read of every cluster can take seconds
(/warnings/system/all most of all).
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.core import cache as cache_module
from app.core import org_scope
from app.core.cache import CacheManager
from app.routers import lxd_warnings
from app.routers.lxd_warnings import WarningInfo
from app.services.lxd.exceptions import LXDConnectionError
from app.services.lxd.fanout import NodeRead


def _row(uuid: str, node: str, project: str = "default") -> WarningInfo:
    return WarningInfo(uuid=uuid, location="m1", source_node=node, project=project,
                       type="t", severity="high", status="new", last_message="m")


class _Manager:
    def __init__(self, nodes):
        self._nodes = list(nodes)

    def get_node_names(self):
        return list(self._nodes)

    def has_attempted(self, _host):
        return True


class _Service:
    def __init__(self, nodes):
        self.connection_manager = _Manager(nodes)

    def get_node_names(self):
        return self.connection_manager.get_node_names()


@pytest.fixture
def manager(monkeypatch):
    m = CacheManager(redis_url=None)
    monkeypatch.setattr(cache_module, "_global_cache", m)
    monkeypatch.setattr(lxd_warnings, "get_cache_manager", lambda: m)
    import app.core.config as config

    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(vault_enabled=False, vault_token=None))
    from app.models.kernel_advisory_schemas import KernelAdvisoryResponse

    async def empty(*_a, **_k):
        return KernelAdvisoryResponse()

    monkeypatch.setattr(lxd_warnings, "build_kernel_report", empty)
    return m


@pytest.fixture
def reads(monkeypatch):
    calls = []
    by_node = {"c1": NodeRead(rows=[_row("a", "c1")])}

    def fetch(_service, node):
        calls.append(node)
        result = by_node[node]
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(lxd_warnings, "_fetch_warnings_for_node", fetch)
    return SimpleNamespace(calls=calls, by_node=by_node)


def _list(fresh=False):
    return asyncio.run(lxd_warnings.list_warnings(host=None, lxd_service=_Service(["c1"]), fresh=fresh))


def _system(nodes=("c1",), fresh=False):
    return asyncio.run(lxd_warnings.get_all_system_warnings(lxd_service=_Service(nodes), fresh=fresh))


def test_the_list_is_read_once_and_then_served_from_the_cache(manager, reads):
    first = _list()
    second = _list()
    assert [w.uuid for w in second.warnings] == ["a"]
    assert reads.calls == ["c1"], "the second read must come from the cache"
    assert first.collected_at is not None and second.collected_at == first.collected_at
    _list(fresh=True)
    assert reads.calls == ["c1", "c1"], "Refresh still reads the clusters"


def test_two_scopes_never_share_a_feed(manager, reads):
    org_scope.set_allowed_clusters({"c1"})
    _list()
    org_scope.set_allowed_clusters({"c1", "c9"})
    _list()
    assert reads.calls == ["c1", "c1"], "a scope must never read another scope's entry"
    org_scope.set_allowed_clusters(None)


def test_a_dismissal_drops_both_feeds(manager, reads):
    _list()
    _system()
    manager.invalidate_resource_cache("warnings")
    _list()
    _system()
    assert reads.calls == ["c1", "c1", "c1", "c1"]


def test_the_system_feed_is_cached_when_a_cluster_answered(manager, reads):
    first = _system()
    second = _system()
    assert reads.calls == ["c1"]
    assert second["collected_at"] == first["collected_at"]
    assert [w["uuid"] for w in second["lxd_warnings"]] == ["a"]


def test_a_feed_nobody_answered_is_never_remembered(manager, reads):
    reads.by_node["c1"] = LXDConnectionError("down")
    body = _system()
    assert body["total"] == 0 and body["unreachable_nodes"] == ["c1"]
    _system()
    assert reads.calls == ["c1", "c1"], "an unanswered, unswept feed must be asked again"


def test_an_lxd_error_answer_keeps_the_list_out_of_the_cache(manager, reads):
    from tests.test_a_failed_listing_is_never_an_empty_answer import _lxd_error

    reads.by_node["c1"] = _lxd_error(500, "boom")
    _list()
    _list()
    assert reads.calls == ["c1", "c1"]
