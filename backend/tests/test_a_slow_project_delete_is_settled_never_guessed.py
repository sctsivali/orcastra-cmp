"""A project delete LXD is still doing is settled by an exact read, and never leaks the transport.

Reproduced live on 2026-10-23: a delete sent seconds after the project's last instance went took
longer than the pooled client's 8 s read. The route answered 400 with
`HTTPSConnectionPool(host='100.66.12.102', ...) Read timed out`, the project stayed, and the call
had run on the event loop, freezing the worker for those 8 s. Grants must go only once the
project is known to be gone, and a follow-up must never take a same-name newcomer's rules.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, List

import pytest
from fastapi import HTTPException
import requests
from pylxd.exceptions import LXDAPIException, NotFound

from app.services import project_deletion, project_grants
from app.services.lxd import project_delete
from app.services.lxd.exceptions import (
    LXDConnectionError,
    LXDOperationError,
    LXDOperationUnknownError,
    LXDProjectNotFoundError,
)
from app.services.shared_cluster_audit import ClusterConfigurationBusy
from tests._project_grant_fakes import GrantSession, Rule, partner, projects_client

HOST = "nuc2"
LEAK = "HTTPSConnectionPool(host='100.66.12.102', port=8443): Read timed out. (read timeout=8)"


class _Response:
    def __init__(self, status: int, body: Any = None):
        self.status_code = status
        self._body = body or {"type": "sync", "metadata": {}}

    def json(self) -> Any:
        return self._body


def _api_error(status: int, message: str) -> LXDAPIException:
    response = _Response(status, {"type": "error", "error": message, "error_code": status})
    error = LXDAPIException(response)
    error.response = response
    return error


class _ProjectNode:
    def __init__(self, delete: Any, get: Any):
        self._delete, self._get = delete, get
        self.delete_timeouts: List[Any] = []

    def delete(self, timeout=None):
        self.delete_timeouts.append(timeout)
        return self._delete()

    def get(self, timeout=None):
        return self._get()


class _Client:
    def __init__(self, node: _ProjectNode):
        projects = {"web": node}
        self.api = type("Api", (), {"projects": projects})()


def _client_for(node: _ProjectNode):
    return lambda _host: _Client(node)


def _raise(error: BaseException):
    def call():
        raise error

    return call


# ── the LXD calls ─────────────────────────────────────────────────────────────────────────


def test_the_delete_is_sent_with_its_own_budget_not_the_pooled_eight_seconds() -> None:
    node = _ProjectNode(delete=lambda: _Response(200), get=lambda: _Response(200))
    project_delete.delete_project_sync(_client_for(node), HOST, "web")
    assert node.delete_timeouts == [project_delete.DELETE_TIMEOUT]
    assert project_delete.DELETE_TIMEOUT[1] > 8


def test_a_read_timeout_after_the_delete_was_sent_is_unknown_never_a_failure() -> None:
    node = _ProjectNode(delete=_raise(requests.exceptions.ConnectionError(LEAK)), get=lambda: _Response(200))
    with pytest.raises(LXDOperationUnknownError) as raised:
        project_delete.delete_project_sync(_client_for(node), HOST, "web")
    assert "100.66" not in str(raised.value)


def test_lxds_own_refusal_keeps_its_sentence() -> None:
    node = _ProjectNode(
        delete=_raise(_api_error(400, "Only empty projects can be removed")), get=lambda: _Response(200)
    )
    with pytest.raises(LXDOperationError) as raised:
        project_delete.delete_project_sync(_client_for(node), HOST, "web")
    assert not isinstance(raised.value, LXDOperationUnknownError)
    assert "Only empty projects can be removed" in str(raised.value)


def test_a_project_already_gone_is_named_as_such() -> None:
    node = _ProjectNode(delete=_raise(NotFound(_Response(404))), get=lambda: _Response(404))
    with pytest.raises(LXDProjectNotFoundError):
        project_delete.delete_project_sync(_client_for(node), HOST, "web")


def test_the_probe_answers_only_from_a_200_or_a_404() -> None:
    present = _ProjectNode(delete=lambda: None, get=lambda: _Response(200))
    gone = _ProjectNode(delete=lambda: None, get=_raise(NotFound(_Response(404))))
    broken = _ProjectNode(delete=lambda: None, get=_raise(requests.exceptions.ReadTimeout(LEAK)))
    assert project_delete.project_exists_sync(_client_for(present), HOST, "web") is True
    assert project_delete.project_exists_sync(_client_for(gone), HOST, "web") is False
    with pytest.raises(LXDOperationUnknownError):
        project_delete.project_exists_sync(_client_for(broken), HOST, "web")


@pytest.mark.asyncio
async def test_the_manager_settles_a_timed_out_delete_by_reading_it(monkeypatch) -> None:
    from app.services.lxd import projects as project_service

    reads = iter([False])
    manager = project_service.ProjectManager(connection_manager=None)

    async def unknown(*_args, **_kwargs):
        raise LXDOperationUnknownError("delete", "web")

    async def exists(_host, _name):
        return next(reads)

    monkeypatch.setattr(project_service, "_run_project_blocking", unknown)
    monkeypatch.setattr(manager, "project_exists", exists)
    await manager.delete_project(HOST, "web")  # gone on the read: a delete, not an error

    monkeypatch.setattr(manager, "project_exists", lambda *_a: asyncio.sleep(0, result=True))
    with pytest.raises(LXDOperationUnknownError):
        await manager.delete_project(HOST, "web")


# ── the route ─────────────────────────────────────────────────────────────────────────────


class _Service:
    def __init__(self, error: BaseException | None = None):
        self.error = error
        self.deleted: List[str] = []

    async def delete_project(self, host, name):
        if self.error is not None:
            raise self.error
        self.deleted.append(f"{host}/{name}")


def _no_follow(monkeypatch) -> List[Any]:
    started: List[Any] = []
    monkeypatch.setattr(
        project_deletion, "follow_unknown_delete", lambda service, unknown, finish, **_k: started.append(unknown)
    )
    return started


def test_an_unknown_outcome_is_503_project_write_unknown_with_no_address(monkeypatch) -> None:
    started = _no_follow(monkeypatch)
    session = GrantSession(rules=[Rule(7, HOST, "web")])
    client = projects_client(_Service(LXDOperationUnknownError("delete", "web")), monkeypatch, session, user=partner())

    r = client.delete(f"/projects/{HOST}/web")

    assert r.status_code == 503, r.text
    assert r.json()["detail"]["code"] == "project_write_unknown"
    assert "100.66" not in r.text and "HTTPSConnectionPool" not in r.text
    assert len(started) == 1 and started[0].rule_ids
    assert session.deleted == [], "no rule is revoked while the outcome is unknown"


def test_a_404_with_nothing_pending_revokes_nothing_and_announces_nothing(monkeypatch) -> None:
    """Anyone with a cluster-wide delete could otherwise strip every organization's staged rules."""
    from app.services.webhooks import emitter

    events: List[Any] = []

    async def record(**kwargs):
        events.append(kwargs)

    monkeypatch.setattr(project_deletion, "_pending", {})
    session = GrantSession(rules=[Rule(7, HOST, "web")])
    client = projects_client(_Service(LXDProjectNotFoundError("web")), monkeypatch, session, user=partner())
    monkeypatch.setattr(emitter, "emit_resource_event", record)

    r = client.delete(f"/projects/{HOST}/web")

    assert r.status_code == 200, r.text
    assert "already deleted" in r.json()["message"]
    assert session.deleted == [], "a rule another organization staged for the name stays"
    assert events == [], "no project.deleted for a project this call did not delete"


def test_a_404_that_lands_a_pending_follow_up_revokes_its_captured_rules(monkeypatch) -> None:
    pending = project_deletion._FollowUp(rule_ids={41})
    monkeypatch.setattr(project_deletion, "_pending", {(HOST, "web"): pending})
    revoked: List[Any] = []

    async def forget(_db, **kwargs):
        revoked.append(kwargs)
        return ""

    monkeypatch.setattr(project_grants, "forget_deleted_project", forget)
    client = projects_client(_Service(LXDProjectNotFoundError("web")), monkeypatch, GrantSession(), user=partner())

    r = client.delete(f"/projects/{HOST}/web")

    assert r.status_code == 200, r.text
    assert revoked == [{"cluster_id": HOST, "project_name": "web", "only_rule_ids": frozenset({41})}]
    assert project_deletion._pending == {}


def test_a_transport_failure_never_reaches_the_body(monkeypatch) -> None:
    client = projects_client(_Service(LXDConnectionError(LEAK)), monkeypatch, GrantSession(), user=partner())
    r = client.delete(f"/projects/{HOST}/web")
    assert r.status_code == 503
    assert "100.66" not in r.text


def test_a_cluster_held_by_another_change_is_409_not_a_hang(monkeypatch) -> None:
    @asynccontextmanager
    async def busy(*_args, **_kwargs):
        raise ClusterConfigurationBusy(HOST)
        yield  # pragma: no cover

    monkeypatch.setattr(project_deletion, "serialize_cluster_configuration", busy)
    client = projects_client(_Service(), monkeypatch, GrantSession(), user=partner())
    r = client.delete(f"/projects/{HOST}/web")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "cluster_busy"


# ── the follow-up ─────────────────────────────────────────────────────────────────────────


class _Probe:
    def __init__(self, answers):
        self.answers = list(answers)

    async def project_exists(self, _host, _name):
        answer = self.answers.pop(0) if self.answers else self.answers_default
        if isinstance(answer, BaseException):
            raise answer
        return answer

    answers_default = True


@asynccontextmanager
async def _sessions():
    yield object()


@asynccontextmanager
async def _lock(*_args, **_kwargs):
    yield


async def _run_follow(monkeypatch, probe: _Probe) -> tuple[List[Any], List[str]]:
    revoked: List[Any] = []
    finished: List[str] = []

    async def forget(_db, **kwargs):
        revoked.append(kwargs)
        return ""

    async def finish():
        finished.append("done")

    monkeypatch.setattr(project_deletion, "FOLLOW_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(project_deletion, "FOLLOW_WINDOW_SECONDS", 0.2)
    monkeypatch.setattr(project_deletion, "serialize_cluster_configuration", _lock)
    monkeypatch.setattr(project_grants, "forget_deleted_project", forget)
    unknown = project_deletion.ProjectDeleteUnknown(HOST, "web", frozenset({11, 12}))
    assert project_deletion.follow_unknown_delete(probe, unknown, finish, session_maker=_sessions)
    await asyncio.gather(*list(project_deletion._background))
    return revoked, finished


@pytest.mark.asyncio
async def test_once_gone_the_follow_up_revokes_only_the_captured_rules(monkeypatch) -> None:
    revoked, finished = await _run_follow(monkeypatch, _Probe([True, LXDOperationUnknownError("delete", "web"), False, False]))
    assert revoked == [{"cluster_id": HOST, "project_name": "web", "only_rule_ids": frozenset({11, 12})}]
    assert finished == ["done"]


@pytest.mark.asyncio
async def test_a_same_name_project_back_under_the_lock_keeps_every_rule(monkeypatch) -> None:
    revoked, finished = await _run_follow(monkeypatch, _Probe([False, True]))
    assert revoked == [] and finished == []


@pytest.mark.asyncio
async def test_still_present_when_the_window_ends_revokes_nothing(monkeypatch) -> None:
    revoked, finished = await _run_follow(monkeypatch, _Probe([]))
    assert revoked == [] and finished == []


def test_the_captured_rule_filter_is_part_of_the_revoke_query() -> None:
    from sqlalchemy import and_, select

    from app.models.db_models import TenantPolicyRule

    seen: List[str] = []

    class _Recording(GrantSession):
        async def execute(self, stmt):
            seen.append(str(stmt))
            return await super().execute(stmt)

    asyncio.run(
        project_grants.revoke_project_grants(
            _Recording(), cluster_id=HOST, project_name="web", commit=False, only_rule_ids={11}
        )
    )
    assert "tenant_policy_rules.id IN" in seen[0]
    assert and_ and select and TenantPolicyRule  # imported for the statement shape above


@pytest.mark.asyncio
async def test_the_follow_ups_are_bounded_and_one_per_project(monkeypatch) -> None:
    started: List[Any] = []
    monkeypatch.setattr(project_deletion, "_pending", {})
    monkeypatch.setattr(project_deletion, "spawn", lambda coro: (coro.close(), started.append(1), _Done())[2])

    def unknown(host: str, name: str, ids):
        return project_deletion.ProjectDeleteUnknown(host, name, frozenset(ids))

    assert project_deletion.follow_unknown_delete(_Probe([]), unknown(HOST, "a", {1}), lambda: None)
    assert project_deletion.follow_unknown_delete(_Probe([]), unknown(HOST, "a", {2}), lambda: None)
    assert len(started) == 1, "a repeat joins the running follow-up"
    assert project_deletion._pending[(HOST, "a")].rule_ids == {1, 2}
    assert project_deletion.follow_unknown_delete(_Probe([]), unknown(HOST, "b", {3}), lambda: None)
    assert project_deletion.follow_unknown_delete(_Probe([]), unknown(HOST, "c", {4}), lambda: None) is False, (
        "one cluster cannot take every slot"
    )
    assert project_deletion.follow_unknown_delete(_Probe([]), unknown("other", "c", {4}), lambda: None)


class _Done:
    def add_done_callback(self, _callback) -> None:
        return None

    def cancel(self) -> None:
        return None


@pytest.mark.asyncio
async def test_a_cluster_the_breaker_gave_up_on_is_not_probed(monkeypatch) -> None:
    from app.services.lxd import health

    probes: List[str] = []

    class _Counting:
        async def project_exists(self, host, name):
            probes.append(host)
            return False

    monkeypatch.setattr(health, "is_tripped", lambda host: host == HOST)
    assert await project_deletion._exists(_Counting(), HOST, "web") is None
    assert probes == []


# ── the proxy door ────────────────────────────────────────────────────────────────────────


def test_a_proxy_failure_never_carries_transport_text() -> None:
    from app.routers.integrations import _proxy_error_text

    timeout = requests.exceptions.ConnectionError(LEAK)
    assert "100.66" not in _proxy_error_text(timeout, "timeout", "delete_project", HOST)
    assert "100.66" not in _proxy_error_text(RuntimeError(LEAK), "operation_failed", "stop_instance", HOST)
    refusal = _api_error(400, "Only empty projects can be removed")
    assert "Only empty projects" in _proxy_error_text(refusal, "invalid_request", "delete_project", HOST)


@pytest.mark.asyncio
async def test_the_proxy_deletes_through_the_shared_delete_and_follows_an_unknown_one(monkeypatch) -> None:
    from app.core import database
    from app.routers import integrations
    from tests._project_grant_fakes import NoCache

    calls: List[Any] = []
    followed: List[Any] = []
    announced: List[str] = []

    async def shared(db, service, host, name):
        calls.append((host, name))
        if name == "slow":
            raise project_deletion.ProjectDeleteUnknown(host, name, frozenset({3}))
        if name == "never-there":
            return project_deletion.ProjectDeleted(already_gone=True, revoke_note="")
        return project_deletion.ProjectDeleted(already_gone=False, revoke_note="")

    async def announce(host, project, name):
        announced.append(name)

    monkeypatch.setattr(project_deletion, "delete_project_and_forget", shared)
    monkeypatch.setattr(project_deletion, "follow_unknown_delete", lambda s, u, f, **_k: followed.append(u))
    monkeypatch.setattr(database, "async_session_maker", _sessions)
    monkeypatch.setattr(integrations, "get_cache_manager", lambda: NoCache())
    monkeypatch.setattr(integrations, "_announce_proxy_project_deleted", announce)

    def request(name: str):
        return integrations.ProxyRequest(
            cluster_id=HOST, operation="delete_project", project="default", params={"name": name}
        )

    done = await integrations._delete_project_for_proxy(object(), request("web"))
    assert done == {"deleted": True, "name": "web", "already_deleted": False}
    gone = await integrations._delete_project_for_proxy(object(), request("never-there"))
    assert gone["already_deleted"] is True
    with pytest.raises(project_deletion.ProjectDeleteUnknown):
        await integrations._delete_project_for_proxy(object(), request("slow"))
    assert calls == [(HOST, "web"), (HOST, "never-there"), (HOST, "slow")]
    assert [u.rule_ids for u in followed] == [frozenset({3})]
    assert announced == ["web"], "only a project this call deleted is announced"
    with pytest.raises(Exception):
        await integrations._delete_project_for_proxy(object(), request(".."))
    assert len(calls) == 3, "a bad name never reaches the delete"


@pytest.mark.asyncio
async def test_a_proxy_caller_that_leaves_does_not_cut_the_delete_short(monkeypatch) -> None:
    from app.core import database
    from app.routers import integrations

    release = asyncio.Event()
    finished: List[str] = []

    async def slow_delete(db, service, host, name):
        await release.wait()
        finished.append(name)
        return project_deletion.ProjectDeleted(already_gone=False, revoke_note="")

    async def announce(host, project, name):
        finished.append(f"announced {name}")

    monkeypatch.setattr(project_deletion, "delete_project_and_forget", slow_delete)
    monkeypatch.setattr(database, "async_session_maker", _sessions)
    monkeypatch.setattr(integrations, "_announce_proxy_project_deleted", announce)
    request = integrations.ProxyRequest(cluster_id=HOST, operation="delete_project", project="default", params={"name": "web"})

    caller = asyncio.ensure_future(integrations._delete_project_for_proxy(object(), request))
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    release.set()
    await asyncio.gather(*list(project_deletion._background))
    assert finished == ["web", "announced web"]


def test_a_dashboard_delete_whose_requester_left_still_announces(monkeypatch) -> None:
    import inspect

    from app.routers import projects

    source = inspect.getsource(projects.delete_project)
    cancelled = source.index("if request_cancelled:")
    assert source.index("project_deletion.spawn(_announce_project_deleted", cancelled) < source.index(
        "raise asyncio.CancelledError", cancelled
    )


def test_a_dot_name_is_the_callers_400_and_never_reaches_lxd() -> None:
    node = _ProjectNode(delete=lambda: pytest.fail("LXD was asked"), get=lambda: pytest.fail("LXD was asked"))
    for name in (".", ".."):
        with pytest.raises(HTTPException) as refused:
            project_delete.delete_project_sync(_client_for(node), HOST, name)
        assert refused.value.status_code == 400
        with pytest.raises(HTTPException):
            project_delete.project_exists_sync(_client_for(node), HOST, name)
