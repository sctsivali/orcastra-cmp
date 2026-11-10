"""A queued add-on event is judged again where it is sent, listed or sent again.

The bound is judged when the event is queued (test_an_addon_hears_only_its_installers_projects.py).
Delivery then checks the stored grant only, so an identity provider outage or a restart cannot
drop it. The delivery log and redeliver judge the current bound, so another party's names in old
rows stay out of them.
"""

import asyncio
import pytest

from app.services import key_party_bound as kpb
from app.services.webhooks import reach
from tests._addon_reach_fakes import BOUND, Install, hold_installer
from tests.test_a_lifecycle_change_reaches_the_addons_watching_it import FakeSubscription


@pytest.fixture
def installer(monkeypatch):
    return hold_installer(monkeypatch)


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
    unnamed = {"data": {"cluster_id": "nuc2", "resource_type": "instance", "resource_name": "vm"}}
    install.granted_scope = [{"cluster_id": "nuc2", "projects": ["own"]}]
    refused = "the installation's grant no longer names that project"
    assert await dispatcher._still_authorised(_DB(), sub, theirs) == refused
    assert await dispatcher._still_authorised(_DB(), sub, unnamed) == refused
    assert await dispatcher._still_authorised(_DB(), sub, own) is None
    # The bound was judged at enqueue; delivery does not ask the identity provider again.
    install.granted_scope = [{"cluster_id": "nuc2", "projects": ["*"]}]

    async def no_idp(_email, *, roles=None):
        raise AssertionError("the identity provider was asked at delivery")

    monkeypatch.setattr(kpb, "creator_is_admin", no_idp)
    assert await dispatcher._still_authorised(_DB(), sub, theirs) is None



async def test_a_queued_payload_is_judged_by_what_it_names(installer) -> None:
    install = Install(id=1)

    def instance(project):
        data = {"cluster_id": "nuc2", "resource_type": "instance", "resource_name": "vm"}
        return {"type": "orcastra.instance.created.v1", "data": {**data, **({"project": project} if project else {})}}

    assert await reach.payload_reaches(install, instance("own")) is True
    assert await reach.payload_reaches(install, instance("theirs")) is False
    assert await reach.payload_reaches(install, instance(None)) is False, "a row that names no project is not reached"
    about_the_install = {"type": "orcastra.installation.updated.v1", "data": {"resource_type": "installation"}}
    assert await reach.payload_reaches(install, about_the_install) is True
    deleted = {"type": "orcastra.project.deleted.v1",
               "data": {"cluster_id": "nuc2", "resource_type": "project", "resource_name": "gone"}}
    assert await reach.payload_reaches(install, deleted) is False, (
        "the queue does not keep who held a deleted project, so on a held cluster it stays hidden"
    )
    installer["bound"] = None
    assert await reach.payload_reaches(install, deleted) is True, "elsewhere the stored grant decides"
    named = Install(id=1, granted_scope=[{"cluster_id": "nuc2", "projects": ["own"]}])
    assert await reach.payload_reaches(named, deleted) is False



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
        "the installation's grant no longer names that project"
    )



def _delivery(ident: int, project: str):
    from tests.test_the_delivery_log_can_be_paged import Delivery

    row = Delivery(ident, "delivered")
    row.response_excerpt = "ok"
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
    excerpts = {d["id"]: d["response_excerpt"] for d in page.deliveries}
    assert payloads[2]["data"]["project"] == "own" and excerpts[2] == "ok"
    assert payloads[1] == extension_webhooks.REDACTED_PAYLOAD, "another party's names stay out of the log"
    assert excerpts[1] is None, "a receiver's answer can echo what it was sent"
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



async def test_a_page_of_the_log_waits_once_for_a_slow_cluster(installer, monkeypatch) -> None:
    import time

    from app.routers import extension_webhooks
    from tests.test_the_delivery_log_can_be_paged import FakeDB

    async def bound(_api_key, cluster_id, _db):
        if cluster_id == "slow":
            await asyncio.sleep(5)
        return "partner@example.test", BOUND

    async def install_for(_db, _install_id, _user):
        return Install(id=1, granted_scope=[{"cluster_id": "nuc2", "projects": ["*"]},
                                            {"cluster_id": "slow", "projects": ["*"]}])

    monkeypatch.setattr(kpb, "_bound", bound)
    monkeypatch.setattr(reach, "REACH_SECONDS", 0.2)
    monkeypatch.setattr(extension_webhooks, "_install_for", install_for)
    rows = [_delivery(5, "own")]
    for ident in (4, 3, 2):
        row = _delivery(ident, "own")
        row.payload["data"]["cluster_id"] = "slow"
        rows.append(row)
    started = time.monotonic()
    page = await extension_webhooks.list_deliveries(
        install_id=1, user=object(), db=FakeDB(rows, subscription_ids=[1]), state=None, limit=50, offset=0
    )
    payloads = {d["id"]: d["payload"] for d in page.deliveries}
    assert payloads[5]["data"]["project"] == "own"
    assert all(payloads[i] == extension_webhooks.REDACTED_PAYLOAD for i in (4, 3, 2))
    assert time.monotonic() - started < 2, "three rows on the slow cluster wait once, not three times"

