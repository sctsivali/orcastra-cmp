"""GET /nodes carries each Cluster's release verdict, read from the stored feeds only
(routers/nodes.py, services/hypervisor_releases/service.attach).

The verdict is attached after the 180 s cache, so a cached body still gets the current answer,
and the route never reaches the internet: the fetch is patched to fail the test if it is called.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core import config
from app.core.cache import CacheManager
from app.models.schemas import NodeStatus
from app.services.hypervisor_releases import feed, service
from app.services.release_check.store import ReleaseCheckStore
from tests._hypervisor_feeds import incus_body, lxd_body

NOW = 1_800_000_000.0


@pytest.fixture
def stored(monkeypatch):
    local = ReleaseCheckStore(redis=lambda: None, state_key=service.STATE_KEY)
    local.write({
        engine: {
            "last_attempt_at": NOW,
            "last_success_at": NOW,
            "error": None,
            "failures": 0,
            "tracks": feed.tracks_to_dict(parse(body)),
        }
        for engine, parse, body in (
            ("lxd", feed.parse_lxd, lxd_body()),
            ("incus", feed.parse_incus, incus_body()),
        )
    })
    monkeypatch.setattr(service, "get_store", lambda: local)
    monkeypatch.setattr(service.time, "time", lambda: NOW + 60)

    async def no_network(*_args, **_kwargs):
        raise AssertionError("a request path reached the internet")

    monkeypatch.setattr(service, "fetch_guarded", no_network)
    return local


@pytest.fixture
def enabled(monkeypatch):
    def apply(value: bool):
        patched = config.get_settings().model_copy(update={"hypervisor_release_check_enabled": value})
        monkeypatch.setattr(service, "get_settings", lambda: patched)
    apply(True)
    return apply


def _node(name, version, *, connected=True, node_type="LXD", lts=True):
    return NodeStatus(
        name=name,
        endpoint=f"https://{name}:8443",
        connected=connected,
        node_type=node_type,
        server_version=version,
        api_version="1.0",
        environment={"server": (node_type or "lxd").lower(), "server_lts": lts, "server_version": version},
    )


class _Fleet:
    def __init__(self, statuses):
        self.statuses = statuses
        self.cert_manager = None

    async def check_nodes_status_async(self):
        return list(self.statuses)


def _get_nodes(monkeypatch, statuses):
    from app.routers import nodes

    monkeypatch.setattr(nodes, "get_cache_manager", lambda: CacheManager(redis_url=None))
    return asyncio.run(nodes.get_nodes_status(lxd_service=_Fleet(statuses), fresh=True, _rbac=None))


def test_each_cluster_is_told_about_its_own_track(stored, enabled, monkeypatch) -> None:
    response = _get_nodes(monkeypatch, [
        _node("blitzkrieg", "5.21.5"),
        _node("console-lab", "5.21.9"),
        _node("incus-lab", "6.0.4", node_type="Incus", lts=None),
        _node("offline", "5.21.5", connected=False),
    ])
    by_name = {n.name: n.hypervisor_release for n in response.nodes}

    assert by_name["blitzkrieg"].status == "update_available"
    assert by_name["blitzkrieg"].latest_version == "5.21.9"
    assert by_name["console-lab"].status == "latest"
    assert by_name["console-lab"].newer_track.version == "6.10.1"
    assert (by_name["incus-lab"].status, by_name["incus-lab"].channel) == ("update_available", "lts-6.0")
    assert by_name["offline"] is None

    body = response.model_dump(mode="json")
    assert body["nodes"][0]["hypervisor_release"]["checked_at"].startswith("2027-01-15")


def test_turned_off_every_cluster_says_nothing(stored, enabled, monkeypatch) -> None:
    enabled(False)
    response = _get_nodes(monkeypatch, [_node("blitzkrieg", "5.21.5")])
    assert response.nodes[0].hypervisor_release is None


def test_an_unreadable_store_reads_as_not_checked_not_as_a_failure(stored, enabled, monkeypatch) -> None:
    class Broken:
        def read(self):
            raise RuntimeError("redis is down")

    monkeypatch.setattr(service, "get_store", lambda: Broken())
    response = _get_nodes(monkeypatch, [_node("blitzkrieg", "5.21.5")])
    verdict = response.nodes[0].hypervisor_release
    assert (verdict.status, verdict.reason) == ("not_checked", "feed_unreachable")


@pytest.mark.parametrize("state", [
    {"lxd": "x", "incus": [1]},
    {"lxd": {"last_success_at": 10**400, "last_attempt_at": float("nan"), "tracks": {}}},
    {"lxd": {"last_success_at": NOW, "tracks": {"5.21": {"version": "5.21.9", "lts": True, "channel": "5.21/stable", "released_at": "0001-01-01T00:00:00+05:00"}}}},
    {"lxd": {"last_success_at": "soon", "failures": "many", "tracks": ["5.21"]}},
])
def test_a_tampered_store_never_turns_the_page_into_a_500(stored, enabled, monkeypatch, state) -> None:
    # Redis is shared: whatever was written under the key, the Clusters page still answers.
    stored.write(state)
    response = _get_nodes(monkeypatch, [_node("blitzkrieg", "5.21.5")])
    verdict = response.nodes[0].hypervisor_release
    assert verdict is not None and verdict.status in ("not_checked", "update_available")
    response.model_dump(mode="json")
    assert service.due(state, NOW) in (True, False)


def test_a_clustered_card_reads_member_versions_from_the_host_facts_cache_only(stored, enabled, monkeypatch) -> None:
    from app.core import cache as cache_module
    from app.services.lxd import host_kernels

    facts = CacheManager(redis_url=None)
    facts.set("api_responses", host_kernels._cache_key("microcloud"), {
        "cluster_id": "microcloud", "clustered": True, "members_listed": True, "collected_at": "2027-01-15T08:00:00+00:00",
        "members": [
            {"member": "mc1", "server_version": "5.21.9"},
            {"member": "mc2", "server_version": "5.21.5"},
            {"member": "mc3", "server_version": "5.21.4", "error": "offline: down"},
        ],
    }, ttl=600)
    monkeypatch.setattr(cache_module, "get_cache_manager", lambda: facts)

    def no_lxd(*_args, **_kwargs):
        raise AssertionError("GET /nodes read a cluster member")

    monkeypatch.setattr(host_kernels, "read_cluster_kernels", no_lxd)

    clustered = _node("microcloud", "5.21.9")
    clustered.clustered = True
    uncached = _node("other", "5.21.9")
    uncached.clustered = True
    response = _get_nodes(monkeypatch, [clustered, uncached])
    by_name = {n.name: n.hypervisor_release for n in response.nodes}

    mixed = by_name["microcloud"]
    assert (mixed.status, mixed.mixed_versions) == ("update_available", True)
    # the offline member's stale answer is not counted
    assert [(m.member, m.version) for m in mixed.members] == [("mc2", "5.21.5"), ("mc1", "5.21.9")]
    # nothing cached: the endpoint's own version, as before
    assert (by_name["other"].status, by_name["other"].mixed_versions, by_name["other"].members) == ("latest", False, None)
