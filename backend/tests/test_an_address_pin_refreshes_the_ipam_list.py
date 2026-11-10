"""
The IPAM list carries the pin state, the network type and the subnet, and a NIC write
refreshes it.

Every instance lifecycle invalidation clears `ipam:*`, so an address pinned in the Devices tab
shows at once rather than after the two-minute TTL. Rows are joined with the instances and
networks of their node; a node whose instances could not be read answers "unknown" and is
flagged partial (and not cached); and the Partner project bound applies.
"""

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.org_scope import org_scope_dependency
from app.core.rbac import get_current_user
from app.routers import ipam as ipam_module
from app.services.lxd_service import get_lxd_service
from app.core.cache_envelope import is_envelope

HOST = "nuc1"


class _Response:
    def __init__(self, body: Any, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def json(self) -> Any:
        return self._body


class _Node:
    def __init__(self, body: Any = None, raise_exc: Optional[Exception] = None) -> None:
        self._body = body
        self._exc = raise_exc

    def get(self, params=None, timeout=None):
        if self._exc is not None:
            raise self._exc
        return _Response({"metadata": self._body})


PROJECTS = [
    {"name": "default", "config": {}},
    {"name": "UAT-Hermes", "config": {"features.networks": "true"}},
]
NETWORKS = [
    {"name": "lxdbr0", "project": "default", "type": "bridge",
     "config": {"ipv4.address": "10.22.114.1/24", "ipv6.address": "none"}},
    {"name": "ovn-uat-hermes", "project": "UAT-Hermes", "type": "ovn",
     "config": {"ipv4.address": "192.168.3.1/24", "ipv6.address": "none"}},
]
INSTANCES = [
    {"name": "qahf-nic-test", "project": "default",
     "expanded_devices": {"eth1": {"type": "nic", "nictype": "bridged", "parent": "lxdbr0",
                                   "ipv4.address": "10.22.114.231"}}},
    {"name": "hermes-c1", "project": "UAT-Hermes",
     "expanded_devices": {"eth0": {"type": "nic", "network": "ovn-uat-hermes"}}},
]
ALLOCATIONS: Dict[str, List[Dict[str, Any]]] = {
    "default": [
        {"addresses": "10.22.114.1/24", "type": "network", "used_by": "/1.0/networks/lxdbr0",
         "network": "lxdbr0"},
        {"addresses": "10.22.114.231/24", "type": "instance", "hwaddr": "00:16:3e:00:00:01",
         "used_by": "/1.0/instances/qahf-nic-test", "network": "lxdbr0"},
    ],
    "UAT-Hermes": [
        {"addresses": "192.168.3.12/24", "type": "instance", "hwaddr": "00:16:3e:00:00:02",
         "used_by": "/1.0/instances/hermes-c1?project=UAT-Hermes", "network": "ovn-uat-hermes"},
    ],
}


def _app(monkeypatch, *, instances_exc: Optional[Exception] = None, visible=None):
    writes: List[Any] = []

    class _Api:
        _api_endpoint = "https://fake:8443/1.0"
        projects = _Node(PROJECTS)

        def __init__(self) -> None:
            self.session = SimpleNamespace(get=self._allocations)

        @staticmethod
        def _allocations(url: str, timeout=None):
            project = url.split("project=")[1].split("&")[0]
            return _Response({"metadata": ALLOCATIONS.get(project, [])})

        def __getitem__(self, key):
            return {
                "networks": _Node(NETWORKS),
                "instances": _Node(INSTANCES, raise_exc=instances_exc),
            }[key]

    client = SimpleNamespace(
        api=_Api(),
        host_info={"api_extensions": ["networks_all_projects", "instance_all_projects"]},
        project=None,
    )
    conn = SimpleNamespace(
        get_client=lambda node: client,
        get_node_names=lambda: [HOST],
        has_attempted=lambda node: True,
    )
    svc = SimpleNamespace(connection_manager=conn, get_node_names=lambda: [HOST])

    class _Cache:
        def get(self, *a, **k):
            return None

        def set(self, namespace, key, value, ttl=None):
            # The soft TTL: a stale-while-revalidate entry is kept longer than it is fresh.
            writes.append((key, value.get("s", ttl) if is_envelope(value) else ttl))

        def instance_list_generation(self):
            return "g1"

    monkeypatch.setattr(ipam_module, "get_cache_manager", lambda: _Cache())
    monkeypatch.setattr(ipam_module, "verify_cluster_access", lambda host: None)
    monkeypatch.setattr(ipam_module, "check_tenant_permission", lambda *a, **k: True)
    monkeypatch.setattr(ipam_module, "tenant_acl_active", lambda: False)
    monkeypatch.setattr(
        ipam_module, "project_is_visible",
        (lambda host, project: project in visible) if visible is not None else (lambda host, project: True),
    )
    app = FastAPI()
    app.include_router(ipam_module.router)
    app.dependency_overrides[get_lxd_service] = lambda: svc
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False), writes


def _by_address(body: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {row["address"]: row for row in body["allocations"]}


def test_every_row_names_its_pin_state_network_type_and_subnet(monkeypatch) -> None:
    tc, writes = _app(monkeypatch)
    r = tc.get("/ipam?fresh=true")
    assert r.status_code == 200, r.text
    rows = _by_address(r.json())
    pinned = rows["10.22.114.231"]
    assert pinned["allocation"] == "static"
    assert pinned["network_type"] == "bridge"
    assert pinned["network_project"] == "default"
    assert pinned["subnet_ipv4"] == "10.22.114.1/24"
    assert pinned["subnet_ipv6"] == ""
    leased = rows["192.168.3.12"]
    assert leased["allocation"] == "dhcp"
    assert leased["network_type"] == "ovn"
    assert leased["network_project"] == "UAT-Hermes"
    assert leased["subnet_ipv4"] == "192.168.3.1/24"
    assert rows["10.22.114.1"]["allocation"] == "gateway"
    body = r.json()
    assert body["partial_nodes"] == [] and body["unreachable_nodes"] == []
    assert writes and writes[0][1] == 120, "a complete read is cached for the full TTL"


def test_an_unread_instance_list_marks_the_node_partial_and_the_rows_unknown(monkeypatch) -> None:
    tc, writes = _app(monkeypatch, instances_exc=RuntimeError("instances timed out"))
    r = tc.get("/ipam?fresh=true")
    assert r.status_code == 200, r.text
    body = r.json()
    rows = _by_address(body)
    assert rows["10.22.114.231"]["allocation"] == "unknown"
    assert rows["192.168.3.12"]["allocation"] == "unknown"
    assert rows["10.22.114.1"]["allocation"] == "gateway", "the gateway needs no instance"
    assert rows["10.22.114.231"]["subnet_ipv4"] == "10.22.114.1/24", "the network facts still land"
    assert body["partial_nodes"] == [HOST]
    assert body["node_errors"][HOST].startswith("partial:")
    assert "instances" in body["node_errors"][HOST]
    assert writes == [], "a partial read must not be remembered as the answer"


def test_the_partner_project_bound_drops_a_foreign_projects_rows(monkeypatch) -> None:
    tc, _ = _app(monkeypatch, visible={"default"})
    r = tc.get("/ipam?fresh=true")
    assert r.status_code == 200, r.text
    projects = {row["project"] for row in r.json()["allocations"]}
    assert projects == {"default"}


def test_an_instance_lifecycle_change_clears_the_ipam_list() -> None:
    from app.core.cache import CacheManager

    cleared: List[str] = []
    mgr = CacheManager.__new__(CacheManager)
    mgr.delete_pattern = lambda ns, pattern: cleared.append(pattern)  # type: ignore[method-assign]
    mgr.set = lambda *a, **k: None  # type: ignore[method-assign]
    CacheManager.invalidate_instance_cache(mgr)
    assert "ipam:*" in cleared, (
        "a NIC pin, a create or a delete changes the allocation table; leaving ipam:list "
        f"cached shows the old picture for two minutes. cleared={cleared}"
    )


def test_a_failed_project_read_does_not_name_the_project_to_the_caller(monkeypatch) -> None:
    """A Partner on a shared cluster must not learn the other organization's project names
    from a banner; the project is logged, the reason shown is generic."""
    tc, _ = _app(monkeypatch)
    from app.routers import ipam as ipam_module

    class _Api:
        _api_endpoint = "https://fake:8443/1.0"
        projects = _Node(PROJECTS)

        def __init__(self) -> None:
            self.session = SimpleNamespace(get=self._allocations)

        @staticmethod
        def _allocations(url: str, timeout=None):
            if "project=UAT-Hermes" in url:
                raise RuntimeError("read timed out")
            project = url.split("project=")[1].split("&")[0]
            return _Response({"metadata": ALLOCATIONS.get(project, [])})

        def __getitem__(self, key):
            return {"networks": _Node(NETWORKS), "instances": _Node(INSTANCES)}[key]

    client = SimpleNamespace(api=_Api(), host_info={"api_extensions": ["networks_all_projects", "instance_all_projects"]}, project=None)
    monkeypatch.setattr(ipam_module, "_read_join_sources", lambda c, n, r: ({"default": {}, "UAT-Hermes": {"features.networks": "true"}}, {}, {}, True))
    svc = SimpleNamespace(connection_manager=SimpleNamespace(get_client=lambda node: client))
    result = ipam_module._fetch_ipam_for_node(svc, HOST, None)
    assert result.problems, "the failed project read must be named as a problem"
    assert all("UAT-Hermes" not in p for p in result.problems), result.problems
    assert any("RuntimeError" in p for p in result.problems), result.problems


def test_a_lease_listed_under_a_borrowing_project_belongs_to_the_instances_project(monkeypatch) -> None:
    """LXD lists a shared network's leases under every project that borrows the network, and
    names the instance's own project only in used_by. The row must carry that project, so the
    pin lookup finds the instance and a tenant of the borrowing project is not shown it; and
    the same lease listed three times is one row."""
    tc, _ = _app(monkeypatch)
    from app.routers import ipam as ipam_module

    class _Api:
        _api_endpoint = "https://fake:8443/1.0"
        projects = _Node([{"name": "default", "config": {}}, {"name": "borrower", "config": {}},
                          {"name": "other", "config": {"features.networks": "true"}}])

        def __init__(self) -> None:
            self.session = SimpleNamespace(get=self._allocations)

        @staticmethod
        def _allocations(url: str, timeout=None):
            project = url.split("project=")[1].split("&")[0]
            shared = [
                {"addresses": "10.22.114.1/24", "type": "network", "used_by": "/1.0/networks/lxdbr0", "network": "lxdbr0"},
                {"addresses": "10.22.114.231/24", "type": "instance", "hwaddr": "00:16:3e:00:00:01",
                 "used_by": "/1.0/instances/qahf-nic-test", "network": "lxdbr0"},
                {"addresses": "10.22.114.77/24", "type": "instance", "hwaddr": "00:16:3e:00:00:07",
                 "used_by": "/1.0/instances/theirs?project=other", "network": "lxdbr0"},
            ]
            return _Response({"metadata": shared if project in ("default", "borrower") else []})

        def __getitem__(self, key):
            return {
                "networks": _Node([{"name": "lxdbr0", "project": "default", "type": "bridge",
                                    "config": {"ipv4.address": "10.22.114.1/24"}}]),
                "instances": _Node([
                    {"name": "qahf-nic-test", "project": "default",
                     "expanded_devices": {"eth1": {"type": "nic", "nictype": "bridged", "parent": "lxdbr0",
                                                   "ipv4.address": "10.22.114.231"}}},
                    {"name": "theirs", "project": "other", "expanded_devices": {"eth0": {"type": "nic", "network": "lxdbr0"}}},
                ]),
            }[key]

    client = SimpleNamespace(api=_Api(), host_info={"api_extensions": ["networks_all_projects", "instance_all_projects"]}, project=None)
    svc = SimpleNamespace(connection_manager=SimpleNamespace(get_client=lambda node: client))
    read = ipam_module._fetch_ipam_for_node(svc, HOST, None)
    rows = read.rows
    mine = [r for r in rows if r.used_by == "qahf-nic-test"]
    assert len(mine) == 1, [(r.project, r.address) for r in rows]
    assert mine[0].project == "default"
    assert mine[0].allocation == "static"
    theirs = [r for r in rows if r.used_by == "theirs"]
    assert len(theirs) == 1 and theirs[0].project == "other" and theirs[0].allocation == "dhcp"
    # The gateway of a borrowed network is listed for each project that borrows it.
    assert sorted(r.project for r in rows if r.type == "network") == ["borrower", "default"]


def test_a_daemon_with_the_extension_is_read_once_for_every_project(monkeypatch) -> None:
    """One all-projects read instead of one per project, which also avoids a duplicate row per
    borrowing project on a cluster with many projects. The rows name their owner in used_by."""
    tc, _ = _app(monkeypatch)
    from app.routers import ipam as ipam_module
    calls: List[str] = []

    class _Api:
        _api_endpoint = "https://fake:8443/1.0"
        projects = _Node([{"name": "default", "config": {}}, {"name": "borrower", "config": {}}])

        def __init__(self) -> None:
            self.session = SimpleNamespace(get=self._allocations)

        @staticmethod
        def _allocations(url: str, timeout=None):
            calls.append(url)
            assert "all-projects=true" in url, "a daemon with the extension must not be walked"
            return _Response({"metadata": [
                {"addresses": "10.22.114.1/24", "type": "network", "used_by": "/1.0/networks/lxdbr0", "network": "lxdbr0"},
                {"addresses": "10.22.114.231/24", "type": "instance", "hwaddr": "00:16:3e:00:00:01",
                 "used_by": "/1.0/instances/qahf-nic-test", "network": "lxdbr0"},
                {"addresses": "192.168.3.1/24", "type": "network", "used_by": "/1.0/networks/ovn-x?project=other", "network": "ovn-x"},
                {"addresses": "192.168.3.12/24", "type": "instance", "hwaddr": "00:16:3e:00:00:02",
                 "used_by": "/1.0/instances/theirs?project=other", "network": "ovn-x"},
            ]})

        def __getitem__(self, key):
            return {
                "networks": _Node([
                    {"name": "lxdbr0", "project": "default", "type": "bridge", "config": {"ipv4.address": "10.22.114.1/24"}},
                    {"name": "ovn-x", "project": "other", "type": "ovn", "config": {"ipv4.address": "192.168.3.1/24"}},
                ]),
                "instances": _Node([
                    {"name": "qahf-nic-test", "project": "default",
                     "expanded_devices": {"eth1": {"type": "nic", "nictype": "bridged", "parent": "lxdbr0", "ipv4.address": "10.22.114.231"}}},
                    {"name": "theirs", "project": "other", "expanded_devices": {"eth0": {"type": "nic", "network": "ovn-x"}}},
                ]),
            }[key]

    client = SimpleNamespace(api=_Api(), host_info={"api_extensions": ["networks_all_projects", "instance_all_projects", "network_allocations"]}, project=None)
    svc = SimpleNamespace(connection_manager=SimpleNamespace(get_client=lambda node: client))
    read = ipam_module._fetch_ipam_for_node(svc, HOST, None)
    assert len(calls) == 1 and "all-projects=true" in calls[0]
    by = {(r.project, r.address): r for r in read.rows}
    assert by[("default", "10.22.114.1")].allocation == "gateway"
    assert by[("default", "10.22.114.231")].allocation == "static"
    assert by[("other", "192.168.3.1")].allocation == "gateway"
    assert by[("other", "192.168.3.12")].allocation == "dhcp"
    assert by[("other", "192.168.3.12")].network_type == "ovn"
    assert by[("other", "192.168.3.12")].subnet_ipv4 == "192.168.3.1/24"
    assert read.complete
