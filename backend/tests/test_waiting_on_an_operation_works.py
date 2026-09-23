"""POST /operations/{id}/wait waits through LXD's wait endpoint and reports the final state.

The route called `operation.wait(timeout=timeout)`, but pylxd's Operation.wait takes no
arguments, so every wait on a visible operation raised TypeError and answered 500. The test
fake accepted a timeout, which hid it. The fake here keeps pylxd's signature, so a return to
that call fails again.
"""

import inspect
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from pylxd import exceptions as pylxd_exceptions

from app.routers import lxd_operations
from tests.instance_access_fakes import CLUSTER, partner


class _PylxdShapedOperation:
    """What client.operations.get returns, with pylxd's own wait signature."""

    def __init__(self, op_id: str) -> None:
        self.id = op_id
        self.resources = {"instances": ["/1.0/instances/web01"]}
        self.metadata = {}

    def wait(self) -> None:
        raise AssertionError("pylxd's wait blocks with no timeout and must not be called")


class _Node:
    def __init__(self, final: dict, wait_error: Optional[Exception]) -> None:
        self.final = final
        self.wait_error = wait_error
        self.asked: list[tuple[str, Any, Any]] = []

        def wait_get(params: Optional[dict] = None, timeout: Any = None) -> Any:
            self.asked.append(("wait", dict(params or {}), timeout))
            if self.wait_error is not None:
                raise self.wait_error
            return SimpleNamespace(json=lambda: {"metadata": self.final})

        self.wait = SimpleNamespace(get=wait_get)

    def get(self, *args: Any, **kwargs: Any) -> Any:
        self.asked.append(("get", None, None))
        return SimpleNamespace(json=lambda: {"metadata": {"status": "Running", "status_code": 103}})


def _service(node: _Node) -> Any:
    client = SimpleNamespace(
        operations=SimpleNamespace(get=lambda op_id: _PylxdShapedOperation(op_id)),
        api=SimpleNamespace(operations={"op-1": node}),
    )
    return SimpleNamespace(connection_manager=SimpleNamespace(get_client=lambda host: client))


async def _wait(node: _Node, timeout: int = 30) -> dict:
    with partner({"default"}):
        return await lxd_operations.wait_operation(
            operation_id="op-1", host=CLUSTER, timeout=timeout, lxd_service=_service(node)
        )


async def test_a_wait_reports_the_final_state() -> None:
    node = _Node({"status": "Success", "status_code": 200, "err": ""}, None)
    result = await _wait(node, timeout=12)
    assert result["success"] is True
    assert result["status"] == "Success"
    assert node.asked == [("wait", {"timeout": 12}, (5, 22))]


async def test_a_failed_operation_is_not_success() -> None:
    node = _Node({"status": "Failure", "status_code": 400, "err": "disk full"}, None)
    result = await _wait(node)
    assert result["success"] is False
    assert result["err"] == "disk full"


async def test_a_wait_that_runs_out_reports_the_running_operation() -> None:
    elapsed = pylxd_exceptions.LXDAPIException(
        SimpleNamespace(status_code=500, json=lambda: {"error": "context deadline exceeded"})
    )
    node = _Node({}, elapsed)
    result = await _wait(node, timeout=1)
    assert result["success"] is False
    assert result["status"] == "Running"
    assert [kind for kind, _p, _t in node.asked] == ["wait", "get"]


def test_the_wait_is_bounded() -> None:
    parameter = inspect.signature(lxd_operations.wait_operation).parameters["timeout"].default
    bounds = {type(m).__name__: m for m in parameter.metadata}
    assert bounds["Ge"].ge == 0 and bounds["Le"].le == 300


@pytest.mark.parametrize("seconds", [0, 300])
async def test_the_read_timeout_outlasts_the_wait(seconds: int) -> None:
    node = _Node({"status": "Success", "status_code": 200}, None)
    await _wait(node, timeout=seconds)
    _kind, _params, read_timeout = node.asked[0]
    assert read_timeout[1] > seconds
