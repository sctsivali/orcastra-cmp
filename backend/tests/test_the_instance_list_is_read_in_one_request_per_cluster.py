"""A cluster's instances are read in one request, state included.

The per-node read walked the projects one by one (`projects.all()` plus one
`GET /1.0/instances?project=` per project) and then asked `/state` once per running
instance, keyed by bare name: 11 s on a 52-project host, and two projects holding an instance
of the same name shared one state entry. A daemon that advertises `instance_all_projects`
answers everything, state inline, in one `?all-projects=true&recursion=2` request; a daemon
that does not is walked project by project by `list_across_projects`, still with state.
"""

import logging
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from pylxd.exceptions import NotFound
from requests import exceptions as requests_exc

from app.services.lxd import health
from app.services.lxd import instance as instance_module
from app.services.lxd.instance import InstanceManager

NODE = "nuc2"
STATE = {"network": {"eth0": {"addresses": [{"family": "inet", "address": "10.0.0.5", "scope": "global"}]}}}


def _row(name: str, project: str, status: str = "Running", state: Any = None) -> Dict[str, Any]:
    return {
        "name": name,
        "project": project,
        "status": status,
        "status_code": 103 if status == "Running" else 102,
        "type": "container",
        "config": {"user.tags": "a, b"},
        "devices": {},
        "expanded_devices": {},
        "profiles": ["default"],
        "location": "none",
        "state": state if state is not None else (STATE if status == "Running" else {"network": None}),
    }


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Instances:
    def __init__(
        self,
        rows_by_project: Dict[str, List[dict]],
        calls: List[str],
        all_projects: bool,
        timeout_first=False,
        timeouts: Optional[List[Any]] = None,
    ):
        self._rows = rows_by_project
        self.calls = calls
        self._all = all_projects
        self._timeout_first = timeout_first
        self._timeouts = timeouts

    def get(self, params=None, timeout=None):
        params = params or {}
        if self._timeouts is not None:
            self._timeouts.append(timeout)
        self.calls.append(
            f"instances recursion={params.get('recursion')} all-projects={params.get('all-projects')} project={params.get('project')}"
        )
        if self._timeout_first and params.get("recursion") == 2:
            raise requests_exc.ReadTimeout("read budget")
        if params.get("all-projects") == "true":
            if not self._all:
                raise AssertionError("all-projects asked of a daemon that does not advertise it")
            rows = [dict(r) for rows in self._rows.values() for r in rows]
            if params.get("recursion") == 1:
                for r in rows:
                    r.pop("state", None)
            return _Response({"metadata": rows})
        rows = [dict(r) for r in self._rows.get(params.get("project"), [])]
        if params.get("recursion") == 1:
            for row in rows:
                row.pop("state", None)
        return _Response({"metadata": rows})

    def __getitem__(self, name):
        raise AssertionError(f"a per-instance read of {name} must not happen on the list path")


class _Projects:
    def __init__(self, names: List[str], calls: List[str]):
        self._names, self.calls = names, calls

    def get(self, params=None, timeout=None):
        self.calls.append(f"projects recursion={(params or {}).get('recursion')}")
        return _Response({"metadata": [{"name": n, "config": {}} for n in self._names]})

    def all(self):
        raise AssertionError("pylxd projects.all() must not be used any more")


class _Api:
    """pylxd's api root: attribute access and subscript address the same node."""

    def __init__(self, **nodes):
        self._nodes = nodes

    def __getattr__(self, name):
        try:
            return self._nodes[name]
        except KeyError:
            raise AttributeError(name)

    def __getitem__(self, name):
        return self._nodes[name]


def _client(
    rows_by_project,
    calls,
    all_projects=True,
    timeout_first=False,
    timeouts=None,
):
    return SimpleNamespace(
        host_info={"api_extensions": ["instance_all_projects"] if all_projects else []},
        api=_Api(
            instances=_Instances(
                rows_by_project,
                calls,
                all_projects,
                timeout_first,
                timeouts,
            ),
            projects=_Projects(list(rows_by_project), calls),
        ),
    )


def _manager(client) -> InstanceManager:
    manager = InstanceManager(connection_manager=SimpleNamespace(get_client=lambda node: client), resource_manager=None)
    manager.cert_manager = None
    return manager


@pytest.fixture(autouse=True)
def _breaker_open(monkeypatch):
    monkeypatch.setattr(health, "is_tripped", lambda node: False)


def test_one_request_carries_every_project_with_state():
    calls: List[str] = []
    rows = {"default": [_row("web", "default")], "qa": [_row("web", "qa", "Stopped"), _row("db", "qa")]}
    vms, connected = _manager(_client(rows, calls))._fetch_vms_from_node(NODE, "https://nuc2:8443")

    assert connected is True
    assert calls == ["instances recursion=2 all-projects=true project=None"]
    keys = sorted((v.project, v.name, v.status) for v in vms)
    assert keys == [("default", "web", "Running"), ("qa", "db", "Running"), ("qa", "web", "Stopped")]
    by_key = {(v.project, v.name): v for v in vms}
    assert by_key[("default", "web")].ip_addresses == ["10.0.0.5"]
    assert by_key[("qa", "web")].ip_addresses == []
    assert all(v.host == NODE and v.host_endpoint == "https://nuc2:8443" for v in vms)
    assert by_key[("qa", "db")].tags == ["a", "b"] and by_key[("qa", "db")].location is None


def test_a_daemon_without_the_extension_is_walked_with_state():
    calls: List[str] = []
    rows = {"default": [_row("web", "default")], "qa": [_row("db", "qa")]}
    vms, connected = _manager(_client(rows, calls, all_projects=False))._fetch_vms_from_node(NODE, "")

    assert connected is True
    assert sorted(v.project for v in vms) == ["default", "qa"]
    assert "projects recursion=0" in calls
    assert all("recursion=2" in c for c in calls if c.startswith("instances"))
    assert all("all-projects=None" in c for c in calls if c.startswith("instances"))


def test_a_state_read_that_outruns_the_budget_falls_back_to_a_list_without_addresses(caplog):
    calls: List[str] = []
    rows = {"default": [_row("web", "default")]}
    with caplog.at_level(logging.WARNING):
        vms, connected = _manager(_client(rows, calls, timeout_first=True))._fetch_vms_from_node(NODE, "")

    assert connected is True and len(vms) == 1
    assert calls == [
        "instances recursion=2 all-projects=true project=None",
        "instances recursion=1 all-projects=true project=None",
    ]
    assert vms[0].ip_addresses == []
    assert "addresses omitted" in caplog.text


def test_a_read_that_raises_reports_the_node_unreachable():
    class _Broken:
        def get(self, params=None, timeout=None):
            raise RuntimeError("connection reset")

    client = SimpleNamespace(host_info={"api_extensions": ["instance_all_projects"]}, api=_Api(instances=_Broken()))
    vms, connected = _manager(client)._fetch_vms_from_node(NODE, "")
    assert (vms, connected) == ([], False)


def test_a_partial_walk_keeps_the_rows_it_could_read_and_says_so(caplog):
    calls: List[str] = []

    class _Flaky(_Instances):
        def get(self, params=None, timeout=None):
            if (params or {}).get("project") == "qa":
                raise RuntimeError("project read failed")
            return super().get(params, timeout)

    rows = {"default": [_row("web", "default")], "qa": [_row("db", "qa")]}
    client = SimpleNamespace(
        host_info={"api_extensions": []},
        api=_Api(instances=_Flaky(rows, calls, False), projects=_Projects(list(rows), calls)),
    )
    with caplog.at_level(logging.WARNING):
        vms, connected = _manager(client)._fetch_vms_from_node(NODE, "")

    assert connected is True
    assert [v.name for v in vms] == ["web"]
    assert "partial" in caplog.text


def test_fetch_node_instances_is_the_same_read():
    calls: List[str] = []
    rows = {"default": [_row("web", "default")]}
    manager = _manager(_client(rows, calls))
    vms, connected = manager.fetch_node_instances(NODE, "https://nuc2:8443")
    assert connected and [v.name for v in vms] == ["web"] and vms[0].host_endpoint == "https://nuc2:8443"


def test_an_exact_project_read_never_asks_for_other_projects():
    calls: List[str] = []
    rows = {
        "default": [_row("web", "default")],
        "qa": [_row("db", "qa")],
    }

    vms, connected, complete = _manager(_client(rows, calls))._fetch_vms_from_node_detailed(
        NODE,
        "https://nuc2:8443",
        project="qa",
    )

    assert connected is True and complete is True
    assert [(vm.project, vm.name) for vm in vms] == [("qa", "db")]
    assert calls == ["instances recursion=2 all-projects=None project=qa"]


def test_an_exact_project_state_timeout_falls_back_without_widening_the_read():
    calls: List[str] = []
    timeouts: List[Any] = []
    rows = {"qa": [_row("db", "qa")]}

    vms, connected, complete = _manager(
        _client(rows, calls, timeout_first=True, timeouts=timeouts)
    )._fetch_vms_from_node_detailed(
        NODE,
        "",
        project="qa",
    )

    assert connected is True and complete is True
    assert [(vm.project, vm.name) for vm in vms] == [("qa", "db")]
    assert vms[0].ip_addresses == []
    assert calls == [
        "instances recursion=2 all-projects=None project=qa",
        "instances recursion=1 all-projects=None project=qa",
    ]
    assert timeouts == [
        instance_module._SCOPED_INSTANCE_STATE_READ_TIMEOUT,
        instance_module._SCOPED_INSTANCE_LIST_READ_TIMEOUT,
    ]
    assert sum(sum(timeout) for timeout in timeouts) <= (
        instance_module._INSTANCE_NODE_TIMEOUT_SECONDS
        - instance_module._SCOPED_INSTANCE_PRELUDE_RESERVE_SECONDS
    )


def test_a_missing_exact_project_is_a_complete_empty_answer():
    class _MissingProject:
        def get(self, params=None, timeout=None):
            raise NotFound(
                SimpleNamespace(
                    status_code=404,
                    json=lambda: {"error": "Project not found"},
                )
            )

    client = SimpleNamespace(
        host_info={"api_extensions": ["instance_all_projects"]},
        api=_Api(instances=_MissingProject()),
    )

    vms, connected, complete = _manager(client)._fetch_vms_from_node_detailed(
        NODE,
        "",
        project="gone",
    )

    assert vms == []
    assert connected is True
    assert complete is True
