"""Console kill, active sessions and the operations list read operations in every project.

LXD scopes `/1.0/operations` by project and answers for `default` when none is named. The three
listings asked with only `recursion=1`, so a console in any other project never appeared, and
closing its tab could not kill it even though the kill matched the right (project, instance)
pair. They now ask with `all-projects=true`, and fall back without it on a daemon that refuses
the parameter with 400.
"""

from types import SimpleNamespace
from typing import Any, Optional

import pytest
from pylxd import exceptions as pylxd_exceptions

from app.routers import console, lxd_operations
from app.services.lxd.utils import read_running_operations
from tests.instance_access_fakes import CLUSTER, partner


def _op(op_id: str, project: str) -> dict:
    query = "" if project == "default" else f"?project={project}"
    return {"id": op_id, "class": "websocket", "description": "Showing console", "created_at": "",
            "may_cancel": True, "metadata": {},
            "resources": {"instances": [f"/1.0/instances/web01{query}"]}}


class _Operations:
    """`/1.0/operations`: answers for default only unless all-projects is asked for."""

    def __init__(self, refuse_all_projects: bool = False) -> None:
        self.ops = [_op("op-default", "default"), _op("op-own", "own-web")]
        self.refuse_all_projects = refuse_all_projects
        self.asked: list[dict] = []
        self.deleted: list[str] = []

    def get(self, params: Optional[dict] = None, timeout: Any = None) -> Any:
        params = dict(params or {})
        self.asked.append(params)
        if params.get("all-projects") == "true":
            if self.refuse_all_projects:
                raise pylxd_exceptions.LXDAPIException(SimpleNamespace(status_code=400))
            running = self.ops
        else:
            running = [op for op in self.ops if "?project=" not in op["resources"]["instances"][0]]
        return SimpleNamespace(status_code=200, json=lambda: {"metadata": {"running": running}})

    def __getitem__(self, op_id: str) -> Any:
        return SimpleNamespace(delete=lambda: self.deleted.append(op_id) or SimpleNamespace(status_code=200))


@pytest.fixture
def operations(monkeypatch: pytest.MonkeyPatch) -> _Operations:
    from app.services import lxd_service as lxd_service_module

    ops = _Operations()
    client = SimpleNamespace(api=SimpleNamespace(operations=ops))
    service = SimpleNamespace(
        connection_manager=SimpleNamespace(get_client=lambda host: client, get_node_names=lambda: [CLUSTER]),
        get_node_names=lambda: [CLUSTER],
    )
    monkeypatch.setattr(lxd_service_module, "get_lxd_service", lambda: service)
    ops.service = service
    return ops


async def test_kill_all_reaches_a_console_outside_default(operations: _Operations) -> None:
    with partner({"default", "own-web"}):
        response = await console.kill_all_sessions(
            instance_name="web01", host=CLUSTER, project="own-web", _=None, _rbac=None
        )
    assert response.success
    assert operations.deleted == ["op-own"]


async def test_active_sessions_see_a_console_outside_default(operations: _Operations) -> None:
    with partner({"default", "own-web"}):
        response = await console.check_active_sessions(
            instance_name="web01", host=CLUSTER, project="own-web", _rbac=None
        )
    assert [s.operation_id for s in response.sessions] == ["op-own"]


async def test_the_operations_list_shows_every_visible_project(operations: _Operations) -> None:
    with partner({"default", "own-web"}):
        response = await lxd_operations.list_operations(host=CLUSTER, lxd_service=operations.service)
    assert sorted(op.id for op in response.operations) == ["op-default", "op-own"]


def test_a_daemon_refusing_all_projects_is_asked_again_without_it() -> None:
    ops = _Operations(refuse_all_projects=True)
    response = read_running_operations(SimpleNamespace(api=SimpleNamespace(operations=ops)))
    assert [op["id"] for op in response.json()["metadata"]["running"]] == ["op-default"]
    assert ops.asked == [{"recursion": "1", "all-projects": "true"}, {"recursion": "1"}]


def test_any_other_error_is_not_retried() -> None:
    class _Broken(_Operations):
        def get(self, params: Optional[dict] = None, timeout: Any = None) -> Any:
            self.asked.append(dict(params or {}))
            raise pylxd_exceptions.LXDAPIException(SimpleNamespace(status_code=500))

    ops = _Broken()
    with pytest.raises(pylxd_exceptions.LXDAPIException):
        read_running_operations(SimpleNamespace(api=SimpleNamespace(operations=ops)))
    assert len(ops.asked) == 1
