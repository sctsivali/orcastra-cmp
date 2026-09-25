"""A rebind that fails partway leaves no move behind, so no move is ever committed without its record.

reconcile_personal_clusters flushes each moved row and commits once at the end. When the share
audit refused a later cluster, the exception skipped that commit and the records, and the member
sync only logged it. The earlier moves were still flushed in the session, and the offboarding
reconcile's per-row commit then wrote them: a cluster bound to an org with no rebind record.
"""

from typing import Any, Dict

import pytest
from sqlalchemy import select

from app.models.db_models import Organization, OrganizationMember
from app.routers import integrations
from app.services import integration_cluster_rebind
from tests._sqlite_session import AsyncSessionFacade, org_tables, sqlite_session
from tests.test_a_push_rebinds_only_into_the_org_its_key_claimed import (
    PARTNER,
    body,
    bound,
    key,
    member,
    org,
    personal_cluster,
    push,
    world,  # noqa: F401 - the fixture
)

K_PARTNER = "oak_partner"
LEAVER = "leaver@example.test"


def reconcile(monkeypatch: pytest.MonkeyPatch, *, enabled: bool, dry_run: bool) -> None:
    patched = integrations.get_settings().model_copy(update={
        "member_reconcile_enabled": enabled, "member_reconcile_dry_run": dry_run,
    })
    monkeypatch.setattr(integrations, "get_settings", lambda: patched)


async def claimed_org_with_a_leaver() -> AsyncSessionFacade:
    db = sqlite_session(*org_tables())
    db.add(Organization(name="Partner", slug="partner", source="orcahub", external_id="ext-p",
                        api_key_id=K_PARTNER, created_by="system", is_active=True))
    await db.commit()
    db.add(OrganizationMember(organization_id=1, user_email=LEAVER, role="tenant"))
    await db.commit()
    await personal_cluster(db, "a-ok", PARTNER)
    await personal_cluster(db, "b-refused", PARTNER)
    return db


async def after_the_request(db: AsyncSessionFacade) -> Dict[str, Any]:
    """What is committed: anything the request left uncommitted is dropped, as get_db's close does."""
    db.sync.rollback()
    db.sync.expire_all()
    return await bound(db)


async def test_a_refused_later_cluster_leaves_the_earlier_move_uncommitted(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    reconcile(monkeypatch, enabled=True, dry_run=False)
    db = await claimed_org_with_a_leaver()
    world["fail"].add("b-refused")

    result = await push(db, key(K_PARTNER, PARTNER),
                        body([org("ext-p", "partner")], [member(PARTNER, "ext-p", "PARTNER")]))
    assert result.members_removed == 1, "the offboarding reconcile ran and committed after the rebind"
    assert world["shares"] == [("a-ok", 1), ("b-refused", 1)]
    assert await after_the_request(db) == {"a-ok": None, "b-refused": None}
    assert world["log"].calls == []

    members = (await db.execute(select(OrganizationMember.user_email))).scalars().all()
    assert members == [PARTNER], "the member upsert and the prune before and after it are kept"


async def test_the_next_push_moves_both_once_the_audit_allows_it(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    reconcile(monkeypatch, enabled=True, dry_run=False)
    db = await claimed_org_with_a_leaver()
    world["fail"].add("b-refused")
    await push(db, key(K_PARTNER, PARTNER),
               body([org("ext-p", "partner")], [member(PARTNER, "ext-p", "PARTNER")]))
    world["fail"].clear()
    await push(db, key(K_PARTNER, PARTNER),
               body([org("ext-p", "partner")], [member(PARTNER, "ext-p", "PARTNER")]))
    assert await after_the_request(db) == {"a-ok": 1, "b-refused": 1}
    assert sorted(c["target_id"] for c in world["log"].calls) == ["a-ok", "b-refused"]


async def test_the_failure_is_rolled_back_and_raised_to_the_member_sync(
    world: Dict[str, Any], caplog: pytest.LogCaptureFixture  # noqa: F811
) -> None:
    """Rolled back and raised, so the sync stays best-effort and still says what failed."""
    db = await claimed_org_with_a_leaver()
    db.add(OrganizationMember(organization_id=1, user_email=PARTNER, role="partner"))
    await db.commit()
    world["fail"].add("b-refused")
    pushed = [integrations.SyncMember(**member(PARTNER, "ext-p", "PARTNER"))]

    with pytest.raises(RuntimeError, match="share audit refused b-refused"):
        await integration_cluster_rebind.rebind_creator_clusters(
            db, PARTNER, pushed, api_key_id=K_PARTNER, creator=PARTNER, roles=world["directory"],
        )
    await db.commit()
    assert await bound(db) == {"a-ok": None, "b-refused": None}, "nothing was left flushed to commit"

    await integrations._upsert_synced_members(db, pushed, K_PARTNER, PARTNER)
    assert "share audit refused b-refused" in caplog.text
    assert world["log"].calls == []
