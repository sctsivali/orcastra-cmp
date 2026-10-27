"""Every touch of a My Key is on its trail: issued or refused, downloaded or refused, deleted.

A download hands out the private key and wrote no record at all. A refused create or a refused
download wrote none either, and those are the records most worth having. Each record carries the
client it came from, kept on key rows only, and key rows are kept a year rather than the cluster
log's thirty days.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from app.core.auth import AuthenticatedUser
from app.core.request_context import RequestContext
from app.routers import my_keys
from app.services.task_journal import repository_sweeps
from app.services.task_journal.sink_mapping import record_to_log_row

OWNER = "owner@partner.test"


def _user(email: str = OWNER) -> AuthenticatedUser:
    return AuthenticatedUser(user_id=email, email=email, groups=["role_partner"])


class _Vault:
    def vault_read(self, path: str):
        key_id = path.split("/")[-2]
        if key_id == "key_a":
            return {"id": "key_a", "node": "nuc2", "owner_email": OWNER, "name": "ops", "project": "default"}
        return None


@pytest.fixture
def audits(monkeypatch) -> List[Dict[str, Any]]:
    recorded: List[Dict[str, Any]] = []
    monkeypatch.setattr(my_keys.activity_logger, "log_activity", lambda **kw: recorded.append(kw))
    monkeypatch.setattr(my_keys, "get_vault_keys_client", lambda: _Vault())
    monkeypatch.setattr(my_keys, "get_current_allowed_clusters", lambda: None)
    return recorded


async def test_a_download_is_recorded_with_its_owner(monkeypatch, audits) -> None:
    monkeypatch.setattr(my_keys, "get_key_from_vault", lambda _v, _k: (b"c", b"k"))
    monkeypatch.setattr(my_keys, "create_pfx", lambda _c, _k: b"pfx")
    await my_keys.download_key("key_a", _rbac=_user())
    (record,) = audits
    assert (record["action"], record["result"], record["target_host"]) == ("user.ssh_key.download", "success", "nuc2")
    assert record["details"]["owner_email"] == OWNER


async def test_a_refused_download_is_recorded_without_naming_a_cluster(audits) -> None:
    with pytest.raises(HTTPException):
        await my_keys.download_key("key_unknown", _rbac=_user())
    (record,) = audits
    assert (record["action"], record["result"], record["target_host"]) == ("user.ssh_key.download", "denied", None)


async def test_a_download_that_fails_after_authorizing_is_recorded(monkeypatch, audits) -> None:
    monkeypatch.setattr(my_keys, "get_key_from_vault", lambda _v, _k: (None, None))
    with pytest.raises(HTTPException):
        await my_keys.download_key("key_a", _rbac=_user())
    (record,) = audits
    assert record["result"] == "failure" and record["error_code"] == "404"


async def test_a_refused_create_is_recorded(monkeypatch, audits) -> None:
    class _Service:
        class connection_manager:  # noqa: N801 - mirrors the service's attribute
            @staticmethod
            def get_client(_node):
                return None

    monkeypatch.setattr(my_keys, "verify_cluster_access", lambda _c: None)
    request = my_keys.KeyCreateRequest(name="ops", node="nuc2", auth_level="standard")
    with pytest.raises(HTTPException):
        await my_keys.create_key(request, lxd_service=_Service(), _=None, _rbac=_user(), db=None)
    (record,) = audits
    assert (record["action"], record["result"], record["target_host"]) == ("user.ssh_key.create", "failure", "nuc2")


def test_an_audit_actor_carries_its_client_address_and_agent() -> None:
    context = RequestContext(client_ip="203.0.113.7", user_agent="Mozilla/5.0\x00\x1b[31m" + "x" * 400)
    context.extra.update({"client_ip_source": "x-forwarded-for", "client_proxy_trusted": True})
    client = context.get_actor()["client"]
    assert client["ip"] == "203.0.113.7" and client["ip_source"] == "x-forwarded-for" and client["proxy_trusted"] is True
    assert len(client["user_agent"]) == 256 and "\x00" not in client["user_agent"] and "\x1b" not in client["user_agent"]


def _record(target_type: str) -> Dict[str, Any]:
    return {
        "@timestamp": "2026-11-01T00:00:00Z", "action": "user.ssh_key.download", "result": "success",
        "target": {"type": target_type, "id": "key_a", "host": "nuc2"},
        "actor": {"user_id": OWNER, "role": "partner", "client": {"ip": "203.0.113.7", "user_agent": "Firefox"}},
        "details": {"name": "ops"},
    }


def test_only_a_key_row_keeps_the_client() -> None:
    assert record_to_log_row(_record("ssh_key"))["details"]["client"]["ip"] == "203.0.113.7"
    other = record_to_log_row({**_record("instance"), "action": "instance.start"})
    assert "client" not in (other["details"] or {}), "wider rows never carry an address"


def test_key_rows_are_kept_for_their_own_retention() -> None:
    captured: List[Any] = []

    class _Db:
        async def execute(self, statement, params=None):
            captured.append((str(statement), params))

            class _R:
                rowcount = 0

            return _R()

    import asyncio

    asyncio.run(repository_sweeps.prune_batch(_Db(), task_days=30, log_days=30, key_days=365))
    sql, params = captured[0]
    assert "resource_type NOT IN ('ssh_key', 'ssh_key_cleanup')" in sql
    assert params["key_days"] == 365 and params["days"] == 30
    asyncio.run(repository_sweeps.prune_batch(_Db(), task_days=30, log_days=90, key_days=10))
    assert captured[-2][1]["key_days"] == 90, "a key row is never kept for less than any other row"


def test_a_failed_create_is_recorded_as_a_request_never_as_a_key_id(monkeypatch) -> None:
    """Security review 2026-10-27: a request named after another owner's key id must not land in
    that key's trail, so a failed create has its own type and a prefixed id."""
    from app.routers import my_keys

    events = []
    monkeypatch.setattr(my_keys.activity_logger, "log_activity", lambda **kwargs: events.append(kwargs))
    request = my_keys.KeyCreateRequest(name="key_victim", node="nuc2", project="default", auth_level="standard", expire_days=1)
    my_keys._audit_create_failure(request, "vault_unavailable", "Vault did not answer")
    assert events[0]["target_type"] == "ssh_key_request"
    assert events[0]["target_id"] == "request:key_victim"
    from app.core.task_scope import KEY_RESOURCE_TYPES

    assert "ssh_key_request" in KEY_RESOURCE_TYPES, "still visible only to its actor and an Administrator"
