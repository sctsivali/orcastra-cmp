"""Outcomes that happen before a migration changed anything must not strand it.

A cluster that does not answer, an unreadable certificate store, or a consent that lapsed by
claim time must not park the job in manual recovery with its locks and concurrency slot held.
Operator actions that change no daemon (Release, Cancel, Recheck) work without both clusters
reachable, so such a job can still be released.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from app.services.break_glass_service import PinStoreUnavailable
from app.services.lxd_certs.pinning import TRUST_TOFU, PinState
from app.services.migrations import actions, context, repository, transfer, worker
from app.services.migrations.contracts import (
    MigrationClusterUnavailable,
    MigrationConflict,
    MigrationPreMutationRefusal,
)

PIN = "a" * 64


def _job(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "job_id": "job-1",
        "plan_id": "plan-1",
        "revision": 4,
        "fence_token": 3,
        "scope": "instance",
        "intent": "move",
        "strategy": "cross_cluster_pull",
        "phase": "queued",
        "status": "running",
        "safe_result": {},
        "source_cluster_id": "cluster-a",
        "target_cluster_id": "cluster-b",
        "source_project": "src",
        "target_project": "dst",
        "source_organization_id": 7,
        "target_organization_id": 7,
        "source_cluster_fingerprint": PIN,
        "target_cluster_fingerprint": PIN,
        "lease_expires_at": None,
        "locked_by": None,
        "retention_hours": 24,
        "requester_role": "partner",
        "actor_issuer": "iss",
        "actor_subject": "sub",
        "actor_email": "operator@example.test",
        "actor_provider_uuid": "uuid",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# ----------------------------------------------------------------------------- context


@pytest.mark.asyncio
async def test_a_cluster_that_does_not_answer_is_unavailable_not_an_http_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def client_for(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(context, "client_for", client_for)

    with pytest.raises(MigrationClusterUnavailable) as raised:
        await context._bound_clients(_job(), object(), PIN, PIN)

    assert raised.value.code == "cluster_unavailable"


@pytest.mark.asyncio
async def test_an_unreadable_certificate_store_is_a_retry_not_a_changed_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def pin_state(_cluster_id: str, *, strict: bool = False) -> Optional[PinState]:
        if strict:
            raise PinStoreUnavailable("Vault sealed")
        return None

    monkeypatch.setattr(context, "cluster_pin_state", pin_state)

    with pytest.raises(MigrationClusterUnavailable):
        await context._current_job_pins(_job())


@pytest.mark.asyncio
async def test_a_missing_pin_is_still_a_registration_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def pin_state(_cluster_id: str, *, strict: bool = False) -> Optional[PinState]:
        return None

    monkeypatch.setattr(context, "cluster_pin_state", pin_state)

    with pytest.raises(context.MigrationAuthorityError):
        await context._current_job_pins(_job())


# ----------------------------------------------------------------------------- worker


class _Session:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_exc: Any) -> None:
        return None


def _drive(monkeypatch: pytest.MonkeyPatch, job: SimpleNamespace, raised: BaseException) -> dict[str, Any]:
    """Run run_migration_job for one claimed job whose step raises `raised`; return the update."""
    captured: dict[str, Any] = {}

    async def claim_job(_db: Any, _job_id: str, _worker_id: str) -> SimpleNamespace:
        return job

    async def run_claimed(*_args: Any, **_kwargs: Any) -> None:
        raise raised

    async def fenced_update(_job: Any, _worker_id: str, **values: Any) -> bool:
        captured.update(values, _via="fenced")
        return True

    async def set_job(_db: Any, _job_id: str, **values: Any) -> bool:
        captured.update(values, _via="set_job")
        return True

    async def heartbeat(*_args: Any) -> None:
        return None

    async def drain() -> int:
        return 0

    monkeypatch.setattr(worker, "async_session_maker", lambda: _Session())
    monkeypatch.setattr(worker.repository, "claim_job", claim_job)
    monkeypatch.setattr(worker.repository, "set_job", set_job)
    monkeypatch.setattr(worker, "_run_claimed", run_claimed)
    monkeypatch.setattr(worker, "_fenced_job_update", fenced_update)
    monkeypatch.setattr(worker, "_heartbeat_loop", heartbeat)
    monkeypatch.setattr(worker, "drain_migration_audits", drain)

    async def untouched_journal(_job_id: str) -> list[Any]:
        return [SimpleNamespace(operation_refs=[], state="pending")]

    async def persisted(_db: Any, _job_id: str, **_kwargs: Any) -> Any:
        return SimpleNamespace(safe_result=dict(job.safe_result))

    monkeypatch.setattr(worker, "_resources", untouched_journal)
    monkeypatch.setattr(worker.repository, "get_job", persisted)
    return captured


@pytest.mark.asyncio
async def test_a_queued_job_whose_cluster_does_not_answer_stays_queued_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _drive(monkeypatch, _job(), MigrationClusterUnavailable("cluster-b did not answer."))

    await worker.run_migration_job("job-1", allow_mutation=True)

    assert captured["status"] == "pending"
    assert captured.get("phase", "queued") == "queued"
    assert captured["error_code"] == "cluster_unavailable"
    assert captured["locked_by"] is None
    assert captured["lease_expires_at"] > repository.utcnow()
    assert captured["safe_result"]["cluster_unavailable_retries"] == 1
    assert not captured.get("terminal")


@pytest.mark.asyncio
async def test_a_queued_job_fails_cleanly_once_its_cluster_stays_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job(safe_result={"cluster_unavailable_retries": worker._PRE_MUTATION_RETRY_LIMIT})
    captured = _drive(monkeypatch, job, MigrationClusterUnavailable("cluster-b did not answer."))

    await worker.run_migration_job("job-1", allow_mutation=True)

    assert captured["phase"] == "failed"
    assert captured["status"] == "failure"
    assert captured["terminal"] is True
    assert captured["error_code"] == "cluster_unavailable"


@pytest.mark.asyncio
async def test_an_executing_job_whose_cluster_does_not_answer_backs_off_instead_of_manual_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job(phase="initial_sync")
    captured = _drive(monkeypatch, job, MigrationClusterUnavailable("cluster-b did not answer."))

    await worker.run_migration_job("job-1", allow_mutation=True)

    assert captured["status"] == "unknown"
    assert captured["phase"] != "manual_recovery"


@pytest.mark.asyncio
async def test_a_refusal_before_any_change_fails_the_queued_job_and_frees_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refusal = MigrationPreMutationRefusal(
        "Required migration acknowledgements are missing: unmeasured-size-accepted",
        code="acknowledgement_required",
    )
    captured = _drive(monkeypatch, _job(), refusal)

    await worker.run_migration_job("job-1", allow_mutation=True)

    assert captured["phase"] == "failed"
    assert captured["status"] == "failure"
    assert captured["terminal"] is True
    assert captured["error_code"] == "acknowledgement_required"


@pytest.mark.asyncio
async def test_a_consent_that_no_longer_holds_at_claim_is_a_pre_mutation_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job()
    plan = SimpleNamespace(request_payload={
        "scope": "instance", "intent": "move",
        "source": {"cluster_id": "cluster-a", "project": "src", "resource_name": "web"},
        "target": {"cluster_id": "cluster-b", "project": "dst", "resource_name": "web"},
    })

    async def job_plan(_job: Any) -> Any:
        return plan

    async def resources(_job_id: str) -> list[Any]:
        return []

    async def fresh(*_args: Any) -> dict[str, Any]:
        return {"blockers": [], "warnings": [{"code": "source_size_unmeasured"}]}

    async def admit(*_args: Any) -> None:
        raise MigrationConflict("Required migration acknowledgements are missing: unmeasured-size-accepted")

    async def must_not_mutate(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("the job moved on after its admission was refused")

    monkeypatch.setattr(transfer, "_job_plan", job_plan)
    monkeypatch.setattr(transfer, "_resources", resources)
    monkeypatch.setattr(transfer, "_fresh_worker_preflight", fresh)
    monkeypatch.setattr(transfer, "_admit_worker_run", admit)
    monkeypatch.setattr(transfer, "_fenced_job_update", must_not_mutate)

    with pytest.raises(MigrationPreMutationRefusal) as raised:
        await transfer._perform_copy_or_cutover(job, "worker-a", object(), object())

    assert raised.value.code == "acknowledgement_required"


@pytest.mark.asyncio
async def test_fresh_blockers_at_claim_are_a_pre_mutation_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job()
    plan = SimpleNamespace(request_payload={
        "scope": "instance", "intent": "copy",
        "source": {"cluster_id": "cluster-a", "project": "src", "resource_name": "web"},
        "target": {"cluster_id": "cluster-b", "project": "dst", "resource_name": "web2"},
    })

    async def job_plan(_job: Any) -> Any:
        return plan

    async def resources(_job_id: str) -> list[Any]:
        return []

    async def fresh(*_args: Any) -> dict[str, Any]:
        return {"blockers": [{"code": "target_instance_exists"}], "warnings": []}

    monkeypatch.setattr(transfer, "_job_plan", job_plan)
    monkeypatch.setattr(transfer, "_resources", resources)
    monkeypatch.setattr(transfer, "_fresh_worker_preflight", fresh)

    with pytest.raises(MigrationPreMutationRefusal) as raised:
        await transfer._perform_copy_or_cutover(job, "worker-a", object(), object())

    assert raised.value.code == "preflight_blocked"


# ----------------------------------------------------------------------------- operator actions


class _Svc(actions.MigrationActionsMixin):
    def __init__(self) -> None:
        self.db = object()
        self.settings = SimpleNamespace(migration_recovery_actions_enabled=True)


def _authority(role: str) -> SimpleNamespace:
    return SimpleNamespace(
        role=SimpleNamespace(value=role),
        pin=SimpleNamespace(
            identity=SimpleNamespace(issuer="iss", subject="sub", email=f"{role}@example.test"),
            provider_uuid="uuid",
        ),
    )


def _actions_world(
    monkeypatch: pytest.MonkeyPatch, row: SimpleNamespace, *, role: str, clusters_answer: bool
) -> dict[str, Any]:
    world: dict[str, Any] = {"order": [], "set": None, "scheduled": 0}

    async def get_job(_db: Any, _job_id: str, *, lock: bool = False) -> SimpleNamespace:
        world["order"].append("lock" if lock else "read")
        return row

    async def set_job(_db: Any, _job_id: str, **values: Any) -> bool:
        world["set"] = values
        return True

    async def list_resources(_db: Any, _job_id: str) -> list[Any]:
        return []

    async def job_plan(_row: Any) -> None:
        world["order"].append("plan")

    class Verifier:
        def __init__(self, _settings: Any) -> None:
            pass

        async def pin_user(self, _user: Any) -> SimpleNamespace:
            world["order"].append("authority")
            return _authority(role)

    async def clients(_row: Any, **_kwargs: Any) -> tuple[Any, Any]:
        world["order"].append("clients")
        if not clusters_answer:
            raise MigrationClusterUnavailable("cluster-b did not answer.")
        return object(), object()

    async def org_ids(_user: Any, _db: Any) -> list[int]:
        return [7]

    async def prove(_row: Any, _authority: Any) -> None:
        world["order"].append("prove")
        if world.get("ownership_lost"):
            raise actions.MigrationAuthorityError("Project ownership changed or became ambiguous.")

    monkeypatch.setattr(actions.repository, "get_job", get_job)
    monkeypatch.setattr(actions.repository, "set_job", set_job)
    monkeypatch.setattr(actions.repository, "list_resources", list_resources)
    monkeypatch.setattr(actions, "_job_plan", job_plan)
    monkeypatch.setattr(actions, "AuthentikAuthorityVerifier", Verifier)
    monkeypatch.setattr(actions, "_authoritative_clients", clients)
    monkeypatch.setattr(actions, "get_user_org_ids", org_ids)
    monkeypatch.setattr(actions, "_prove_job_authority", prove)
    monkeypatch.setattr(actions, "_schedule", lambda coro: (coro.close(), world.__setitem__("scheduled", world["scheduled"] + 1)))
    return world


@pytest.mark.asyncio
async def test_an_admin_can_release_a_stuck_job_whose_cluster_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _job(phase="manual_recovery", status="manual_recovery")
    world = _actions_world(monkeypatch, row, role="admin", clusters_answer=False)

    await _Svc().act(
        "job-1", action="release", expected_revision=4, reason="cluster decommissioned",
        confirmation="RELEASE", user=object(), requester_role="admin",
    )

    assert world["set"]["phase"] == "failed"
    assert world["set"]["terminal"] is True
    assert "clients" not in world["order"]


@pytest.mark.asyncio
async def test_recheck_and_cancel_do_not_need_the_clusters_to_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _job(phase="manual_recovery", status="manual_recovery")
    world = _actions_world(monkeypatch, row, role="partner", clusters_answer=False)
    await _Svc().act(
        "job-1", action="recheck", expected_revision=4, reason="cluster is back",
        user=object(), requester_role="partner",
    )
    assert world["set"]["status"] == "pending"
    assert world["scheduled"] == 1

    row = _job(phase="queued", status="pending")
    world = _actions_world(monkeypatch, row, role="partner", clusters_answer=False)
    await _Svc().act(
        "job-1", action="cancel", expected_revision=4, reason="not needed any more",
        user=object(), requester_role="partner",
    )
    assert world["set"]["phase"] == "cancelled"
    assert "clients" not in world["order"]


@pytest.mark.asyncio
async def test_a_queued_job_waiting_out_a_retry_delay_can_be_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A retry delay is written as a lease with no holder.
    row = _job(phase="queued", status="pending", locked_by=None,
               lease_expires_at=repository.utcnow() + timedelta(seconds=50))
    world = _actions_world(monkeypatch, row, role="partner", clusters_answer=True)

    await _Svc().act(
        "job-1", action="cancel", expected_revision=4, reason="not needed any more",
        user=object(), requester_role="partner",
    )

    assert world["set"]["phase"] == "cancelled"


@pytest.mark.asyncio
async def test_no_external_call_runs_while_the_job_row_is_locked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _job(phase="source_retained", status="waiting")
    world = _actions_world(monkeypatch, row, role="partner", clusters_answer=True)

    class _Lock:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_exc: Any) -> None:
            return None

    async def refence(*_args: Any) -> None:
        return None

    async def active_work(*_args: Any) -> bool:
        return False

    monkeypatch.setattr(actions, "serialize_cluster_configuration", lambda *_a: _Lock())
    monkeypatch.setattr(actions, "refence_target_for_rollback", refence)
    monkeypatch.setattr(actions, "_target_has_active_work", active_work)

    await _Svc().act(
        "job-1", action="rollback", expected_revision=4, reason="target misbehaves",
        confirmation="ROLLBACK", user=object(), requester_role="partner",
    )

    lock_at = world["order"].index("lock")
    assert {"authority", "clients", "plan"} <= set(world["order"][:lock_at])
    assert world["set"]["phase"] == "rolling_back"


@pytest.mark.asyncio
async def test_an_action_refuses_when_the_job_changed_between_checks_and_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _job(phase="source_retained", status="waiting")
    moved = _job(phase="cleaning_up", status="running", revision=5)
    world = _actions_world(monkeypatch, first, role="partner", clusters_answer=True)
    rows = iter([first, moved])

    async def get_job(_db: Any, _job_id: str, *, lock: bool = False) -> SimpleNamespace:
        world["order"].append("lock" if lock else "read")
        return next(rows)

    monkeypatch.setattr(actions.repository, "get_job", get_job)

    with pytest.raises(MigrationConflict):
        await _Svc().act(
            "job-1", action="cleanup", expected_revision=4, reason="done with it",
            confirmation="CLEANUP", user=object(), requester_role="partner",
        )

    assert world["set"] is None


# ----------------------------------------------------------------------------- transport errors


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(__import__("requests").exceptions.ConnectionError("reset by peer"), id="connection"),
        pytest.param(__import__("requests").exceptions.ReadTimeout("read timed out"), id="timeout"),
        pytest.param(__import__("app.core.blocking", fromlist=["BlockingPoolFull"]).BlockingPoolFull("migration.read", 4), id="pool-full"),
        pytest.param(__import__("app.core.blocking", fromlist=["BlockingTimeout"]).BlockingTimeout("migration.read", 90), id="blocking-timeout"),
    ],
)
@pytest.mark.asyncio
async def test_a_transport_error_while_reconciling_backs_off_instead_of_manual_recovery(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    captured = _drive(monkeypatch, _job(phase="reconciling"), error)

    await worker.run_migration_job("job-1", allow_mutation=True)

    assert captured["status"] == "unknown"
    assert captured["phase"] != "manual_recovery"


@pytest.mark.asyncio
async def test_a_transport_error_before_a_queued_job_changed_anything_retries_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import requests

    captured = _drive(monkeypatch, _job(), requests.exceptions.ConnectionError("reset by peer"))

    await worker.run_migration_job("job-1", allow_mutation=True)

    assert captured["status"] == "pending"
    assert captured["error_code"] == "cluster_unavailable"


@pytest.mark.asyncio
async def test_an_unexpected_error_still_stops_for_manual_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _drive(monkeypatch, _job(phase="final_sync"), ValueError("unexpected"))

    await worker.run_migration_job("job-1", allow_mutation=True)

    assert captured["status"] == "manual_recovery"


# ----------------------------------------------------------------------------- after a mutation


def _journal(monkeypatch: pytest.MonkeyPatch, *, touched: bool, persisted: dict[str, Any]) -> None:
    resources = [SimpleNamespace(operation_refs=[{"operation_id": "op"}] if touched else [],
                                 state="syncing" if touched else "pending")]

    async def listed(_job_id: str) -> list[Any]:
        return resources

    async def get_job(_db: Any, _job_id: str, **_kwargs: Any) -> Any:
        return SimpleNamespace(safe_result=persisted)

    monkeypatch.setattr(worker, "_resources", listed)
    monkeypatch.setattr(worker.repository, "get_job", get_job)


@pytest.mark.asyncio
async def test_a_transport_error_after_the_transfer_started_is_never_a_queued_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.blocking import BlockingTimeout

    # Claimed in queued, so the in-memory phase still says queued after the run moved on.
    job = _job(safe_result={"cluster_unavailable_retries": worker._PRE_MUTATION_RETRY_LIMIT})
    captured = _drive(monkeypatch, job, BlockingTimeout("migration.verify_target", 90))
    _journal(monkeypatch, touched=True, persisted={})

    await worker.run_migration_job("job-1", allow_mutation=True)

    assert captured["status"] == "unknown"
    assert not captured.get("terminal")


@pytest.mark.asyncio
async def test_a_queued_retry_keeps_what_the_run_already_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _drive(monkeypatch, _job(), MigrationClusterUnavailable("cluster-b did not answer."))
    _journal(monkeypatch, touched=False, persisted={"billing_override_audited": True})

    await worker.run_migration_job("job-1", allow_mutation=True)

    assert captured["status"] == "pending"
    assert captured["safe_result"]["billing_override_audited"] is True
    assert captured["safe_result"]["cluster_unavailable_retries"] == 1


@pytest.mark.asyncio
async def test_cancel_and_recheck_still_prove_current_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    for action, phase, status in (("cancel", "queued", "pending"), ("recheck", "manual_recovery", "manual_recovery")):
        row = _job(phase=phase, status=status)
        world = _actions_world(monkeypatch, row, role="partner", clusters_answer=False)
        world["ownership_lost"] = True
        with pytest.raises(actions.MigrationAuthorityError):
            await _Svc().act("job-1", action=action, expected_revision=4, reason="ownership moved away",
                             user=object(), requester_role="partner")
        assert world["set"] is None, action


@pytest.mark.asyncio
async def test_release_needs_no_ownership_so_a_deregistered_cluster_stays_releasable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _job(phase="manual_recovery", status="manual_recovery")
    world = _actions_world(monkeypatch, row, role="admin", clusters_answer=False)
    world["ownership_lost"] = True

    await _Svc().act("job-1", action="release", expected_revision=4, reason="cluster deregistered",
                     confirmation="RELEASE", user=object(), requester_role="admin")

    assert world["set"]["phase"] == "failed"
    assert "prove" not in world["order"]
