"""The external policy sync writes only into orcahub organizations the calling key owns.

The endpoint's own authentication compares a plain sha256 with the HMAC every key since RC1
stores, so every current key gets 401 and nothing reaches this code today. The lookup and the
ownership check are fixed anyway, so a future repair of that authentication cannot open the path
unguarded: the org lookup had no source filter, and a NULL owner skipped the ownership check.
The tests get past the dead authentication with a legacy-shaped hash, the only kind it accepts.
"""

import hashlib
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from app.routers import integrations
from app.routers.integrations import SyncPoliciesRequest


class _Result:
    def __init__(self, value: Any = None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class FakeSession:
    def __init__(self, org: Optional[Any]) -> None:
        self.org = org
        self.statements: List[Any] = []
        self.added: List[Any] = []
        self.committed = False

    async def execute(self, stmt: Any, *_a: Any, **_k: Any) -> _Result:
        self.statements.append(stmt)
        if len(self.statements) == 1:
            return _Result(self.org)
        return _Result(None)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = 1

    async def commit(self) -> None:
        self.committed = True


class _Cache:
    def invalidate_resource_cache(self, *_a: Any) -> None:
        return None


class _Request:
    headers = {"X-API-Key-ID": "oak_test", "X-API-Key-Secret": "s3cret"}


@pytest.fixture(autouse=True)
def dead_auth_passes(monkeypatch):
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: object())
    monkeypatch.setattr(
        integrations,
        "get_api_key_from_vault",
        lambda _v, _k: ({"id": "oak_test"}, hashlib.sha256(b"s3cret").hexdigest()),
    )
    monkeypatch.setattr(integrations, "get_cache_manager", lambda: _Cache())


def _body() -> SyncPoliciesRequest:
    return SyncPoliciesRequest(policies=[{
        "external_id": "pol-1",
        "name": "p",
        "org_external_id": "ext-1",
        "rules": [{"cluster_id": "cluster-a", "project_name": "proj-a"}],
    }])


def _org(api_key_id: Optional[str]) -> SimpleNamespace:
    return SimpleNamespace(id=7, slug="org-a", api_key_id=api_key_id)


async def test_an_unclaimed_org_is_not_writable():
    db = FakeSession(_org(None))
    result = await integrations.sync_policies_from_external(body=_body(), request=_Request(), db=db)
    assert result["synced"] == 0
    assert any("not found" in e for e in result["errors"])
    assert db.added == [], "no policy may be written into an org no key owns"


async def test_another_keys_org_is_not_writable():
    db = FakeSession(_org("oak_other"))
    result = await integrations.sync_policies_from_external(body=_body(), request=_Request(), db=db)
    assert result["synced"] == 0
    assert db.added == []


async def test_the_lookup_is_limited_to_orcahub_orgs():
    db = FakeSession(None)
    await integrations.sync_policies_from_external(body=_body(), request=_Request(), db=db)
    sql = str(db.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "organizations.source = 'orcahub'" in sql


async def test_the_owner_still_writes():
    db = FakeSession(_org("oak_test"))
    result = await integrations.sync_policies_from_external(body=_body(), request=_Request(), db=db)
    assert result["synced"] == 1
    assert result["errors"] == []
    kinds = {type(obj).__name__ for obj in db.added}
    assert {"TenantAccessPolicy", "TenantPolicyRule"} <= kinds
