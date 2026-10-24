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
from datetime import datetime, timedelta, timezone
from typing import Any, List

import pytest
from fastapi import HTTPException
import requests
from pylxd.exceptions import LXDAPIException, NotFound

from app.services import (
    project_delete_announce,
    project_delete_store,
    project_delete_sweeper,
    project_deletion,
    project_grants,
)
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


def _open_row(fakes, name: str, rule_ids, *, state: str = "pending", lease_seconds: float = -1.0,
              deadline_minutes: float = 10.0, origin: str = "route"):
    """An open follow-up as an earlier request (or a worker that died) left it."""
    from app.models.project_delete_models import ProjectDeleteFollowup

    now = datetime.now(timezone.utc)
    row = ProjectDeleteFollowup(
        cluster_id=HOST, project_name=name, origin=origin, requester="qa-earlier", event_id=f"ev-{name}",
        rule_ids=sorted(rule_ids), state=state, lease_until=now + timedelta(seconds=lease_seconds),
        next_check_at=now - timedelta(seconds=1), deadline_at=now + timedelta(minutes=deadline_minutes),
    )
    fakes.db.sync.add(row)
    fakes.db.sync.commit()
    return row


def test_an_unknown_outcome_is_503_and_leaves_a_pending_row_with_the_captured_rules(monkeypatch) -> None:
    rule = Rule(7, HOST, "web")
    session = GrantSession(rules=[rule])
    client = projects_client(_Service(LXDOperationUnknownError("delete", "web")), monkeypatch, session, user=partner())

    r = client.delete(f"/projects/{HOST}/web")

    assert r.status_code == 503, r.text
    assert r.json()["detail"]["code"] == "project_write_unknown"
    assert "100.66" not in r.text and "HTTPSConnectionPool" not in r.text
    rows = client.deletes.rows()
    assert [(row.state, row.rule_ids, row.origin, row.requester) for row in rows] == [
        ("pending", [rule.id], "route", "qa-partner")
    ], "the sweeper, in any worker, owns it from here"
    assert session.deleted == [], "no rule is revoked while the outcome is unknown"
    assert client.deletes.announced == []


def test_a_404_with_nothing_pending_revokes_nothing_announces_nothing_and_keeps_no_row(monkeypatch) -> None:
    """Anyone with a cluster-wide delete could otherwise strip every organization's staged rules."""
    session = GrantSession(rules=[Rule(7, HOST, "web")])
    client = projects_client(_Service(LXDProjectNotFoundError("web")), monkeypatch, session, user=partner())

    r = client.delete(f"/projects/{HOST}/web")

    assert r.status_code == 200, r.text
    assert "already deleted" in r.json()["message"]
    assert session.deleted == [], "a rule another organization staged for the name stays"
    assert client.deletes.announced == [], "no project.deleted for a project this call did not delete"
    assert client.deletes.rows() == []


def test_a_404_that_lands_an_earlier_delete_revokes_only_what_that_delete_captured(monkeypatch) -> None:
    revoked: List[Any] = []

    async def forget(_db, **kwargs):
        revoked.append(kwargs)
        return ""

    monkeypatch.setattr(project_grants, "forget_deleted_project", forget)
    staged_since = Rule(9, HOST, "web")
    client = projects_client(
        _Service(LXDProjectNotFoundError("web")), monkeypatch, GrantSession(rules=[staged_since]), user=partner()
    )
    _open_row(client.deletes, "web", {41})

    r = client.delete(f"/projects/{HOST}/web")

    assert r.status_code == 200, r.text
    assert revoked == [{"cluster_id": HOST, "project_name": "web", "only_rule_ids": frozenset({41})}], (
        "the rule staged after the earlier delete landed belongs to a later project"
    )
    (row,) = client.deletes.rows()
    assert row.state == "settled" and row.rule_ids == [41]
    assert [(f.event_id, later) for f, later in client.deletes.announced] == [("ev-web", True)]


def test_a_real_delete_revokes_every_rule_and_announces_from_its_row(monkeypatch) -> None:
    session = GrantSession(rules=[Rule(7, HOST, "web")])
    service = _Service()
    client = projects_client(service, monkeypatch, session, user=partner())

    r = client.delete(f"/projects/{HOST}/web")

    assert r.status_code == 200, r.text
    assert service.deleted == [f"{HOST}/web"]
    assert [d.project_name for d in session.deleted if isinstance(d, Rule)] == ["web"]
    (row,) = client.deletes.rows()
    assert row.state == "settled", "settled until its announcement has gone out"
    ((announced, later),) = client.deletes.announced
    assert announced.event_id == row.event_id and later is False
    assert client.deletes.cleared == ["route"]


def test_a_refused_delete_keeps_no_row(monkeypatch) -> None:
    client = projects_client(
        _Service(LXDOperationError("Only empty projects can be removed")), monkeypatch, GrantSession(), user=partner()
    )
    r = client.delete(f"/projects/{HOST}/web")
    assert r.status_code == 400 and "Only empty projects" in r.text
    assert client.deletes.rows() == []


def test_a_transport_failure_never_reaches_the_body(monkeypatch) -> None:
    client = projects_client(_Service(LXDConnectionError(LEAK)), monkeypatch, GrantSession(), user=partner())
    r = client.delete(f"/projects/{HOST}/web")
    assert r.status_code == 503
    assert "100.66" not in r.text
    assert client.deletes.rows() == []


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


def test_the_route_owes_nothing_after_the_shielded_delete_returns() -> None:
    import inspect

    from app.routers import projects

    route = inspect.getsource(projects.delete_project)
    assert "_finish_critical_write(" in route and "project_deletion.delete_project_and_forget(" in route
    assert "_announce" not in route and "schedule_orcahub_event" not in route, (
        "everything a delete owes happens inside the shielded call, so a requester that left costs nothing"
    )
    service = inspect.getsource(project_deletion.delete_project_and_forget)
    assert service.count("await _settled(") == 2


# ── the intent row and the sweeper ────────────────────────────────────────────────────────


class _Probe:
    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = 0

    async def project_exists(self, _host, _name):
        self.asked += 1
        answer = self.answers.pop(0) if self.answers else True
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture
def durable(monkeypatch):
    from tests._project_delete_fakes import durable_deletes

    fakes = durable_deletes(monkeypatch)
    revoked: List[Any] = []

    async def forget(_db, **kwargs):
        revoked.append(kwargs)
        return ""

    @asynccontextmanager
    async def lock(*_args, **_kwargs):
        yield

    monkeypatch.setattr(project_grants, "forget_deleted_project", forget)
    monkeypatch.setattr(project_delete_sweeper, "serialize_cluster_configuration", lock)
    fakes.revoked = revoked
    return fakes


@pytest.mark.asyncio
async def test_a_crash_between_sending_and_the_answer_is_settled_by_another_worker(durable) -> None:
    opened = await project_delete_store.open_intent(
        HOST, "web", rule_ids={11, 12}, origin="route", requester="qa-partner", scope_project=None
    )
    assert opened.created_here and opened.state == "sending"
    assert await project_delete_store.claim_due(8) == [], "a live request still holds its lease"

    (row,) = durable.rows()
    row.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)  # the worker died
    durable.db.sync.commit()
    (claimed,) = await project_delete_store.claim_due(8)

    outcome = await project_delete_sweeper.finish(_Probe([False, False]), claimed, durable.maker)

    assert outcome == "settled"
    assert durable.revoked == [{"cluster_id": HOST, "project_name": "web", "only_rule_ids": frozenset({11, 12})}]
    assert [(f.requester, later) for f, later in durable.announced] == [("qa-partner", True)]
    assert durable.rows()[0].state == "settled"


@pytest.mark.asyncio
async def test_a_same_name_project_back_under_the_lock_keeps_every_rule(durable) -> None:
    _open_row(durable, "web", {11})
    (claimed,) = await project_delete_store.claim_due(8)
    assert await project_delete_sweeper.finish(_Probe([False, True]), claimed, durable.maker) == "superseded"
    assert durable.revoked == [] and durable.announced == []
    assert durable.rows()[0].state == "expired"


@pytest.mark.asyncio
async def test_still_present_it_waits_and_at_the_deadline_expires_with_nothing_revoked(durable) -> None:
    _open_row(durable, "web", {11})
    (claimed,) = await project_delete_store.claim_due(8)
    assert await project_delete_sweeper.finish(_Probe([True]), claimed, durable.maker) == "waiting"
    (row,) = durable.rows()
    assert row.state == "pending" and row.lease_until is None
    assert await project_delete_store.claim_due(8) == [], "rechecked later, not on every pass"

    row.next_check_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    row.deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    durable.db.sync.commit()
    (claimed,) = await project_delete_store.claim_due(8)
    probe = _Probe([False])
    assert await project_delete_sweeper.finish(probe, claimed, durable.maker) == "expired"
    assert probe.asked == 0 and durable.revoked == []


@pytest.mark.asyncio
async def test_a_pass_takes_one_row_per_cluster_and_leases_it(durable) -> None:
    _open_row(durable, "a", {1})
    _open_row(durable, "b", {2})
    from app.models.project_delete_models import ProjectDeleteFollowup

    other = durable.rows()[1]
    other.cluster_id = "other"
    durable.db.sync.commit()
    first = await project_delete_store.claim_due(8)
    assert sorted(f.cluster_id for f in first) == [HOST, "other"]
    _open_row(durable, "c", {3})
    second = await project_delete_store.claim_due(8)
    assert [f.project_name for f in second] == ["c"], "leased rows are not claimed twice"
    assert ProjectDeleteFollowup


@pytest.mark.asyncio
async def test_a_repeat_joins_the_open_row_and_the_caps_bound_the_rest(durable, monkeypatch) -> None:
    monkeypatch.setattr(project_delete_store, "MAX_OPEN_PER_CLUSTER", 2)

    async def intent(host: str, name: str, ids):
        return await project_delete_store.open_intent(
            host, name, rule_ids=ids, origin="route", requester=None, scope_project=None
        )

    a = await intent(HOST, "a", {1})
    joined = await intent(HOST, "a", {2})
    assert a.created_here and not joined.created_here
    assert joined.id == a.id and joined.rule_ids == frozenset({1}), (
        "a joining call stores none of its rules unless its own delete ends unknown"
    )
    await project_delete_store.mark_pending(joined, merge_ids={2})
    assert durable.rows()[0].rule_ids == [1, 2]
    assert await intent(HOST, "b", {3}) is not None
    assert await intent(HOST, "c", {4}) is None, "one cluster cannot take every slot"
    assert await intent("other", "c", {4}) is not None


@pytest.mark.asyncio
async def test_a_cluster_the_breaker_gave_up_on_is_not_probed(monkeypatch) -> None:
    from app.services.lxd import health

    probe = _Probe([False])
    monkeypatch.setattr(health, "is_tripped", lambda host: host == HOST)
    assert await project_delete_sweeper._exists(probe, HOST, "web") is None
    assert probe.asked == 0


@pytest.mark.asyncio
async def test_a_failed_announcement_stays_settled_for_a_retry_with_the_same_event_id(monkeypatch) -> None:
    from app.services import orcahub_webhook
    from app.services.webhooks import emitter
    from tests._project_delete_fakes import durable_deletes

    fakes = durable_deletes(monkeypatch, record_announcements=False)
    _open_row(fakes, "web", {11}, state="settled")
    events: List[str] = []
    audits: List[str] = []
    fail = {"orcahub": True}

    async def webhook(**kwargs):
        events.append(kwargs["event_id"])
        return 1

    async def orcahub(**kwargs):
        if fail["orcahub"]:
            raise RuntimeError("OrcaHub answered 502")
        return 1

    monkeypatch.setattr(emitter, "emit_resource_event", webhook)
    monkeypatch.setattr(orcahub_webhook, "emit_orcahub_event_durable", orcahub)
    monkeypatch.setattr(
        project_delete_announce, "get_activity_logger",
        lambda: type("L", (), {"log_activity": lambda self, **kw: audits.append(kw["actor_user_id"])})(),
    )

    (claimed,) = await project_delete_store.claim_due(8)
    assert await project_delete_sweeper.finish(_Probe([]), claimed, fakes.maker) == "announced"
    (row,) = fakes.rows()
    assert row.state == "settled" and "502" in row.last_error and audits == [], (
        "the audit line waits until the webhook and OrcaHub have both gone out"
    )

    fail["orcahub"] = False
    row.next_check_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    fakes.db.sync.commit()
    (claimed,) = await project_delete_store.claim_due(8)
    await project_delete_sweeper.finish(_Probe([]), claimed, fakes.maker)
    assert fakes.rows()[0].state == "done"
    assert events == ["evt_ev-web", "evt_ev-web"], "the webhook dedupes a retry by the row's event id"
    assert audits == ["qa-earlier"], "attributed to the requester even after a restart"


def test_the_captured_rule_filter_is_part_of_the_revoke_query() -> None:
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


# ── the proxy door ────────────────────────────────────────────────────────────────────────


def test_a_proxy_failure_never_carries_transport_text() -> None:
    from app.routers.integrations import _proxy_error_text

    timeout = requests.exceptions.ConnectionError(LEAK)
    assert "100.66" not in _proxy_error_text(timeout, "timeout", "delete_project", HOST)
    assert "100.66" not in _proxy_error_text(RuntimeError(LEAK), "operation_failed", "stop_instance", HOST)
    refusal = _api_error(400, "Only empty projects can be removed")
    assert "Only empty projects" in _proxy_error_text(refusal, "invalid_request", "delete_project", HOST)


@asynccontextmanager
async def _sessions():
    yield object()


@pytest.mark.asyncio
async def test_the_proxy_deletes_through_the_shared_delete_as_its_own_origin(monkeypatch) -> None:
    from app.core import database
    from app.routers import integrations

    calls: List[Any] = []

    async def shared(db, service, host, name, **kwargs):
        calls.append((host, name, kwargs))
        if name == "slow":
            raise project_deletion.ProjectDeleteUnknown(host, name, frozenset({3}))
        if name == "never-there":
            return project_deletion.ProjectDeleted(already_gone=True, revoke_note="")
        return project_deletion.ProjectDeleted(already_gone=False, revoke_note="")

    monkeypatch.setattr(project_deletion, "delete_project_and_forget", shared)
    monkeypatch.setattr(database, "async_session_maker", _sessions)

    def request(name: str):
        return integrations.ProxyRequest(
            cluster_id=HOST, operation="delete_project", project="default", params={"name": name}
        )

    done = await integrations._delete_project_for_proxy(object(), request("web"), "oak_qa")
    assert done == {"deleted": True, "name": "web", "already_deleted": False}
    gone = await integrations._delete_project_for_proxy(object(), request("never-there"), "oak_qa")
    assert gone["already_deleted"] is True
    with pytest.raises(project_deletion.ProjectDeleteUnknown):
        await integrations._delete_project_for_proxy(object(), request("slow"), "oak_qa")
    assert [c[:2] for c in calls] == [(HOST, "web"), (HOST, "never-there"), (HOST, "slow")]
    assert calls[0][2] == {"origin": "proxy", "requester": "oak_qa", "scope_project": "default"}
    with pytest.raises(Exception):
        await integrations._delete_project_for_proxy(object(), request(".."), "oak_qa")
    assert len(calls) == 3, "a bad name never reaches the delete"


@pytest.mark.asyncio
async def test_a_proxy_caller_that_leaves_does_not_cut_the_delete_short(monkeypatch) -> None:
    from app.core import database
    from app.routers import integrations

    release = asyncio.Event()
    finished: List[str] = []

    async def slow_delete(db, service, host, name, **_kwargs):
        await release.wait()
        finished.append(name)
        return project_deletion.ProjectDeleted(already_gone=False, revoke_note="")

    monkeypatch.setattr(project_deletion, "delete_project_and_forget", slow_delete)
    monkeypatch.setattr(database, "async_session_maker", _sessions)
    request = integrations.ProxyRequest(cluster_id=HOST, operation="delete_project", project="default", params={"name": "web"})

    caller = asyncio.ensure_future(integrations._delete_project_for_proxy(object(), request, "oak_qa"))
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    release.set()
    await asyncio.gather(*list(project_deletion._background))
    assert finished == ["web"]


def test_a_dot_name_is_the_callers_400_and_never_reaches_lxd() -> None:
    node = _ProjectNode(delete=lambda: pytest.fail("LXD was asked"), get=lambda: pytest.fail("LXD was asked"))
    for name in (".", ".."):
        with pytest.raises(HTTPException) as refused:
            project_delete.delete_project_sync(_client_for(node), HOST, name)
        assert refused.value.status_code == 400
        with pytest.raises(HTTPException):
            project_delete.project_exists_sync(_client_for(node), HOST, name)
