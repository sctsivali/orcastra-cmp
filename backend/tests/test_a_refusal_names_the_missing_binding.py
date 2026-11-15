"""A refused project create says which layer is missing and where to fix it.

OrcaHub reported "None of the organizations this key's creator ... is bound to cluster" with no
way to tell that a cluster binding was missing, while the key's cluster grant (Edit Access) and its
owner rows (Synced Organizations) were both in place. The refusal now names the cluster, the
organizations of the creator this key owns, and the screen that binds one of them. It never names
the creator's email or another party's organizations. When the key was held to its creator's bound
only because the identity provider did not answer the admin-role read, the refusal says so, since
retrying is then the fix.
"""

from typing import Any, Dict

import pytest

from app.models.db_models import Organization, OrganizationMember, OrganizationOwnerKey
from app.services import key_party_bound as kpb
from app.services.key_party_bound import narrow_to_party_bound, project_change_refusal
from tests._raw_proxy_fakes import CLUSTER_ID, key
from tests._sqlite_session import sqlite_session

CREATOR = "partner@example.test"
STAR: Dict[str, Any] = {**key(["*"]), "created_by": CREATOR}


async def _async(value):
    return value


@pytest.fixture
def unbound(monkeypatch):
    """The creator's party does not hold CLUSTER_ID and runs no organization bound to it."""
    state: Dict[str, Any] = {"admin": False, "named": (["Acme Synced (122)"], 0)}

    async def bound(api_key, cluster_id, _db):
        return CREATOR, frozenset({"default"})

    async def is_admin(_email, *, roles=None):
        return state["admin"]

    async def no_one_else(*_args, **_kwargs):
        return None

    async def named(_db, _creator, _key_id):
        return state["named"]

    monkeypatch.setattr(kpb, "_bound", bound)
    monkeypatch.setattr(kpb, "creator_is_admin", is_admin)
    monkeypatch.setattr(kpb, "_creator", lambda api_key, db: _async(CREATOR))
    monkeypatch.setattr(kpb, "creator_organizations_for_refusal", named)
    monkeypatch.setattr("app.services.api_key_grant_bound.refuse_a_project_another_party_holds", no_one_else)
    monkeypatch.setattr("app.core.org_scope.org_to_hold_project_grant", lambda *_a, **_k: _async(None))
    return state


async def _refusal(api_key: Dict[str, Any]) -> str:
    narrowed = await narrow_to_party_bound(dict(api_key))
    refusal = await project_change_refusal(narrowed, CLUSTER_ID, "fresh", creating=True, db=object())
    assert refusal is not None and refusal.status_code == 403
    assert isinstance(refusal.detail, str), "a string detail, which every client can show"
    return refusal.detail


async def test_a_key_refusal_names_the_cluster_the_layer_and_where_to_bind(unbound) -> None:
    detail = await _refusal(STAR)
    assert f"is bound to cluster '{CLUSTER_ID}'" in detail
    assert "Organizations this key owns: Acme Synced (122)." in detail
    assert "Settings > Integrations > Synced Organizations > Manage clusters" in detail
    assert CREATOR not in detail, "the creator's email is not the key holder's to read"


async def test_a_key_that_owns_none_of_the_creators_organizations_says_so(unbound) -> None:
    unbound["named"] = ([], 2)
    detail = await _refusal(STAR)
    assert "The key's creator runs 2 organizations; this key owns none of them." in detail


async def test_a_creator_who_runs_no_organization_is_told_so(unbound) -> None:
    unbound["named"] = ([], 0)
    detail = await _refusal(STAR)
    assert "The key's creator runs no organization." in detail


async def test_an_unanswered_admin_role_read_asks_for_a_retry(unbound) -> None:
    unbound["admin"] = None
    narrowed = await narrow_to_party_bound(dict(STAR))
    assert narrowed.get(kpb.ROLE_UNKNOWN) == [CLUSTER_ID]
    detail = await _refusal(STAR)
    assert "identity provider did not answer" in detail and "Retry" in detail


async def test_a_known_role_carries_no_retry_hint(unbound) -> None:
    detail = await _refusal(STAR)
    assert "identity provider" not in detail


# ── the organizations named come from the database ─────────────────────────────────────────────

async def test_only_the_creators_own_organizations_that_this_key_owns_are_named() -> None:
    db = sqlite_session(Organization, OrganizationMember, OrganizationOwnerKey)
    db.add_all([
        Organization(id=1, name="Own Synced", slug="own-synced", created_by="system", is_active=True,
                     source="orcahub", api_key_id="oak_test"),
        OrganizationOwnerKey(organization_id=1, api_key_id="oak_test", added_via="claim"),
        OrganizationMember(organization_id=1, user_email=CREATOR, role="partner"),
        # the creator runs this one, but another key owns it: counted, not named
        Organization(id=2, name="Other Key", slug="other-key", created_by=CREATOR, is_active=True,
                     source="orcahub", api_key_id="oak_other"),
        OrganizationOwnerKey(organization_id=2, api_key_id="oak_other", added_via="claim"),
        # a plain member row is not running the organization
        Organization(id=3, name="Seller", slug="seller", created_by="system", is_active=True,
                     source="orcahub", api_key_id="oak_test"),
        OrganizationOwnerKey(organization_id=3, api_key_id="oak_test", added_via="claim"),
        OrganizationMember(organization_id=3, user_email=CREATOR, role="tenant"),
    ])
    await db.commit()

    named, others = await kpb.creator_organizations_for_refusal(db, CREATOR, "oak_test")

    assert named == ["Own Synced (1)"]
    assert others == 1
