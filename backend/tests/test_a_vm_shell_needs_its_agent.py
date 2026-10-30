"""A shell on a VM without lxd-agent is refused before the exec, with the reason.

LXD 5.21 accepts that exec and then drops its sockets, so the failure used to look like a lost
connection, and the browser retried it every second for as long as the page stayed open.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services import lxd_ws
from app.services.lxd_ws import SessionSetupError
from app.services.terminal_service import TerminalService


class _Response:
    def __init__(self, metadata: dict[str, Any], status_code: int = 200) -> None:
        self.status_code = status_code
        self._metadata = metadata

    def json(self) -> dict[str, Any]:
        return {"metadata": self._metadata}


class _State:
    def __init__(self, cluster: "_Cluster") -> None:
        self.cluster = cluster

    def get(self, **_kwargs: Any) -> _Response:
        self.cluster.state_reads += 1
        if self.cluster.state_error:
            raise RuntimeError("state read failed")
        return _Response({"processes": self.cluster.processes})


class _Exec:
    def __init__(self, cluster: "_Cluster") -> None:
        self.cluster = cluster

    def post(self, **_kwargs: Any) -> _Response:
        self.cluster.exec_posts += 1
        return _Response({"id": "op-1", "metadata": {"fds": {"0": "data", "control": "control"}}}, 202)


class _Instance:
    def __init__(self, cluster: "_Cluster") -> None:
        self.cluster = cluster
        self.state = _State(cluster)
        self.exec = _Exec(cluster)

    def get(self, **_kwargs: Any) -> _Response:
        return _Response(
            {
                "status": "Running",
                "type": self.cluster.instance_type,
                "expanded_config": self.cluster.config,
            }
        )


class _Cluster:
    """Stands in for pylxd: `client.api.instances[name]`."""

    def __init__(
        self,
        *,
        instance_type: str = "virtual-machine",
        processes: int = 13,
        config: dict[str, str] | None = None,
        state_error: bool = False,
    ) -> None:
        self.instance_type = instance_type
        self.processes = processes
        self.config = config or {}
        self.state_error = state_error
        self.state_reads = 0
        self.exec_posts = 0
        self.api = self
        self.instances = {"vm": _Instance(self)}


def _open(monkeypatch: pytest.MonkeyPatch, cluster: _Cluster) -> dict[str, Any]:
    monkeypatch.setattr(lxd_ws, "lxd_client", lambda _host: cluster)
    monkeypatch.setattr(lxd_ws, "connection_material", lambda _host: ("https://cluster:8443", None))
    return TerminalService().open_exec_operation("vm", "lab", "default")


def test_a_vm_without_its_agent_is_refused_before_any_exec(monkeypatch):
    cluster = _Cluster(processes=-1)

    with pytest.raises(SessionSetupError) as refused:
        _open(monkeypatch, cluster)

    assert "agent" in str(refused.value).lower()
    assert "Serial" in str(refused.value)
    assert refused.value.retryable is False
    assert cluster.exec_posts == 0


def test_a_vm_with_its_agent_opens_a_shell(monkeypatch):
    cluster = _Cluster(processes=13)

    assert _open(monkeypatch, cluster)["operation_id"] == "op-1"
    assert cluster.exec_posts == 1


def test_a_vm_that_turned_off_agent_metrics_still_gets_a_shell(monkeypatch):
    # LXD stops asking the agent for figures here, so -1 says nothing about the exec path.
    cluster = _Cluster(processes=-1, config={"security.agent.metrics": "false"})

    _open(monkeypatch, cluster)

    assert cluster.exec_posts == 1


def test_a_container_has_no_agent_to_ask_about(monkeypatch):
    cluster = _Cluster(instance_type="container", processes=-1)

    _open(monkeypatch, cluster)

    assert cluster.state_reads == 0
    assert cluster.exec_posts == 1


def test_an_unread_state_does_not_cost_the_shell(monkeypatch):
    cluster = _Cluster(state_error=True)

    _open(monkeypatch, cluster)

    assert cluster.exec_posts == 1
