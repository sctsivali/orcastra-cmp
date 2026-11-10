"""The orphaned-group cleanup deletes nothing when it could not read the whole key roster.

A Vault outage or a failed metadata read leaves a partial roster, and against it every `key_*` LXD
group looks orphaned; deleting them would strip the permissions from every live key in the fleet.
So the cleanup refuses a partial read with a 503 and says why.
"""

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from app.routers import my_keys


class _Response:
    def __init__(self, status: int, body: Dict[str, Any]):
        self.status_code = status
        self._body = body

    def json(self) -> Dict[str, Any]:
        return self._body


class _Vault:
    vault_addr = "http://vault.invalid"
    headers: Dict[str, str] = {}

    def __init__(self, *, list_status: int = 200, keys: List[str] = (), unreadable: set = frozenset()):
        self._list_status = list_status
        self._keys = list(keys)
        self._unreadable = unreadable
        self.session = SimpleNamespace(get=self._get)

    def _get(self, *_args, **_kwargs):
        return _Response(self._list_status, {"data": {"keys": self._keys}})

    def vault_read(self, path: str):
        key_id = path.split("/")[-2]
        if key_id in self._unreadable:
            raise RuntimeError("vault read failed")
        return {"id": key_id, "group_name": key_id}


class _Groups:
    def __init__(self, names: List[str]):
        self.names = names
        self.deleted: List[str] = []

    def get(self, timeout=None):
        return _Response(200, {"metadata": [f"/1.0/auth/groups/{n}" for n in self.names]})

    def __getitem__(self, name: str):
        groups = self

        class _One:
            def delete(self, timeout=None):
                groups.deleted.append(name)
                return _Response(200, {})

        return _One()


def _lxd(groups: _Groups):
    client = SimpleNamespace(api=SimpleNamespace(auth=SimpleNamespace(groups=groups)))
    manager = SimpleNamespace(get_node_names=lambda: ["nuc1"], get_client=lambda _n: client)
    return SimpleNamespace(connection_manager=manager)


@pytest.mark.parametrize(
    "vault",
    [
        _Vault(list_status=503),
        _Vault(keys=["key_a", "key_b"], unreadable={"key_b"}),
    ],
    ids=["vault-down", "one-key-unreadable"],
)
async def test_a_partial_roster_deletes_no_group(monkeypatch, vault) -> None:
    groups = _Groups(["key_a", "key_b"])
    monkeypatch.setattr(my_keys, "get_vault_keys_client", lambda: vault)

    with pytest.raises(HTTPException) as caught:
        await my_keys.cleanup_orphaned_groups(lxd_service=_lxd(groups), _=None, _rbac=None)

    assert caught.value.status_code == 503
    assert groups.deleted == []


async def test_a_complete_roster_still_removes_a_real_orphan(monkeypatch) -> None:
    groups = _Groups(["key_a", "key_gone", "ops-team"])
    monkeypatch.setattr(my_keys, "get_vault_keys_client", lambda: _Vault(keys=["key_a"]))

    result = await my_keys.cleanup_orphaned_groups(lxd_service=_lxd(groups), _=None, _rbac=None)

    assert groups.deleted == ["key_gone"]
    assert result["deleted_count"] == 1


def test_an_empty_vault_path_is_a_complete_empty_roster() -> None:
    keys, complete = my_keys.read_key_roster(_Vault(list_status=404))
    assert keys == [] and complete is True
