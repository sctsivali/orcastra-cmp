"""An add-on hears about a project only when its installation reaches that project.

Webhooks were routed by cluster alone, so an add-on a Partner installed on a cluster another
organization also holds received that party's project, instance and volume names (security review
of the RC5 follow-up round, 2026-11-06). The event's project must now be in the installation's
grant for that cluster (owner decision 2026-11-06: filtered per grant on every cluster), the grant
held to its installer's bound where another party holds the cluster, as its key is
(services/key_party_bound.py), when the event is queued. A queued event is judged again in
test_a_queued_addon_event_is_judged_where_it_is_sent_or_shown.py.
"""

import asyncio
from typing import Any, Dict, List

import pytest

from app.services import key_party_bound as kpb
from app.services.webhooks import reach
from app.services.webhooks import emitter
from tests._addon_reach_fakes import BOUND, Install, hold_installer
from tests.test_a_lifecycle_change_reaches_the_addons_watching_it import FakeSubscription, _run


@pytest.fixture
def installer(monkeypatch):
    return hold_installer(monkeypatch)


def _interested(
    install, project, monkeypatch, *, resource_type="instance", resource_name="vm-a",
    event_type="orcastra.instance.created.v1", reached=(),
) -> List[int]:
    sub = FakeSubscription(id=10, installation_id=install.id, event_types=[event_type])
    enqueued: Dict[str, Any] = {}

    async def fake_enqueue(_db, event, *, subscriptions):
        enqueued["ids"] = [s.id for s in subscriptions]
        return list(subscriptions)

    monkeypatch.setattr(emitter, "enqueue_event", fake_enqueue)
    import app.core.database as database

    class _Maker:
        def __call__(self):
            return self

        async def __aenter__(self):
            from tests.test_a_lifecycle_change_reaches_the_addons_watching_it import FakeDB

            return FakeDB([(sub, install)])

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(database, "async_session_maker", _Maker())
    asyncio.run(emitter.emit_resource_event(
        event_type=event_type, cluster_id="nuc2",
        resource_type=resource_type, resource_name=resource_name, project=project,
        reached_organizations=reached,
    ))
    return enqueued.get("ids", [])


def test_an_event_in_the_other_partys_project_is_not_sent(installer, monkeypatch) -> None:
    assert _interested(Install(id=1), "theirs", monkeypatch) == []


def test_an_event_in_the_installers_own_project_is_sent(installer, monkeypatch) -> None:
    assert _interested(Install(id=1), "own", monkeypatch) == [10]


def test_an_admin_installers_addon_and_an_unshared_cluster_hear_everything(installer, monkeypatch) -> None:
    installer["admin"] = True
    assert _interested(Install(id=1), "theirs", monkeypatch) == [10]
    installer["admin"] = False
    installer["bound"] = None
    assert _interested(Install(id=1), "theirs", monkeypatch) == [10]


def test_on_a_shared_cluster_a_named_grant_hears_its_projects_inside_the_bound(installer, monkeypatch) -> None:
    named = Install(id=1, granted_scope=[{"cluster_id": "nuc2", "projects": ["own", "theirs"]}])
    assert _interested(named, "theirs", monkeypatch) == [], "a project another party holds is dropped"
    assert _interested(named, "own", monkeypatch) == [10]


def test_on_an_unshared_cluster_a_named_grant_hears_only_its_projects(installer, monkeypatch) -> None:
    installer["bound"] = None
    named = Install(id=1, granted_scope=[{"cluster_id": "nuc2", "projects": ["default"]}])
    assert _interested(named, "elsewhere", monkeypatch) == [], "a project outside the grant is not heard"
    assert _interested(named, "default", monkeypatch) == [10]
    no_list = Install(id=1, granted_scope=[{"cluster_id": "nuc2"}])
    assert _interested(no_list, "elsewhere", monkeypatch) == [], "a grant naming no project reaches default"


def test_a_project_event_is_judged_by_the_project_it_names(installer, monkeypatch) -> None:
    assert _interested(Install(id=1), "own", monkeypatch, resource_type="project", resource_name="theirs") == []
    assert _interested(Install(id=1), "theirs", monkeypatch, resource_type="project", resource_name="own") == [10]


def test_an_unreadable_bound_sends_nothing(monkeypatch) -> None:
    async def broken(_api_key, _cluster_id, _db):
        raise RuntimeError("database gone")

    monkeypatch.setattr(kpb, "_bound", broken)
    assert _interested(Install(id=1), "own", monkeypatch) == []


def test_the_existing_cluster_routing_still_holds(monkeypatch) -> None:
    # The conftest seam answers "no bound": a grant with no project list reaches default only.
    from tests.test_a_lifecycle_change_reaches_the_addons_watching_it import FakeInstall

    install = FakeInstall(id=1, granted_scope=[{"cluster_id": "nuc2"}])
    sub = FakeSubscription(id=10, installation_id=1, event_types=["orcastra.instance.created.v1"])
    count, _ = _run([(sub, install)], monkeypatch=monkeypatch)
    assert count == 1


def test_a_project_delete_reaches_the_organizations_that_held_it(installer, monkeypatch) -> None:
    # "gone" is in no bound any more: the delete revoked the policies that put it there.
    deleted = dict(event_type="orcastra.project.deleted.v1", resource_type="project", resource_name="gone")
    assert _interested(Install(id=1), None, monkeypatch, reached={7}, **deleted) == [10]
    assert _interested(Install(id=1), None, monkeypatch, reached={8}, **deleted) == [], (
        "another organization held it, so this add-on never reached it"
    )
    named = Install(id=1, granted_scope=[{"cluster_id": "nuc2", "projects": ["own"]}])
    assert _interested(named, None, monkeypatch, reached={7}, **deleted) == [], (
        "its organization held the project, but the add-on's grant never named it"
    )


def test_a_slow_reach_is_not_reached(installer, monkeypatch) -> None:
    async def slow(_api_key, _cluster_id, _db):
        await asyncio.sleep(1)
        return "partner@example.test", BOUND

    monkeypatch.setattr(kpb, "_bound", slow)
    monkeypatch.setattr(reach, "REACH_SECONDS", 0.05)
    assert _interested(Install(id=1), "own", monkeypatch) == []


async def test_a_revoke_names_the_organizations_whose_policies_held_the_project() -> None:
    from app.services import project_grants
    from tests._project_grant_fakes import GrantSession, Policy, Rule

    held, other = Policy(7, "Partner A", id=501), Policy(8, "Partner B", id=502)
    session = GrantSession(policies=[held, other], rules=[Rule(501, "nuc2", "gone"), Rule(502, "nuc2", "kept")])
    held_by: set = set()
    await project_grants.revoke_project_grants(session, cluster_id="nuc2", project_name="gone", held_by=held_by)
    assert held_by == {7}


def test_a_party_that_reuses_a_deleted_name_is_not_told_it_went_away(installer, monkeypatch) -> None:
    # Organization 8 created its own "gone" while organization 7's delete was being announced.
    installer["bound"] = frozenset({"default", "gone"})
    deleted = dict(event_type="orcastra.project.deleted.v1", resource_type="project", resource_name="gone")
    assert _interested(Install(id=1, organization_id=8), None, monkeypatch, reached={7}, **deleted) == []
    installer["bound"] = None
    assert _interested(Install(id=1, organization_id=8), None, monkeypatch, reached=(), **deleted) == [10], (
        "on a cluster no other party holds, the stored grant decides as before"
    )


def test_a_slow_identity_provider_holds_the_addon_to_the_bound(installer, monkeypatch) -> None:
    async def slow_admin(_email, *, roles=None):
        await asyncio.sleep(1)
        return True

    monkeypatch.setattr(kpb, "creator_is_admin", slow_admin)
    monkeypatch.setattr(reach, "ROLE_SECONDS", 0.05)
    assert _interested(Install(id=1), "own", monkeypatch) == [10], "its own projects are still heard"
    assert _interested(Install(id=1), "theirs", monkeypatch) == [], "an unanswered provider is not an Admin"


def test_an_event_reads_each_installers_bound_once(installer, monkeypatch) -> None:
    from tests.test_a_lifecycle_change_reaches_the_addons_watching_it import FakeDB

    reads: List[str] = []

    async def bound(api_key, _cluster_id, _db):
        reads.append(api_key["created_by"])
        return "partner@example.test", BOUND

    monkeypatch.setattr(kpb, "_bound", bound)
    subs = [FakeSubscription(id=10 + i, installation_id=i, event_types=["orcastra.instance.created.v1"]) for i in range(3)]
    installs = [Install(id=i) for i in range(3)]

    async def run():
        return await emitter._subscriptions_for_cluster(
            FakeDB(list(zip(subs, installs))), "nuc2", "orcastra.instance.created.v1", "own"
        )

    async def no_generations():
        return None

    monkeypatch.setattr("app.services.cluster_generations.current_or_none", no_generations)
    monkeypatch.setattr("app.services.cluster_generations.reachable", lambda scope, generations: {"nuc2"})
    assert [s.id for s in asyncio.run(run())] == [10, 11, 12]
    assert reads == ["partner@example.test"], "three subscriptions of one installer, one bound read"


async def test_a_migration_project_delete_names_its_owner_and_the_policies_that_held_it(monkeypatch) -> None:
    from types import SimpleNamespace

    from app.services.migrations import project_retention_store as store

    captured: Dict[str, Any] = {}

    async def fenced(*_a, **_k):
        return True

    async def events(_db, **kwargs):
        captured.update(kwargs)

    async def revoke(_db, *, cluster_id, project_name, commit, held_by):
        held_by.add(9)

    class _Db:
        async def execute(self, *_a, **_k):
            return None

        async def commit(self):
            return None

    monkeypatch.setattr(store, "_fenced_update", fenced)
    monkeypatch.setattr(store, "_events", events)
    monkeypatch.setattr(store, "revoke_project_grants", revoke)
    job = SimpleNamespace(job_id="job-1", fence_token=1, source_cluster_id="nuc2", source_project="gone",
                          source_organization_id=7)
    outbox = [
        {"event_type": "orcastra.project.deleted.v1", "extra": {"cluster_id": "nuc2", "resource_name": "gone"}},
        {"event_type": "orcastra.instance.deleted.v1", "extra": {"cluster_id": "nuc2", "resource_name": "vm"}},
    ]
    await store.finish_project_cleanup(_Db(), job, worker_id="w", event_actor={}, message="done", outbox_events=outbox)
    project_event, instance_event = captured["outbox_events"]
    assert project_event["extra"]["held_organizations"] == [7, 9]
    assert "held_organizations" not in instance_event["extra"]

    from app.services.migrations import completion

    emitted: Dict[str, Any] = {}

    async def emit(**kwargs):
        emitted.update(kwargs)
        return 1

    monkeypatch.setattr(completion, "emit_resource_event", emit)
    row = SimpleNamespace(channel="webhook", event_type="orcastra.project.deleted.v1", event_id="evt-1", payload={
        "cluster_id": "nuc2", "project": "gone", "resource_name": "gone", "occurred_at": "2026-11-06T00:00:00Z",
        **project_event["extra"],
    })
    assert await completion.deliver_completion_event(row) is True
    assert list(emitted["reached_organizations"]) == [7, 9]


def test_a_slow_installer_does_not_cost_the_others_their_events(monkeypatch) -> None:
    import time

    from tests.test_a_lifecycle_change_reaches_the_addons_watching_it import FakeDB

    async def bound(api_key, _cluster_id, _db):
        if api_key["created_by"] == "slow@example.test":
            await asyncio.sleep(5)
        return api_key["created_by"], BOUND

    async def not_admin(_email, *, roles=None):
        return False

    async def no_generations():
        return None

    monkeypatch.setattr(kpb, "_bound", bound)
    monkeypatch.setattr(kpb, "creator_is_admin", not_admin)
    monkeypatch.setattr(reach, "EVENT_REACH_SECONDS", 0.2)
    monkeypatch.setattr(emitter, "EVENT_REACH_SECONDS", 0.2)
    monkeypatch.setattr("app.services.cluster_generations.current_or_none", no_generations)
    monkeypatch.setattr("app.services.cluster_generations.reachable", lambda scope, generations: {"nuc2"})
    rows = [
        (FakeSubscription(id=10, installation_id=1, event_types=["orcastra.instance.created.v1"]),
         Install(id=1, installed_by="slow@example.test")),
        (FakeSubscription(id=11, installation_id=2, event_types=["orcastra.instance.created.v1"]),
         Install(id=2, installed_by="fast@example.test")),
    ]
    started = time.monotonic()
    heard = asyncio.run(emitter._subscriptions_for_cluster(FakeDB(rows), "nuc2", "orcastra.instance.created.v1", "own"))
    assert [s.id for s in heard] == [11], "the slow installer's bound is unread at the deadline"
    assert time.monotonic() - started < 2, "every installer shares one deadline"


async def test_a_role_answer_that_arrives_late_still_fills_the_cache(monkeypatch) -> None:
    from app.core.rbac import Role
    from app.services.authentik_role_reading import RoleReading

    async def bound(_api_key, _cluster_id, _db):
        return "admin@example.test", BOUND

    class SlowRoles:
        calls = 0

        async def read_current_roles(self, _email):
            SlowRoles.calls += 1
            await asyncio.sleep(0.2)
            return RoleReading(frozenset({Role.ADMIN}), "read")

    monkeypatch.setattr(kpb, "_bound", bound)
    as_key = {"id": "oak_addon", "created_by": "admin@example.test"}
    assert await kpb.party_bound(as_key, "nuc2", roles=SlowRoles(), role_seconds=0.05) == (BOUND, True)
    await asyncio.sleep(0.4)
    assert await kpb.party_bound(as_key, "nuc2", roles=SlowRoles(), role_seconds=0.05) == (None, False)
    assert SlowRoles.calls == 1, "the late answer was kept rather than asked for again"



async def test_concurrent_events_share_one_bound_read_and_a_cap(monkeypatch) -> None:
    from tests.test_a_lifecycle_change_reaches_the_addons_watching_it import FakeDB

    reads: List[str] = []
    running = {"now": 0, "most": 0}

    async def bound(api_key, _cluster_id, _db):
        reads.append(api_key["created_by"])
        running["now"] += 1
        running["most"] = max(running["most"], running["now"])
        await asyncio.sleep(0.05)
        running["now"] -= 1
        return api_key["created_by"], BOUND

    async def not_admin(_email, *, roles=None):
        return False

    async def no_generations():
        return None

    monkeypatch.setattr(kpb, "_bound", bound)
    monkeypatch.setattr(kpb, "creator_is_admin", not_admin)
    monkeypatch.setattr("app.services.cluster_generations.current_or_none", no_generations)
    monkeypatch.setattr("app.services.cluster_generations.reachable", lambda scope, generations: {"nuc2"})

    def one_emit(installers):
        rows = [
            (FakeSubscription(id=10 + i, installation_id=i, event_types=["orcastra.instance.created.v1"]),
             Install(id=i, installed_by=email))
            for i, email in enumerate(installers)
        ]
        return emitter._subscriptions_for_cluster(FakeDB(rows), "nuc2", "orcastra.instance.created.v1", "own")

    await asyncio.gather(one_emit(["a@example.test"]), one_emit(["a@example.test"]), one_emit(["a@example.test"]))
    assert reads == ["a@example.test"], "three events at once, one read of that installer's bound"

    reads.clear()
    many = [f"p{i}@example.test" for i in range(12)]
    heard = await one_emit(many)
    assert len(heard) == 12 and sorted(reads) == sorted(many)
    assert running["most"] <= reach.BOUND_READS_AT_ONCE, "a burst does not take a connection per installer"


async def test_a_cancelled_event_leaves_no_waiter_behind(monkeypatch) -> None:
    async def slow(_api_key, _cluster_id, _db):
        await asyncio.sleep(1)
        return "partner@example.test", BOUND

    monkeypatch.setattr(kpb, "_bound", slow)
    memo: Dict[Any, Any] = {}
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(reach.read_bounds([(Install(id=1), "nuc2")], memo, 5), 0.05)
    await asyncio.sleep(0)
    waiters = [t for t in asyncio.all_tasks() if t.get_coro().__name__ == "_installer_bound"]
    assert waiters == [], "the instance create emit's 2 s cut left a waiter holding the event"
