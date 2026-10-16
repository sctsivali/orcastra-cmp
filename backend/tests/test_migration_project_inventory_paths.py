"""Project inventory asks LXD for its dashed collection paths, never pylxd attribute names.

Found live: pylxd turns ``client.api.network_acls`` into ``/1.0/network_acls``, which LXD
answers with 404, so every project relocation preflight failed closed on ACL inventory.
"""

from __future__ import annotations

from typing import Any

from app.services.migrations import preflight


class _Node:
    def __init__(self, path: str, seen: list[str]) -> None:
        self.path, self.seen = path, seen

    def __getattr__(self, name: str) -> "_Node":
        if name.startswith("_"):
            raise AttributeError(name)
        return _Node(f"{self.path}/{name}", self.seen)

    def __getitem__(self, name: str) -> "_Node":
        return _Node(f"{self.path}/{name}", self.seen)

    def get(self, **_kwargs: Any) -> Any:
        self.seen.append(self.path)
        rows = (
            [
                {"name": "lxdbr0", "managed": True, "type": "bridge"},
                {"name": "ovn0", "managed": True, "type": "ovn"},
                {"name": "enp5s0", "managed": False, "type": "physical"},
            ]
            if self.path.endswith("/networks")
            else []
        )
        return type("R", (), {"status_code": 200, "json": lambda _self: {"metadata": rows}})()


def test_every_collection_is_read_from_its_lxd_path(monkeypatch: Any) -> None:
    seen: list[str] = []
    client = type("Client", (), {"api": _Node("/1.0", seen)})()
    monkeypatch.setattr(preflight, "_authoritative_extensions", lambda _client: set(preflight._OPTIONAL_COLLECTION_EXTENSIONS.values()))
    monkeypatch.setattr(preflight, "_storage_pools", lambda _client: [])

    preflight._known_project_resources(client, "web")

    assert {"/1.0/network-acls", "/1.0/network-zones", "/1.0/network-address-sets", "/1.0/placement-groups"} <= set(seen)
    assert "/1.0/networks/lxdbr0/forwards" in seen and "/1.0/networks/ovn0/forwards" in seen
    # Only an OVN network can hold load balancers. LXD refuses the listing on a bridge.
    assert "/1.0/networks/ovn0/load-balancers" in seen
    assert "/1.0/networks/lxdbr0/load-balancers" not in seen
    # A host interface is not a managed network and has neither.
    assert not [path for path in seen if "enp5s0" in path]
    assert not [path for path in seen if "_" in path]
