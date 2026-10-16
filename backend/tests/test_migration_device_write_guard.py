"""The NIC fence replaces devices only when they are still what it computed them from."""

from __future__ import annotations

from typing import Any, Optional

import pytest

from app.services.migrations import executor


class _Node:
    def __init__(self, devices: dict[str, Any], etag: Optional[str]) -> None:
        self.devices, self.etag, self.puts = devices, etag, []

    def get(self, **_kwargs: Any) -> Any:
        body = {"metadata": {"config": {"a": "1"}, "devices": self.devices, "profiles": ["default"]}}
        return type("R", (), {"json": lambda _s: body, "headers": {"ETag": self.etag} if self.etag else {}})()

    def put(self, **kwargs: Any) -> Any:
        self.puts.append(kwargs)
        return type("R", (), {"json": lambda _s: {"type": "sync", "status_code": 200}})()


def _client(node: _Node) -> Any:
    return type("C", (), {"api": type("A", (), {"instances": {"web": node}})()})()


def test_the_write_sends_the_etag_of_the_read_it_checked() -> None:
    node = _Node({"eth0": {"type": "nic"}}, '"7"')

    executor.replace_instance_devices(
        _client(node), project="p", name="web", devices={"eth0": {"type": "none"}}, expected_devices={"eth0": {"type": "nic"}}
    )

    assert node.puts[0]["headers"] == {"If-Match": '"7"'}
    assert node.puts[0]["json"]["devices"] == {"eth0": {"type": "none"}}
    assert node.puts[0]["json"]["config"] == {"a": "1"}


@pytest.mark.parametrize(("devices", "etag"), [({"eth0": {"type": "nic", "network": "x"}}, '"7"'), ({"eth0": {"type": "nic"}}, None)])
def test_a_changed_device_or_a_missing_etag_writes_nothing(devices: dict[str, Any], etag: Optional[str]) -> None:
    node = _Node(devices, etag)

    with pytest.raises(executor.MigrationOperationUnknown):
        executor.replace_instance_devices(
            _client(node), project="p", name="web", devices={"eth0": {"type": "none"}}, expected_devices={"eth0": {"type": "nic"}}
        )

    assert node.puts == []
