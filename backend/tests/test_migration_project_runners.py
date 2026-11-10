"""Project relocation runners, planned by the real planner and run against an in-memory LXD.

Each test plans with ``preflight.inspect``, builds the resource journal from that plan, and drives
the real runners. The fake keeps daemon state, so a test asserts what exists on each side after
the run, not which calls were made.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.migrations import project_cleanup, project_member_relocation, project_relocation, project_rollback
from app.services.migrations.project_policy import MIGRATION_JOB_MARKER, created_by_job
from tests._migration_fake_daemon import FakeCluster, install, plan, wire

PROJECT = {
    "features.images": "true",
    "features.profiles": "true",
    "features.storage.volumes": "true",
    "features.storage.buckets": "true",
    "limits.instances": "10",
}


def _source() -> FakeCluster:
    cluster = FakeCluster("cluster-a", networks=["lxdbr0"])
    cluster.add_project("web", PROJECT)
    cluster.profiles[("web", "default")] = {
        "name": "default",
        "description": "web default",
        "config": {},
        "devices": {
            "root": {"type": "disk", "path": "/", "pool": "default"},
            "eth0": {"type": "nic", "network": "lxdbr0", "name": "eth0"},
        },
    }
    cluster.profiles[("web", "small")] = {"name": "small", "description": "", "config": {"limits.cpu": "1"}, "devices": {}}
    cluster.add_instance("web", "db-1", status="Stopped", profiles=("default", "small"), config={"volatile.base_image": "base1"})
    cluster.add_instance("web", "web-1", status="Running", config={"boot.autostart": "true", "boot.autostart.priority": "5"})
    # A cached image, and the image db-1 was created from, which LXD keeps without the cached flag.
    cluster.images["web"] = [{"fingerprint": "cached1", "cached": True}, {"fingerprint": "base1", "cached": False, "aliases": []}]
    return cluster


def _member_cluster() -> FakeCluster:
    cluster = FakeCluster("cluster-a", members=("m1", "m2"))
    cluster.add_project("web", PROJECT)
    cluster.profiles[("web", "default")]["devices"] = {"root": {"type": "disk", "path": "/", "pool": "default"}}
    return cluster


def _target() -> FakeCluster:
    return FakeCluster("cluster-b", networks=["lxdbr0", "lxdbr1"])


async def _relocate(monkeypatch: pytest.MonkeyPatch, source: FakeCluster, target: FakeCluster, **kwargs: Any) -> tuple[Any, list[Any], dict[str, list[Any]]]:
    install(monkeypatch)
    job, resources, request = plan(source, target, source_project="web", target_project="web", **kwargs)
    calls = wire(monkeypatch, job, resources, request)
    await project_relocation.advance(job, "worker", source.client, target.client)
    return job, resources, calls


async def test_a_project_relocates_and_retains_its_stopped_source(monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = _source(), _target()

    job, resources, calls = await _relocate(monkeypatch, source, target, mappings={"networks": {"lxdbr0": "lxdbr1"}})

    assert job.phase == "source_retained"
    project = target.projects["web"]
    assert created_by_job(project, job.job_id)
    assert project["config"]["limits.instances"] == "10"
    assert target.profiles[("web", "default")]["devices"]["eth0"]["network"] == "lxdbr1"
    assert target.profiles[("web", "small")]["config"] == {"limits.cpu": "1"}
    web = target.instances[("web", "web-1")]
    assert web["status"] == "Running" and target.instances[("web", "db-1")]["status"] == "Stopped"
    assert web["config"][MIGRATION_JOB_MARKER] == job.job_id
    # The retained sources are stopped, and the autostarting one is fenced from host boots.
    assert {row["status"] for row in source.instances.values()} == {"Stopped"}
    assert source.instances[("web", "web-1")]["config"]["boot.autostart"] == "false"
    retained = calls["retained"][0]
    assert [(src.name, dst.cluster_id) for src, dst in retained["pairs"]] == [("db-1", "cluster-b"), ("web-1", "cluster-b")]
    names = {event["extra"]["resource_name"] for event in retained["outbox_events"] if event["channel"] == "billing"}
    assert names == {"db-1", "web-1"}
    assert {resource.state for resource in resources} == {"retained"}


async def test_the_planner_and_the_worker_derive_the_same_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clone the worker writes reads back as the approved profile, so no drift is reported."""
    source, target = _source(), _target()

    job, _resources, calls = await _relocate(monkeypatch, source, target)

    assert job.phase == "source_retained", calls["updates"][-1]
    assert not any(update.get("phase") == "rolling_back" for update in calls["updates"])


async def test_a_same_cluster_rename_copies_into_the_new_project(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()
    mac = source.instances[("web", "web-1")]["config"]["volatile.eth0.hwaddr"]
    install(monkeypatch)
    job, resources, request = plan(source, source, source_project="web", target_project="web-v2")
    wire(monkeypatch, job, resources, request)

    await project_relocation.advance(job, "worker", source.client, source.client)

    assert job.phase == "source_retained"
    assert {key for key in source.instances if key[0] == "web-v2"} == {("web-v2", "db-1"), ("web-v2", "web-1")}
    assert source.instances[("web-v2", "web-1")]["status"] == "Running"
    # The retained sources keep their name and MAC beside the targets, so their NICs are masked.
    assert source.instances[("web", "web-1")]["devices"]["eth0"] == {"type": "none"}
    assert "volatile.eth0.hwaddr" not in source.instances[("web", "web-1")]["config"]

    job.phase, job.status = "rolling_back", "running"
    await project_rollback.run_project_rollback(job, "worker", source.client, source.client)

    # Rollback gives the NIC back (it came from the profile, so the mask is removed), with the
    # MAC it had, and starts it.
    assert "eth0" not in source.instances[("web", "web-1")]["devices"]
    assert source.instances[("web", "web-1")]["config"]["volatile.eth0.hwaddr"] == mac
    assert source.instances[("web", "web-1")]["status"] == "Running"
    assert "web-v2" not in source.projects


async def test_a_refused_transfer_aborts_and_undoes_only_what_the_job_created(monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = _source(), _target()
    target.refuse.add(("pull", "web-1"))

    job, _resources, calls = await _relocate(monkeypatch, source, target)

    assert calls["finished"][0]["phase"] == "failed"
    assert "web" not in target.projects
    assert not target.instances and not any(key[0] == "web" for key in target.profiles)
    # The running source was stopped for the transfer and started again by the abort.
    assert source.instances[("web", "web-1")]["status"] == "Running"
    assert source.instances[("web", "db-1")]["status"] == "Stopped"


async def test_a_target_start_the_daemon_refuses_aborts_instead_of_retrying(monkeypatch: pytest.MonkeyPatch) -> None:
    """LXD accepts the start, then its start validation fails (ex; a MAC already on that member).

    The target never ran, so this is an abort. Treated as unknown, reconciliation would start it
    again on every backoff while the fenced source stays down.
    """
    source, target = _source(), _target()
    target.fail_async.add(("start", "web-1"))

    job, _resources, calls = await _relocate(monkeypatch, source, target)

    assert calls["finished"][0]["phase"] == "failed"
    assert calls["finished"][0]["error_code"] == "target_start_refused"
    assert "web" not in target.projects
    assert not target.instances
    assert source.instances[("web", "web-1")]["status"] == "Running"
    assert source.instances[("web", "db-1")]["status"] == "Stopped"


async def test_a_crash_after_submission_adopts_the_target_instead_of_copying_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = _source(), _target()
    install(monkeypatch)
    job, resources, request = plan(source, target, source_project="web", target_project="web")
    wire(monkeypatch, job, resources, request)
    target.refuse.add(("pull", "web-1"))
    # First run: db-1 transfers, then web-1 is refused. Simulate instead a lost response by
    # letting the pull happen but losing everything after the submission intent.
    target.refuse.clear()
    original = project_relocation._transfer_staged

    async def crash_after_submit(job_: Any, resource: Any, state: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        if resource.source_ref["name"] == "web-1":
            await original(job_, resource, state, *args, **kwargs)
            resource.safe_result = {**resource.safe_result, "target_identity_hash": None}
            raise ConnectionError("worker died")
        return await original(job_, resource, state, *args, **kwargs)

    monkeypatch.setattr(project_relocation, "_transfer_staged", crash_after_submit)
    with pytest.raises(ConnectionError):
        await project_relocation.advance(job, "worker", source.client, target.client)
    pulls = [call for call in target.calls if call[0] == "pull"]

    monkeypatch.setattr(project_relocation, "_transfer_staged", original)
    await project_relocation.advance(job, "worker", source.client, target.client)

    assert job.phase == "source_retained"
    assert [call for call in target.calls if call[0] == "pull"] == pulls
    assert target.instances[("web", "web-1")]["status"] == "Running"


async def test_rollback_removes_the_target_project_and_restores_the_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = _source(), _target()
    job, _resources, calls = await _relocate(monkeypatch, source, target)
    job.phase, job.status = "rolling_back", "running"

    await project_rollback.run_project_rollback(job, "worker", source.client, target.client)

    finish = calls["finished"][-1]
    assert finish["phase"] == "rolled_back" and finish["revert_metadata"] is True
    assert finish["target_project_deleted"] is True
    assert "web" not in target.projects and not target.instances
    assert source.instances[("web", "web-1")]["status"] == "Running"
    assert source.instances[("web", "web-1")]["config"]["boot.autostart"] == "true"
    assert source.instances[("web", "db-1")]["status"] == "Stopped"


async def test_rollback_refuses_a_target_project_that_gained_an_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = _source(), _target()
    job, _resources, calls = await _relocate(monkeypatch, source, target)
    target.add_instance("web", "made-later")
    job.phase, job.status = "rolling_back", "running"

    await project_rollback.run_project_rollback(job, "worker", source.client, target.client)

    assert calls["refused"][0]["error_code"] == "rollback_target_changed"
    assert "made-later" in calls["refused"][0]["message"]
    assert ("web", "web-1") in target.instances and not calls["finished"]


async def test_finalize_deletes_the_source_project_and_its_cached_images(monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = _source(), _target()
    job, _resources, calls = await _relocate(monkeypatch, source, target)
    job.phase, job.status = "cleaning_up", "running"

    await project_cleanup.run_project_cleanup(job, "worker", source.client, target.client)

    assert calls["cleaned"], calls["refused"]
    assert "web" not in source.projects
    assert not any(key[0] == "web" for key in source.instances) and not source.images.get("web")
    assert ("default", "default") in source.profiles
    assert ("web", "web-1") in target.instances


@pytest.mark.parametrize(
    ("change", "codes"),
    [
        # db-1 inherits this profile, so its definition drifts as well.
        ("profile", {"source_definition_drift", "source_profile_drift"}),
        ("instance", {"source_project_changed"}),
        ("snapshot", {"source_snapshot_drift"}),
    ],
)
async def test_finalize_refuses_a_source_project_that_changed(
    monkeypatch: pytest.MonkeyPatch, change: str, codes: set[str]
) -> None:
    source, target = _source(), _target()
    job, _resources, calls = await _relocate(monkeypatch, source, target)
    if change == "profile":
        source.profiles[("web", "small")]["config"] = {"limits.cpu": "4"}
    elif change == "instance":
        source.add_instance("web", "made-later")
    else:
        source.instances[("web", "db-1")]["snapshots"].append("snap-during-retention")
    job.phase, job.status = "cleaning_up", "running"

    await project_cleanup.run_project_cleanup(job, "worker", source.client, target.client)

    assert calls["refused"][0]["error_code"] in codes
    assert ("web", "db-1") in source.instances and "web" in source.projects
    assert not calls["cleaned"]


async def test_workloads_move_to_a_member_one_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = _member_cluster()
    cluster.add_instance("web", "db-1", status="Stopped")
    cluster.add_instance("web", "web-1", status="Running")
    install(monkeypatch)
    job, resources, request = plan(cluster, cluster, source_project="web", target_project="web", member="m2")
    calls = wire(monkeypatch, job, resources, request)

    await project_member_relocation.relocate_members(job, "worker", cluster.client)

    assert job.phase == "completed"
    assert {row["location"] for row in cluster.instances.values()} == {"m2"}
    assert cluster.instances[("web", "web-1")]["status"] == "Running"
    # Rolling: web-1 is stopped, moved, then started.
    web_calls = [call for call in cluster.calls if call[1] == "web-1"]
    assert web_calls == [("stop", "web-1"), ("relocate", "web-1"), ("start", "web-1")]
    assert calls["updates"][-1]["status"] == "success"


async def test_a_failed_member_move_puts_every_workload_back(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = _member_cluster()
    cluster.add_instance("web", "db-1", status="Running")
    cluster.add_instance("web", "web-1", status="Running")
    cluster.refuse.add(("relocate", "web-1"))
    install(monkeypatch)
    job, resources, request = plan(cluster, cluster, source_project="web", target_project="web", member="m2")
    calls = wire(monkeypatch, job, resources, request)

    await project_member_relocation.relocate_members(job, "worker", cluster.client)

    assert job.phase == "failed"
    assert {row["location"] for row in cluster.instances.values()} == {"m1"}
    assert {row["status"] for row in cluster.instances.values()} == {"Running"}
    assert calls["updates"][-1]["error_code"]


async def test_a_resume_after_the_autostart_fence_still_matches_the_approved_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retention transaction fails after activation fenced autostart; the resumed job must
    not read the fenced source as a changed definition."""
    source, target = _source(), _target()
    install(monkeypatch)
    job, resources, request = plan(source, target, source_project="web", target_project="web")
    calls = wire(monkeypatch, job, resources, request)
    real = project_relocation.enter_project_retention
    failures = iter([RuntimeError("outbox collision")])

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        error = next(failures, None)
        if error is not None:
            raise error
        return await real(*args, **kwargs)

    monkeypatch.setattr(project_relocation, "enter_project_retention", flaky)
    with pytest.raises(RuntimeError):
        await project_relocation.advance(job, "worker", source.client, target.client)
    assert source.instances[("web", "web-1")]["config"]["boot.autostart"] == "false"
    job.phase = "reconciling"

    await project_relocation.advance(job, "worker", source.client, target.client)

    assert job.phase == "source_retained", calls["updates"][-1]
    assert [call for call in target.calls if call[0] == "start"] == [("start", "web-1")]


@pytest.mark.parametrize("runner", ["rollback", "finalize"])
async def test_a_teardown_is_refused_while_another_job_holds_the_project(monkeypatch: pytest.MonkeyPatch, runner: str) -> None:
    """An instance moved out of the new project during retention is another job's retained
    source, and the relocation's rollback must not delete it."""
    source, target = _source(), _target()
    job, _resources, calls = await _relocate(monkeypatch, source, target)
    calls["fence_conflict"].append("Another migration (abcd1234) holds a lock in project 'web'. Finish it first.")
    job.phase, job.status = ("rolling_back" if runner == "rollback" else "cleaning_up"), "running"

    if runner == "rollback":
        await project_rollback.run_project_rollback(job, "worker", source.client, target.client)
    else:
        await project_cleanup.run_project_cleanup(job, "worker", source.client, target.client)

    assert "abcd1234" in calls["refused"][0]["message"]
    assert ("web", "web-1") in target.instances and "web" in source.projects
    assert not calls["finished"] and not calls["cleaned"]


async def test_a_project_on_the_default_profiles_finalizes_without_touching_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without its own profiles a project lists, and would write, the default project's
    profiles. Neither the drift check nor the delete may count or touch them."""
    source, target = FakeCluster("cluster-a"), _target()
    source.add_project("web", {"features.images": "false", "features.profiles": "false", "limits.instances": "10"})
    source.profiles[("default", "shared")] = {"name": "shared", "description": "", "config": {}, "devices": {}}
    source.add_instance("web", "web-1", status="Running")
    install(monkeypatch)
    job, resources, request = plan(source, target, source_project="web", target_project="web")
    calls = wire(monkeypatch, job, resources, request)
    assert not [resource for resource in resources if resource.resource_type == "profile"]
    await project_relocation.advance(job, "worker", source.client, target.client)
    assert job.phase == "source_retained", (job.safe_result, calls["finished"])
    job.phase, job.status = "cleaning_up", "running"

    await project_cleanup.run_project_cleanup(job, "worker", source.client, target.client)

    assert calls["cleaned"], calls["refused"]
    assert ("default", "default") in source.profiles and ("default", "shared") in source.profiles
    assert "web" not in source.projects


async def test_a_resumed_finalize_still_knows_the_base_images(monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = _source(), _target()
    job, resources, calls = await _relocate(monkeypatch, source, target)
    job.phase, job.status = "cleaning_up", "running"
    real = project_cleanup.delete_job_project

    async def crash(*_args: Any, **_kwargs: Any) -> None:
        raise ConnectionError("worker died after the instance deletes")

    monkeypatch.setattr(project_cleanup, "delete_job_project", crash)
    with pytest.raises(ConnectionError):
        await project_cleanup.run_project_cleanup(job, "worker", source.client, target.client)
    assert not any(key[0] == "web" for key in source.instances)
    monkeypatch.setattr(project_cleanup, "delete_job_project", real)

    await project_cleanup.run_project_cleanup(job, "worker", source.client, target.client)

    assert calls["cleaned"], calls["refused"]
    assert "web" not in source.projects and not source.images.get("web")


async def test_finalize_that_finds_drift_after_a_delete_stops_in_manual_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """After this run deleted a source, going back to retention would offer a rollback that
    cannot restore it."""
    source, target = _source(), _target()
    job, _resources, calls = await _relocate(monkeypatch, source, target)
    job.phase, job.status = "cleaning_up", "running"
    real_delete = project_cleanup._delete

    async def delete_then_replace(*args: Any, **kwargs: Any) -> Any:
        state = await real_delete(*args, **kwargs)
        source.instances[("web", "web-1")]["config"]["volatile.uuid"] = "replaced-uuid"
        return state

    monkeypatch.setattr(project_cleanup, "_delete", delete_then_replace)

    await project_cleanup.run_project_cleanup(job, "worker", source.client, target.client)

    assert calls["updates"][-1]["phase"] == "manual_recovery"
    assert calls["updates"][-1]["error_code"] == "source_identity_drift"
    assert not calls["refused"] and not calls["cleaned"]
