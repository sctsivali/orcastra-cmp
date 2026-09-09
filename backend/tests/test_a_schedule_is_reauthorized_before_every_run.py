"""A schedule is a standing instruction that outlives the session that made it by months.

Authorizing it once at creation would be the same mistake the webhook dispatcher records having
avoided: "an organization can lose a cluster between the moment an event was enqueued and the
moment it goes out. Without this, revocation is not real." A schedule's window is not seconds, it
is a quarter.

Two of these refusals exist because a name is not an identity. `cluster_id` is free text with no
foreign key anywhere in this schema, so a cluster deleted and re-registered by a different
organization matches the old string exactly. LXD instance names are unique per project rather than
stable, so an instance deleted and recreated under the same name is a different instance, possibly
a different tenant's, on a shared cluster.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from app.services.snapshots import schedule_authority as authority
from app.services.snapshots.schedule_runner import run_one
from tests._schedule_fakes import FakeDB, FakeSchedule, ops_rows
from tests._snapshot_fakes import FakeClient, FakeInstanceNode, service

UTC = timezone.utc
SLOT = datetime(2026, 9, 9, 3, 0, tzinfo=UTC)


def _run(schedule, node=None, db=None, **kwargs):
    node = node or FakeInstanceNode(snapshots=ops_rows(schedule.id, 1))
    svc = service(FakeClient(node))
    outcome = asyncio.run(
        run_one(db or FakeDB(), schedule, now=SLOT, lxd_service=svc, **kwargs)
    )
    return outcome, node


def test_the_gate_is_a_gate_and_not_a_wall():
    """The control. Every refusal below is only meaningful because this one passes."""
    outcome, node = _run(FakeSchedule(due_slot=SLOT))
    assert outcome.result == "success", outcome.error
    assert any(entry[0] == "create_snapshot" for entry in node.recorded)


def test_a_cluster_that_left_the_organization_stops_the_schedule():
    outcome, node = _run(FakeSchedule(due_slot=SLOT), db=FakeDB(bound=False))
    assert outcome.result == "refused"
    assert outcome.refusal.code == "cluster_left_the_organization"
    assert outcome.terminal is True
    assert node.recorded == [], "the cluster was contacted after the grant was gone"


def test_a_schedule_whose_organization_is_gone_stops():
    """`organization_id` is SET NULL rather than CASCADE for exactly this: the row survives so the
    refusal can be seen, instead of the evidence being erased."""
    outcome, node = _run(FakeSchedule(due_slot=SLOT, organization_id=None))
    assert outcome.result == "refused"
    assert outcome.refusal.code == "schedule_has_no_organization"
    assert node.recorded == []


def test_a_re_registered_cluster_does_not_inherit_the_schedule():
    async def _different(_cluster_id):
        return "sha256:bbbb"

    outcome, node = _run(
        FakeSchedule(due_slot=SLOT, cluster_fingerprint="sha256:aaaa"),
        fingerprint_reader=_different,
    )
    assert outcome.result == "refused"
    assert outcome.refusal.code == "cluster_is_a_different_registration"
    assert node.recorded == []


def test_a_vault_outage_does_not_pause_the_fleet():
    """`cluster_fingerprint` returns None for BOTH 'nothing pinned' and 'Vault unreadable', and
    swallows every exception to do it. Reading None as a mismatch would stop every schedule on the
    fleet the moment Vault hiccups."""
    async def _unreadable(_cluster_id):
        return None

    outcome, _node = _run(
        FakeSchedule(due_slot=SLOT, cluster_fingerprint="sha256:aaaa"),
        fingerprint_reader=_unreadable,
    )
    assert outcome.result == "success", outcome.error


def test_an_instance_recreated_under_the_same_name_is_not_the_same_instance():
    node = FakeInstanceNode(
        config={"volatile.uuid": "uuid-B"}, snapshots=ops_rows(12, 1)
    )
    outcome, node = _run(FakeSchedule(due_slot=SLOT, instance_uuid="uuid-A"), node=node)
    assert outcome.result == "refused"
    assert outcome.refusal.code == "instance_is_a_different_instance"
    assert outcome.terminal is True
    assert not any(e[0] == "create_snapshot" for e in node.recorded)


def test_a_matching_uuid_is_allowed_through():
    node = FakeInstanceNode(config={"volatile.uuid": "uuid-A"}, snapshots=ops_rows(12, 1))
    outcome, _node = _run(FakeSchedule(due_slot=SLOT, instance_uuid="uuid-A"), node=node)
    assert outcome.result == "success", outcome.error


def test_a_paused_schedule_never_reaches_a_cluster():
    outcome, node = _run(FakeSchedule(due_slot=SLOT, state="paused"))
    assert outcome.result == "refused"
    assert outcome.refusal.code == "schedule_not_active"
    assert node.recorded == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("instance_name", "ct-victim?"),
        ("project", "cust a"),
        ("cluster_id", "../etc"),
        ("instance_name", ""),
    ],
)
def test_a_stored_name_is_revalidated_at_the_moment_it_becomes_a_path(field, value):
    """Validation at creation is not validation at use.

    These three strings are stored once and interpolated into a request path months later, by a
    code path the validating router is not part of. pylxd escapes nothing, so a segment carrying
    `?` ends the path and moves the request to a different resource.
    """
    outcome, node = _run(FakeSchedule(due_slot=SLOT, **{field: value}))
    assert outcome.result == "refused"
    assert outcome.refusal.code == "schedule_target_is_not_addressable"
    assert node.recorded == [], "a malformed name reached the daemon"


def test_a_missing_instance_is_given_a_grace_before_it_is_terminal():
    """An instance can be absent from one listing and present in the next, mid-migration or
    mid-restart. Three ticks of a schedule is hours, which is long enough that absent means gone."""
    refusal = authority.Refusal(code="instance_not_found", message="gone", terminal=False)
    assert authority.refusal_is_terminal(refusal, 0) is False
    assert authority.refusal_is_terminal(refusal, 1) is False
    assert authority.refusal_is_terminal(refusal, 2) is True
