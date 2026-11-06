"""An add-on hears about a project only when its installation reaches that project.

Webhooks were routed by cluster alone, so an add-on a Partner installed on a cluster another
organization also holds received that party's project, instance and volume names (security review
of the RC5 follow-up round, 2026-11-06). The event's project must now be in the installation's
grant for that cluster (owner decision 2026-11-06: filtered per grant on every cluster), the grant
held to its installer's bound where another party holds the cluster, as its key is
(services/key_party_bound.py), at enqueue, at delivery, in the delivery log and on redeliver.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List

import pytest

from app.services import key_party_bound as kpb
from app.services.webhooks import emitter
from tests.test_a_lifecycle_change_reaches_the_addons_watching_it import FakeSubscription, _run

BOUND = frozenset({"default", "own"})


@dataclass
class Install:
    id: int
    status: str = "active"
    granted_scope: List[Dict[str, Any]] = field(default_factory=lambda: [{"cluster_id": "nuc2", "projects": ["*"]}])
    installed_by: str = "partner@example.test"
    api_key_id: str = "oak_addon"
    organization_id: int = 7


@pytest.fixture
def installer(monkeypatch):
    state = {"bound": BOUND, "admin": False}

    async def bound(_api_key, _cluster_id, _db):
        return "partner@example.test", state["bound"]

    async def is_admin(_email, *, roles=None):
        return state["admin"]

    monkeypatch.setattr(kpb, "_bound", bound)
    monkeypatch.setattr(kpb, "creator_is_admin", is_admin)
    return state


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


async def test_a_delivery_is_checked_again_before_it_goes_out(installer, monkeypatch) -> None:
    from app.services.webhooks import dispatcher

    install = Install(id=1)

    class _Rows:
        def scalar_one_or_none(self):
            return install

    class _DB:
        async def execute(self, _statement):
            return _Rows()

    async def no_generations():
        return None

    monkeypatch.setattr("app.services.cluster_generations.current_or_none", no_generations)
    monkeypatch.setattr("app.services.cluster_generations.reachable", lambda scope, generations: {"nuc2"})
    sub = FakeSubscription(id=10, installation_id=1, event_types=["orcastra.instance.created.v1"])
    theirs = {"data": {"cluster_id": "nuc2", "resource_type": "instance", "resource_name": "vm", "project": "theirs"}}
    own = {"data": {"cluster_id": "nuc2", "resource_type": "instance", "resource_name": "vm", "project": "own"}}
    assert await dispatcher._still_authorised(_DB(), sub, theirs) == "the installation no longer reaches that project"
    assert await dispatcher._still_authorised(_DB(), sub, own) is None


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
    monkeypatch.setattr(kpb, "REACH_SECONDS", 0.05)
    assert _interested(Install(id=1), "own", monkeypatch) == []


async def test_a_queued_payload_is_judged_by_what_it_names(installer) -> None:
    install = Install(id=1)

    def instance(project):
        data = {"cluster_id": "nuc2", "resource_type": "instance", "resource_name": "vm"}
        return {"type": "orcastra.instance.created.v1", "data": {**data, **({"project": project} if project else {})}}

    assert await kpb.payload_reaches(install, instance("own")) is True
    assert await kpb.payload_reaches(install, instance("theirs")) is False
    assert await kpb.payload_reaches(install, instance(None)) is False, "a row that names no project is not reached"
    about_the_install = {"type": "orcastra.installation.updated.v1", "data": {"resource_type": "installation"}}
    assert await kpb.payload_reaches(install, about_the_install) is True
    deleted = {"type": "orcastra.project.deleted.v1",
               "data": {"cluster_id": "nuc2", "resource_type": "project", "resource_name": "gone"}}
    assert await kpb.payload_reaches(install, deleted) is True, "a delete is judged by the stored grant"
    named = Install(id=1, granted_scope=[{"cluster_id": "nuc2", "projects": ["own"]}])
    assert await kpb.payload_reaches(named, deleted) is False


async def test_a_queued_project_delete_is_delivered_by_the_stored_grant(installer, monkeypatch) -> None:
    from app.services.webhooks import dispatcher

    install = Install(id=1, granted_scope=[{"cluster_id": "nuc2", "projects": ["own", "gone"]}])

    class _Rows:
        def scalar_one_or_none(self):
            return install

    class _DB:
        async def execute(self, _statement):
            return _Rows()

    async def no_generations():
        return None

    monkeypatch.setattr("app.services.cluster_generations.current_or_none", no_generations)
    monkeypatch.setattr("app.services.cluster_generations.reachable", lambda scope, generations: {"nuc2"})
    sub = FakeSubscription(id=10, installation_id=1, event_types=["orcastra.project.deleted.v1"])

    def deleted(name):
        return {"type": "orcastra.project.deleted.v1",
                "data": {"cluster_id": "nuc2", "resource_type": "project", "resource_name": name}}

    assert await dispatcher._still_authorised(_DB(), sub, deleted("gone")) is None
    assert await dispatcher._still_authorised(_DB(), sub, deleted("theirs")) == (
        "the installation no longer reaches that project"
    )


def _delivery(ident: int, project: str):
    from tests.test_the_delivery_log_can_be_paged import Delivery

    row = Delivery(ident, "delivered")
    row.payload = {"type": "orcastra.instance.created.v1",
                   "data": {"cluster_id": "nuc2", "resource_type": "instance", "resource_name": "vm", "project": project}}
    return row


async def test_the_delivery_log_hides_what_the_installation_does_not_reach(installer, monkeypatch) -> None:
    from app.routers import extension_webhooks
    from tests.test_the_delivery_log_can_be_paged import FakeDB

    async def install_for(_db, _install_id, _user):
        return Install(id=1)

    monkeypatch.setattr(extension_webhooks, "_install_for", install_for)
    db = FakeDB([_delivery(2, "own"), _delivery(1, "theirs")], subscription_ids=[1])
    page = await extension_webhooks.list_deliveries(install_id=1, user=object(), db=db, state=None, limit=50, offset=0)
    payloads = {d["id"]: d["payload"] for d in page.deliveries}
    assert payloads[2]["data"]["project"] == "own"
    assert payloads[1] == extension_webhooks.REDACTED_PAYLOAD, "another party's names stay out of the log"
    assert page.total == 2, "the row is still counted; only its payload is withheld"


async def test_a_delivery_the_installation_does_not_reach_cannot_be_sent_again(installer, monkeypatch) -> None:
    from fastapi import HTTPException

    from app.routers import extension_webhooks

    async def install_for(_db, _install_id, _user):
        return Install(id=1)

    class _One:
        def __init__(self, row):
            self.row = row

        def scalar_one_or_none(self):
            return self.row

    class _DB:
        def __init__(self, row):
            self.row = row
            self.commits = 0

        async def execute(self, _statement):
            return _One(self.row)

        async def commit(self):
            self.commits += 1

    monkeypatch.setattr(extension_webhooks, "_install_for", install_for)
    monkeypatch.setattr(extension_webhooks.activity_logger, "log_activity", lambda **_kw: None)
    theirs = _DB(_delivery(1, "theirs"))
    with pytest.raises(HTTPException) as refused:
        await extension_webhooks.redeliver(install_id=1, delivery_id=1, user=object(), db=theirs)
    assert refused.value.status_code == 404 and theirs.commits == 0
    own = _DB(_delivery(2, "own"))
    assert (await extension_webhooks.redeliver(install_id=1, delivery_id=2, user=object(), db=own))["state"] == "pending"


async def test_a_revoke_names_the_organizations_whose_policies_held_the_project() -> None:
    from app.services import project_grants
    from tests._project_grant_fakes import GrantSession, Policy, Rule

    held, other = Policy(7, "Partner A", id=501), Policy(8, "Partner B", id=502)
    session = GrantSession(policies=[held, other], rules=[Rule(501, "nuc2", "gone"), Rule(502, "nuc2", "kept")])
    held_by: set = set()
    await project_grants.revoke_project_grants(session, cluster_id="nuc2", project_name="gone", held_by=held_by)
    assert held_by == {7}
