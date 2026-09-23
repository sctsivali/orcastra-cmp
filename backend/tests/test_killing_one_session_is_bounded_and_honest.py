"""DELETE /console/{name}/active-sessions/{op} is held to the bound and reports what happened.

The project bound on this route was not pinned by any test: replacing it with a no-op left the
suite green, because the per-operation (project, instance) match masked it whenever the test
used the default project. And the read before the cancel turned any error, a timeout or a 5xx
included, into "already ended" without trying the cancel, so the UI was told a live session was
gone while it kept running. Only a 404 means that now.
"""

from types import SimpleNamespace
from typing import Any, Optional

import pytest
from fastapi import HTTPException
from pylxd import exceptions as pylxd_exceptions

from app.routers import console
from tests.instance_access_fakes import CLUSTER, partner

FOREIGN = "other-db"
OPERATION = {"id": "op-1", "class": "websocket",
             "resources": {"instances": [f"/1.0/instances/web01?project={FOREIGN}"]}}


class _Operations:
    """`client.api.operations`: records every read and cancel of one operation."""

    def __init__(self, read_error: Optional[Exception] = None) -> None:
        self.read_error = read_error
        self.calls: list[str] = []

    def __getitem__(self, op_id: str) -> Any:
        def get(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(f"GET {op_id}")
            if self.read_error is not None:
                raise self.read_error
            return SimpleNamespace(status_code=200, json=lambda: {"metadata": OPERATION})

        def delete(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(f"DELETE {op_id}")
            return SimpleNamespace(status_code=200)

        return SimpleNamespace(get=get, delete=delete)


@pytest.fixture
def install(monkeypatch: pytest.MonkeyPatch):
    from app.services import lxd_service as lxd_service_module

    def _install(read_error: Optional[Exception] = None) -> _Operations:
        ops = _Operations(read_error)
        client = SimpleNamespace(api=SimpleNamespace(operations=ops),
                                 operations=SimpleNamespace(get=lambda op_id: OPERATION))
        service = SimpleNamespace(connection_manager=SimpleNamespace(get_client=lambda host: client))
        monkeypatch.setattr(lxd_service_module, "get_lxd_service", lambda: service)
        return ops

    return _install


def _kill(project: str) -> Any:
    return console.kill_active_session(
        instance_name="web01", operation_id="op-1", host=CLUSTER, project=project, _=None, _rbac=None
    )


async def test_a_kill_in_a_foreign_project_is_refused_before_any_lxd_call(install) -> None:
    ops = install()
    with partner({"default", "own-web"}):
        with pytest.raises(HTTPException) as exc:
            await _kill(FOREIGN)
    assert exc.value.status_code == 403
    assert ops.calls == []


def _lxd_error(status_code: int) -> Exception:
    return pylxd_exceptions.LXDAPIException(
        SimpleNamespace(status_code=status_code, json=lambda: {"error": "daemon error"})
    )


@pytest.mark.parametrize(
    "error",
    [_lxd_error(500), _lxd_error(503), TimeoutError("read timed out"), ConnectionResetError("reset")],
    ids=["500", "503", "timeout", "reset"],
)
async def test_a_failed_read_is_a_failure_and_cancels_nothing(install, error) -> None:
    ops = install(error)
    with partner({"default", "own-web"}):
        response = await _kill("own-web")
    assert response.success is False
    assert response.error == "Could not read the session, so it was not terminated"
    assert ops.calls == ["GET op-1"]


async def test_a_missing_operation_has_already_ended(install) -> None:
    ops = install(pylxd_exceptions.NotFound(
        SimpleNamespace(status_code=404, json=lambda: {"error": "Operation not found"})
    ))
    with partner({"default", "own-web"}):
        response = await _kill("own-web")
    assert response.success is True
    assert "already ended" in response.message
    assert ops.calls == ["GET op-1"]
