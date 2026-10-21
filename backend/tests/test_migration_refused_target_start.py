"""A target start the daemon definitively refuses is an abort, not an unknown outcome.

Found live (final matrix, cell I12): LXD accepted the start of a staged target, then its start
validation failed ("MAC address already defined on another NIC"). The job raised MigrationUnknown,
so reconciliation re-adopted the target and started it again on every backoff, with the fenced
source down the whole time, until it gave up into manual recovery. A failed start that leaves the
target stopped proves the target never ran, so the abort runner may discard it and bring the
source back. A start whose outcome is genuinely unclear stays unknown.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.services.migrations import activation, rollback
from app.services.migrations.contracts import MigrationUnknown, TargetStartRefused
from app.services.migrations.project_policy import MIGRATION_JOB_MARKER

JOB = "a" * 32


def _job(**values: Any) -> SimpleNamespace:
    base = {
        "job_id": JOB,
        "scope": "instance",
        "intent": "move",
        "strategy": "same_cluster_copy",
        "source_cluster_id": "cluster-a",
        "source_project": "src",
        "target_cluster_id": "cluster-a",
        "target_project": "dst",
        "safe_result": {},
        "phase": "cutting_over",
    }
    base.update(values)
    return SimpleNamespace(**base)


def _resource(state: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        resource_id="r1",
        resource_type="instance",
        source_ref={"name": "web"},
        target_ref={"name": "web"},
        safe_result=state,
    )


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {"updates": [], "target_status": "Stopped"}

    async def blocking(function: Any, *args: Any, **kwargs: Any) -> Any:
        return "op-start"

    async def failed(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"settled": True, "failed": True, "err": 'Failed start validation for device "eth0": MAC address already defined on another NIC'}

    async def read(_client: Any, _project: str, name: str, _label: str) -> dict[str, Any]:
        return {"name": name, "status": seen["target_status"], "config": {MIGRATION_JOB_MARKER: JOB}}

    async def update(resource_id: str, **values: Any) -> None:
        seen["updates"].append(values)

    async def lease() -> None:
        return None

    monkeypatch.setattr(activation, "_blocking", blocking)
    monkeypatch.setattr(activation, "await_operation", failed)
    monkeypatch.setattr(activation, "_read", read)
    monkeypatch.setattr(activation, "_resource_update", update)
    monkeypatch.setattr(activation, "_assert_mutation_lease", lease)
    return seen


async def _start(state: dict[str, Any]) -> dict[str, Any]:
    return await activation._change_state(
        _job(), _resource(state), state, object(), cluster_id="cluster-a", project="dst", name="web",
        action="start", intent_key="target_start_attempted", kind="start_target",
    )


async def test_a_failed_start_that_leaves_the_target_stopped_is_a_refusal(daemon: dict[str, Any]) -> None:
    with pytest.raises(TargetStartRefused, match="MAC address already defined"):
        await _start({})

    recorded = daemon["updates"][-1]["safe_result"]
    assert recorded["target_start_attempted"] is True
    assert recorded["target_start_refused"] is True


async def test_only_a_first_start_can_be_a_refusal(daemon: dict[str, Any]) -> None:
    """A retry follows an attempt whose outcome was unknown: that one may have run the target."""
    with pytest.raises(MigrationUnknown) as caught:
        await _start({"target_start_attempted": True, "target_start_refused": True})
    assert not isinstance(caught.value, TargetStartRefused)
    assert daemon["updates"][0]["safe_result"]["target_start_refused"] is False


async def test_a_member_relocation_refusal_is_not_an_abort(daemon: dict[str, Any]) -> None:
    with pytest.raises(MigrationUnknown) as caught:
        await activation._change_state(
            _job(strategy="member_relocation"), _resource({}), {}, object(), cluster_id="cluster-a", project="dst",
            name="web", action="start", intent_key="target_start_attempted", kind="start_target",
        )
    assert not isinstance(caught.value, TargetStartRefused)


async def test_a_failed_start_with_the_target_running_stays_unknown(daemon: dict[str, Any]) -> None:
    daemon["target_status"] = "Running"
    with pytest.raises(MigrationUnknown) as caught:
        await _start({})
    assert not isinstance(caught.value, TargetStartRefused)


async def test_activation_hands_a_refused_start_to_the_abort_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    aborted: list[Any] = []

    async def noop(*_args: Any, **_kwargs: Any) -> Any:
        return True

    async def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise TargetStartRefused("start refused")

    async def abort(job: Any, worker_id: str, source_client: Any, target_client: Any, *, reason: Exception) -> None:
        aborted.append((job.job_id, worker_id, type(reason).__name__))

    async def retention(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("a refused start must never reach retention")

    monkeypatch.setattr(activation, "_validate_pair", noop)
    monkeypatch.setattr(activation, "_fenced_job_update", noop)
    monkeypatch.setattr(activation, "nic_fence_applies", lambda _job: False)
    monkeypatch.setattr(activation, "_activate", refuse)
    monkeypatch.setattr(activation, "enter_source_retention", retention)
    monkeypatch.setattr(rollback, "abort_before_activation", abort)

    await activation.finish_transfer(_job(), "worker-a", object(), object(), [(_resource({}), {}, None)])

    assert aborted == [(JOB, "worker-a", "TargetStartRefused")]


@pytest.fixture
def abort_seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {"manual": [], "deleted": [], "started": [], "finished": []}

    async def resources(_job_id: str) -> list[Any]:
        return [seen["resource"]]

    async def read(_client: Any, _project: str, name: str, _label: str) -> dict[str, Any]:
        return {"name": name, "status": "Stopped", "config": {MIGRATION_JOB_MARKER: JOB}}

    async def delete(_job: Any, _resource: Any, state: dict[str, Any], _client: Any, **kwargs: Any) -> dict[str, Any]:
        seen["deleted"].append(kwargs["name"])
        return state

    async def start_source(_job: Any, _resource: Any, state: dict[str, Any], _client: Any) -> dict[str, Any]:
        seen["started"].append("web")
        return state

    async def manual(_job: Any, _worker: str, *, code: str, message: str) -> None:
        seen["manual"].append(code)

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    class _Session:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_exc: Any) -> None:
            return None

    async def finish(_db: Any, _job: Any, **values: Any) -> None:
        seen["finished"].append(values)

    monkeypatch.setattr(rollback, "_resources", resources)
    monkeypatch.setattr(rollback, "_read", read)
    monkeypatch.setattr(rollback, "_delete", delete)
    monkeypatch.setattr(rollback, "_start_source_if_needed", start_source)
    monkeypatch.setattr(rollback, "_manual_recovery", manual)
    monkeypatch.setattr(rollback, "_resource_update", noop)
    monkeypatch.setattr(rollback, "remove_created_project_if_empty", noop)
    monkeypatch.setattr(rollback, "async_session_maker", _Session)
    monkeypatch.setattr(rollback, "finish_rollback", finish)
    monkeypatch.setattr(rollback, "_actor", lambda _job: {})
    return seen


async def test_the_abort_discards_a_refused_target_and_restarts_the_source(abort_seams: dict[str, Any]) -> None:
    abort_seams["resource"] = _resource({
        "original_status": "running",
        "source_identity_hash": "pinned",
        "target_submission_intent": True,
        "target_start_attempted": True,
        "target_start_refused": True,
    })

    await rollback.run_abort(_job(safe_result={"abort_code": "target_start_refused"}), "worker-a", object(), object())

    assert abort_seams["manual"] == []
    assert abort_seams["deleted"] == ["web"]
    assert abort_seams["started"] == ["web"]
    assert abort_seams["finished"][0]["phase"] == "failed"
    assert abort_seams["finished"][0]["error_code"] == "target_start_refused"


async def test_a_refused_target_that_is_not_the_adopted_object_is_kept(
    abort_seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rollback.executor, "instance_identity_fingerprint", lambda _record: "someone-else")
    abort_seams["resource"] = _resource({
        "original_status": "running",
        "source_identity_hash": "pinned",
        "target_identity_hash": "adopted",
        "target_submission_intent": True,
        "target_start_attempted": True,
        "target_start_refused": True,
    })

    await rollback.run_abort(_job(), "worker-a", object(), object())

    assert abort_seams["manual"] == ["abort_target_unproven"]
    assert abort_seams["deleted"] == []


async def test_a_refused_flag_beside_a_started_target_is_not_trusted(abort_seams: dict[str, Any]) -> None:
    abort_seams["resource"] = _resource({
        "original_status": "running",
        "source_identity_hash": "pinned",
        "target_submission_intent": True,
        "target_start_attempted": True,
        "target_start_refused": True,
        "target_started": True,
    })

    await rollback.run_abort(_job(), "worker-a", object(), object())

    assert abort_seams["manual"] == ["abort_after_activation"]
    assert abort_seams["deleted"] == []


async def test_a_target_that_may_have_run_still_needs_an_operator(abort_seams: dict[str, Any]) -> None:
    abort_seams["resource"] = _resource({
        "original_status": "running",
        "source_identity_hash": "pinned",
        "target_submission_intent": True,
        "target_start_attempted": True,
    })

    await rollback.run_abort(_job(), "worker-a", object(), object())

    assert abort_seams["manual"] == ["abort_after_activation"]
    assert abort_seams["deleted"] == []
