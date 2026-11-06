"""An add-on hears about a project only when its installation reaches that project.

Webhooks were routed by cluster alone, so an add-on a Partner installed on a cluster another
organization also holds received that party's project, instance and volume names (security review
of the RC5 follow-up round, 2026-11-06). The installation's grant is now held to its installer's
bound, as its key is (services/key_party_bound.py), and the event's project must be inside it, at
enqueue and again at delivery.
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


def _interested(install, project, monkeypatch, *, resource_type="instance", resource_name="vm-a") -> List[int]:
    sub = FakeSubscription(id=10, installation_id=install.id, event_types=["orcastra.instance.created.v1"])
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
        event_type="orcastra.instance.created.v1", cluster_id="nuc2",
        resource_type=resource_type, resource_name=resource_name, project=project,
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


def test_on_an_unshared_cluster_routing_by_cluster_is_unchanged(installer, monkeypatch) -> None:
    installer["bound"] = None
    named = Install(id=1, granted_scope=[{"cluster_id": "nuc2", "projects": ["default"]}])
    assert _interested(named, "elsewhere", monkeypatch) == [10]


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
