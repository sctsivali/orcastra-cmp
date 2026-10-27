"""A project deleted through the raw proxy takes its access rules with it.

OrcaHub removes a buyer's project at the end of the retention window with a raw
`DELETE /1.0/projects/<name>`. The raw path sent it straight to LXD, so the tenant's
`auto:<project>` rule outlived the project, kept the cluster in the tenant's scope, and the buyer
still saw the cluster on Overview with no project and no instance (2026-10-27). The raw
delete now runs inside the same deletion core as the route and the structured proxy, and the raw
caller still receives LXD's own answer.
"""

from __future__ import annotations

import inspect
from typing import Any, List, Optional

import pytest
import requests
from pylxd.exceptions import LXDAPIException

from app.core.blocking import BlockingTimeout
from app.services import project_deletion, raw_project_delete
from app.services.lxd.exceptions import (
    LXDOperationError,
    LXDOperationUnknownError,
    LXDProjectNotFoundError,
)
from app.services.raw_project_delete import (
    RawDeleteAnswer,
    RawDeleteRefused,
    raw_project_delete_target,
    send_and_classify,
)
from tests._project_delete_fakes import durable_deletes
from tests._project_grant_fakes import GrantSession, Rule

HOST = "nuc2"


class _Response:
    def __init__(self, status: int, body: Any = None):
        self.status_code = status
        self._body = body if body is not None else {"type": "sync", "status_code": status, "metadata": {}}

    def json(self) -> Any:
        return self._body


def _once(response: Any = None, error: Optional[BaseException] = None):
    async def call():
        if error is not None:
            raise error
        return response

    return call


async def _no_wait(_response: Any) -> None:
    return None


def _wait_raising(error: BaseException):
    async def wait(_response: Any) -> None:
        raise error

    return wait


# ── which requests are project deletes ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method, path, expected",
    [
        ("DELETE", "/1.0/projects/shop-prod", "shop-prod"),
        ("delete", "/1.0/projects/shop-prod?force=1", "shop-prod"),
        ("DELETE", "/1.0/projects/shop%2Dprod", "shop-prod"),
        ("DELETE", "/1.0/projects", None),
        ("DELETE", "/1.0/projects/shop-prod/state", None),
        ("POST", "/1.0/projects/shop-prod", None),
        ("DELETE", "/1.0/instances/web", None),
        ("DELETE", "/1.0/projects/../certificates", None),
    ],
)
def test_only_a_delete_of_one_project_is_recognised(method: str, path: str, expected: Optional[str]) -> None:
    assert raw_project_delete_target(method, path) == expected


# ── LXD's answer, classified ───────────────────────────────────────────────────────────────


async def test_a_sync_success_keeps_lxds_answer() -> None:
    answer = RawDeleteAnswer()
    await send_and_classify("web", answer, request_once=_once(_Response(200)), wait_operation=_no_wait)
    assert answer.status_code == 200 and answer.data["status_code"] == 200


async def test_a_404_says_the_project_is_already_gone() -> None:
    answer = RawDeleteAnswer()
    with pytest.raises(LXDProjectNotFoundError):
        await send_and_classify("web", answer, request_once=_once(_Response(404)), wait_operation=_no_wait)
    assert answer.status_code == 404


async def test_lxds_own_refusal_is_a_refusal_with_its_answer_kept() -> None:
    body = {"type": "error", "error": "Only empty projects can be removed", "error_code": 400}
    answer = RawDeleteAnswer()
    with pytest.raises(RawDeleteRefused):
        await send_and_classify("web", answer, request_once=_once(_Response(400, body)), wait_operation=_no_wait)
    assert answer.status_code == 400 and answer.data == body


@pytest.mark.parametrize(
    "request_once",
    [
        _once(_Response(500)),
        _once(error=requests.exceptions.ConnectionError("read timed out")),
        _once(error=BlockingTimeout("raw_proxy", 20.0)),
    ],
    ids=["5xx", "transport", "deadline"],
)
async def test_a_request_that_may_have_landed_is_unknown(request_once) -> None:
    with pytest.raises(LXDOperationUnknownError):
        await send_and_classify("web", RawDeleteAnswer(), request_once=request_once, wait_operation=_no_wait)


async def test_an_accepted_delete_waits_for_its_operation() -> None:
    waited: List[Any] = []

    async def wait(response: Any) -> None:
        waited.append(response)

    accepted = _Response(202, {"type": "async", "operation": "/1.0/operations/abc"})
    answer = RawDeleteAnswer()
    await send_and_classify("web", answer, request_once=_once(accepted), wait_operation=wait)
    assert waited == [accepted] and answer.status_code == 202


async def test_a_failed_operation_is_a_refusal_and_a_stalled_one_is_unknown() -> None:
    failed = _Response(200, {"type": "sync", "metadata": {"status": "Failure", "err": "busy"}})
    accepted = _once(_Response(202, {"type": "async", "operation": "/1.0/operations/abc"}))
    with pytest.raises(LXDOperationError) as raised:
        await send_and_classify("web", RawDeleteAnswer(), request_once=accepted,
                                wait_operation=_wait_raising(LXDAPIException(failed)))
    assert not isinstance(raised.value, LXDOperationUnknownError)

    stalled = _Response(200, {"type": "error", "error": "Operation abc is still running after 10s", "metadata": None})
    with pytest.raises(LXDOperationUnknownError):
        await send_and_classify("web", RawDeleteAnswer(), request_once=accepted,
                                wait_operation=_wait_raising(LXDAPIException(stalled)))


# ── through the deletion core ──────────────────────────────────────────────────────────────


def _sender(answer: RawDeleteAnswer, response: Any = None, error: Optional[BaseException] = None):
    async def send() -> None:
        await send_and_classify("web", answer, request_once=_once(response, error), wait_operation=_no_wait)

    return send


async def test_a_raw_delete_that_lands_revokes_every_rule_naming_the_project(monkeypatch) -> None:
    fakes = durable_deletes(monkeypatch)
    session = GrantSession(rules=[Rule(7, HOST, "web"), Rule(8, HOST, "other")])
    answer = RawDeleteAnswer()

    outcome = await project_deletion.delete_project_and_forget(
        session, object(), HOST, "web", origin="proxy", requester="key-orcahub", send=_sender(answer, _Response(200))
    )

    assert outcome.already_gone is False
    assert [r.project_name for r in session.deleted if isinstance(r, Rule)] == ["web"]
    assert [r.project_name for r in session.rules] == ["other"]
    (row,) = fakes.rows()
    assert (row.state, row.origin, row.requester) == ("settled", "proxy", "key-orcahub")
    assert answer.status_code == 200


async def test_a_raw_delete_lxd_refused_keeps_the_rules_and_no_row(monkeypatch) -> None:
    fakes = durable_deletes(monkeypatch)
    session = GrantSession(rules=[Rule(7, HOST, "web")])
    answer = RawDeleteAnswer()

    with pytest.raises(RawDeleteRefused):
        await project_deletion.delete_project_and_forget(
            session, object(), HOST, "web", origin="proxy", send=_sender(answer, _Response(400, {"error": "x"}))
        )

    assert session.deleted == [] and fakes.rows() == []


async def test_a_raw_delete_with_an_unknown_outcome_waits_for_the_sweeper(monkeypatch) -> None:
    fakes = durable_deletes(monkeypatch)
    rule = Rule(7, HOST, "web")
    session = GrantSession(rules=[rule])

    with pytest.raises(project_deletion.ProjectDeleteUnknown):
        await project_deletion.delete_project_and_forget(
            session, object(), HOST, "web", origin="proxy",
            send=_sender(RawDeleteAnswer(), error=requests.exceptions.ConnectionError("reset")),
        )

    assert session.deleted == []
    (row,) = fakes.rows()
    assert row.state == "pending" and row.rule_ids == [rule.id]


async def test_a_raw_delete_of_a_project_already_gone_revokes_nothing(monkeypatch) -> None:
    """Naming a project that does not exist must not strip the rules another org staged for it."""
    fakes = durable_deletes(monkeypatch)
    session = GrantSession(rules=[Rule(7, HOST, "web")])
    answer = RawDeleteAnswer()

    outcome = await project_deletion.delete_project_and_forget(
        session, object(), HOST, "web", origin="proxy", send=_sender(answer, _Response(404))
    )

    assert outcome.already_gone is True
    assert session.deleted == [] and fakes.rows() == []
    assert answer.status_code == 404, "the caller still reads LXD's 404, which OrcaHub treats as done"


# ── the route ──────────────────────────────────────────────────────────────────────────────


def test_the_raw_proxy_sends_a_project_delete_through_the_core() -> None:
    from app.routers import integrations

    route = inspect.getsource(integrations.raw_proxy)
    branch = route.index("raw_project_delete_target(method, request.path)")
    assert branch < route.index("raw_request,"), "the branch must come before the plain send"
    assert "_raw_project_delete(" in route
    helper = inspect.getsource(raw_project_delete.delete_through_core)
    assert "project_deletion.spawn(" in helper and "asyncio.shield(task)" in helper


class _Request:
    cluster_id = HOST
    path = "/1.0/projects/web"
    body = None
    external_user_email = None


@pytest.fixture
def route(monkeypatch):
    from app.routers import integrations

    audits: List[Any] = []
    monkeypatch.setattr(integrations, "_audit_raw_proxy", lambda *a, **k: audits.append((a, k)))
    monkeypatch.setattr(integrations.health, "clear", lambda _c: None)

    class _Cache:
        def __getattr__(self, _name):
            return lambda *a, **k: None

    monkeypatch.setattr(integrations, "get_cache_manager", lambda: _Cache())
    integrations._test_audits = audits
    return integrations


def _core(monkeypatch, route, outcome: Any):
    async def run(*_args, **_kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(route, "delete_through_core", run)


async def test_the_raw_caller_reads_lxds_own_answer(monkeypatch, route) -> None:
    _core(monkeypatch, route, RawDeleteAnswer(200, {"type": "sync", "status_code": 200}))
    response = await route._raw_project_delete(object(), object(), _Request(), {"id": "k"}, "default", "web", "url")
    assert response.status_code == 200 and response.data == {"type": "sync", "status_code": 200}


async def test_an_unknown_outcome_is_504_with_the_still_running_advice(monkeypatch, route) -> None:
    _core(monkeypatch, route, project_deletion.ProjectDeleteUnknown(HOST, "web", frozenset()))
    response = await route._raw_project_delete(object(), object(), _Request(), {"id": "k"}, "default", "web", "url")
    assert response.status_code == 504 and "Do not retry yet" in response.error


@pytest.mark.parametrize(
    "error, code",
    [
        (project_deletion.ProjectDeletesBacklogged(HOST), "project_deletes_backlogged"),
        (__import__("app.services.shared_cluster_audit", fromlist=["x"]).ClusterConfigurationBusy(HOST), "cluster_busy"),
    ],
)
async def test_a_busy_cluster_is_409_with_a_code(monkeypatch, route, error, code) -> None:
    from fastapi import HTTPException

    _core(monkeypatch, route, error)
    with pytest.raises(HTTPException) as raised:
        await route._raw_project_delete(object(), object(), _Request(), {"id": "k"}, "default", "web", "url")
    assert raised.value.status_code == 409 and raised.value.detail["code"] == code
