"""Durable instance jobs never guess one organization on a shared cluster."""

import pytest

from app.services.instance_job_identity import resolve_instance_job_organization
from app.services.task_journal.sink import JournalSink
from app.services.task_journal.sink_mapping import record_to_log_row, record_to_task


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _DB:
    def __init__(self, *results):
        self.results = iter(results)
        self.calls = 0

    async def execute(self, _statement):
        self.calls += 1
        return _Rows(next(self.results))


@pytest.mark.asyncio
async def test_an_exclusive_cluster_has_one_exact_target_organization():
    db = _DB([(7, "org-seven")])

    resolved = await resolve_instance_job_organization(
        db,
        cluster_id="cluster-a",
        project="default",
    )

    assert (
        resolved.organization_id,
        resolved.organization_slug,
        resolved.resolution,
    ) == (7, "org-seven", "exact")
    assert db.calls == 1


@pytest.mark.asyncio
async def test_a_shared_default_project_is_explicitly_ambiguous():
    db = _DB([(7, "org-seven"), (8, "org-eight")])

    resolved = await resolve_instance_job_organization(
        db,
        cluster_id="cluster-a",
        project="default",
    )

    assert (resolved.organization_id, resolved.organization_slug, resolved.resolution) == (
        None,
        None,
        "ambiguous",
    )
    assert db.calls == 1


@pytest.mark.asyncio
async def test_one_concrete_shared_project_grant_proves_the_target_organization():
    db = _DB(
        [(7, "org-seven"), (8, "org-eight")],
        [(8, "org-eight")],
    )

    resolved = await resolve_instance_job_organization(
        db,
        cluster_id="cluster-a",
        project="project-eight",
    )

    assert (resolved.organization_id, resolved.organization_slug, resolved.resolution) == (
        8,
        "org-eight",
        "exact",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "project_owners",
    [[], [(7, "org-seven"), (8, "org-eight")]],
)
async def test_missing_or_conflicting_project_ownership_remains_ambiguous(project_owners):
    db = _DB(
        [(7, "org-seven"), (8, "org-eight")],
        project_owners,
    )

    resolved = await resolve_instance_job_organization(
        db,
        cluster_id="cluster-a",
        project="project-x",
    )

    assert resolved.resolution == "ambiguous"
    assert resolved.organization_id is None


@pytest.mark.asyncio
async def test_an_unbound_cluster_is_not_backfilled_as_an_organization():
    db = _DB([(None, None)])

    resolved = await resolve_instance_job_organization(
        db,
        cluster_id="cluster-a",
        project="default",
    )

    assert (resolved.organization_id, resolved.organization_slug, resolved.resolution) == (
        None,
        None,
        "unbound",
    )


@pytest.mark.asyncio
async def test_an_explicit_ambiguous_audit_target_disables_legacy_cluster_backfill():
    record = {
        "@timestamp": "2026-09-21T00:00:00+00:00",
        "event_id": "event-1",
        "action": "instance.create",
        "result": "accepted",
        "actor": {"user_id": "operator@example.test", "role": "partner"},
        "target": {
            "type": "instance",
            "id": "vm-1",
            "host": "shared-a",
            "project": "default",
            "organization_id": None,
            "organization_slug": None,
            "organization_resolution": "ambiguous",
        },
        "details": {"job_id": "job-1"},
    }
    row = record_to_log_row(record)
    task = record_to_task(record)
    assert row is not None
    assert task is not None
    assert task.actor.target_organization_resolution == "ambiguous"

    sink = JournalSink()

    async def must_not_guess(*_args):
        raise AssertionError("an explicit ambiguous target must not use cluster-first backfill")

    sink._org_for = must_not_guess  # type: ignore[method-assign]
    await sink._backfill_org(object(), row)

    assert "_target_organization_resolution" not in row
    assert row["target_organization_id"] is None
