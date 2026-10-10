"""Only those who run an organization install, read and manage its add-ons.

Every add-on surface admitted any membership of the organization (org_scope.get_user_org_ids). A
partner whose only row in an organization was a buyer's tenant row could therefore install add-ons
there and read and manage all of them, settings secrets and webhooks included, and so could a
dashboard teammate stored as a plain member. The surfaces now ask orgs_the_user_runs, the rule a
rotation of an owning add-on already used: a partner row (email compared without case), or the
creator of an organization made in this dashboard. An OrcaHub organization's creator is the key's
minter, a seller, and a stale "admin" row is one a demotion left behind; neither runs it. An
Administrator still manages every organization.
"""

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional, Sequence, Tuple

import pytest
from fastapi import HTTPException

from app.core.auth import AuthenticatedUser
from app.models.db_models import Organization, OrganizationMember
from app.routers import extension_installs, extension_webhooks
from app.services import extension_scope
from app.services.extension_credential_guard import orgs_the_user_runs, runs_installation_organization
from tests._sqlite_session import org_tables, sqlite_session

ME = "partner@example.test"
PARTNER = AuthenticatedUser(user_id="p1", email=ME, groups=["role_partner"], roles=["role_partner"])
ADMIN = AuthenticatedUser(user_id="a1", email="admin@example.test", groups=["role_admin"], roles=["role_admin"])


def org(db: Any, slug: str, *, source: str = "local", created_by: str = "someone@example.test",
        rows: Sequence[Tuple[str, str]] = (), active: bool = True) -> int:
    row = Organization(name=slug.title(), slug=slug, source=source, created_by=created_by, is_active=active,
                       external_id=f"ext-{slug}" if source == "orcahub" else None)
    db.add(row)
    db.sync.flush()
    for email, role in rows:
        db.add(OrganizationMember(organization_id=row.id, user_email=email, role=role))
    db.sync.commit()
    return row.id


def world() -> Tuple[Any, dict]:
    db = sqlite_session(*org_tables())
    ids = {
        "partner": org(db, "partnered", rows=[(ME, "partner")]),
        "partner-other-case": org(db, "partnered-caps", rows=[("Partner@Example.TEST", "partner")]),
        "local-creator": org(db, "made-here", created_by=ME.upper(), rows=[(ME, "member")]),
        "tenant": org(db, "bought-from", source="orcahub", rows=[(ME, "tenant")]),
        "member": org(db, "teammate", rows=[(ME, "member")]),
        "minter": org(db, "provisioned", source="orcahub", created_by=ME),
        "stale-admin": org(db, "demoted", rows=[(ME, "admin")]),
        "inactive": org(db, "closed", rows=[(ME, "partner")], active=False),
        "unrelated": org(db, "theirs"),
    }
    return db, ids


async def test_the_organizations_a_user_runs() -> None:
    db, ids = world()
    assert sorted(await orgs_the_user_runs(db, ME)) == sorted([ids["partner"], ids["partner-other-case"],
                                                               ids["local-creator"]])
    assert await orgs_the_user_runs(db, "") == []


@pytest.mark.parametrize("name, runs", [
    ("partner", True), ("partner-other-case", True), ("local-creator", True), ("tenant", False),
    ("member", False), ("minter", False), ("stale-admin", False), ("unrelated", False),
])
async def test_one_organization_answers_the_same(name: str, runs: bool) -> None:
    db, ids = world()
    assert await runs_installation_organization(db, ids[name], ME) is runs


async def test_the_installation_routes_reach_only_organizations_one_runs() -> None:
    db, ids = world()
    assert sorted(await extension_installs._allowed_org_ids(PARTNER, db)) == sorted(
        [ids["partner"], ids["partner-other-case"], ids["local-creator"]])
    assert await extension_installs._allowed_org_ids(ADMIN, db) is None


async def test_the_preview_of_an_organization_one_does_not_run_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    db, ids = world()

    async def _found(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(slug="billing")

    monkeypatch.setattr(extension_installs.extension_service, "get_extension_or_404", _found)
    with pytest.raises(HTTPException) as hidden:
        await extension_installs.install_preview("billing", ids["tenant"], PARTNER, db)
    assert hidden.value.status_code == 404


@pytest.mark.parametrize("name, visible", [("partner", True), ("tenant", False), ("member", False)])
async def test_the_webhook_routes_reach_only_organizations_one_runs(name: str, visible: bool) -> None:
    db, ids = world()
    row = SimpleNamespace(id=7, organization_id=ids[name])

    class _DB:
        async def execute(self, statement: Any) -> Any:
            if "extension_installations" in str(statement):
                return SimpleNamespace(scalar_one_or_none=lambda: row)
            return await db.execute(statement)

    if visible:
        assert await extension_webhooks._install_for(_DB(), 7, PARTNER) is row
    else:
        with pytest.raises(HTTPException) as hidden:
            await extension_webhooks._install_for(_DB(), 7, PARTNER)
        assert hidden.value.status_code == 404


@pytest.mark.parametrize("name, allowed", [("partner", True), ("tenant", False), ("minter", False)])
async def test_an_install_targets_only_an_organization_one_runs(
    monkeypatch: pytest.MonkeyPatch, name: str, allowed: bool
) -> None:
    db, ids = world()

    async def _bound(cluster_id: str, _db: Any) -> List[Tuple[int, str]]:
        return [(ids[name], name)]

    monkeypatch.setattr(extension_scope, "get_current_allowed_clusters", lambda: {"nuc2"})
    monkeypatch.setattr(extension_scope, "resolve_cluster_orgs", _bound)
    call = extension_scope.assert_install_scope_within_partner(
        db, PARTNER, organization_id=ids[name],
        grants=[{"cluster_id": "nuc2", "permissions": ["read"], "projects": ["default"]}],
        manifest_scope={"permission": "read", "clusters": "selected", "projects": "selected"},
        capabilities=["inventory.read"], is_admin=False,
    )
    if allowed:
        await call
    else:
        with pytest.raises(HTTPException) as refused:
            await call
        assert refused.value.status_code == 404


@pytest.mark.parametrize("user, expected", [(PARTNER, ["made-here", "partnered", "partnered-caps"]),
                                            (ADMIN, None)], ids=["partner", "administrator"])
async def test_the_install_dialog_offers_only_manageable_organizations(
    user: AuthenticatedUser, expected: Optional[List[str]]
) -> None:
    db, _ids = world()
    offered = [o.slug for o in await extension_installs.manageable_organizations(user, db)]
    if expected is None:
        assert "closed" not in offered and {"bought-from", "teammate", "theirs"} <= set(offered)
    else:
        assert sorted(offered) == expected


async def test_an_add_on_is_published_from_an_organization_its_author_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.routers import extensions

    db, ids = world()
    seen: dict = {}

    async def _create(_db: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise HTTPException(status_code=418)

    monkeypatch.setattr(extensions.extension_service, "create_extension", _create)
    with pytest.raises(HTTPException):
        await extensions.register_extension(SimpleNamespace(), None, None, PARTNER, db)
    assert seen["publisher_organization_id"] == min(ids["partner"], ids["partner-other-case"], ids["local-creator"])


def test_no_add_on_surface_asks_for_any_membership() -> None:
    app = Path(__file__).resolve().parents[1] / "app"
    files = sorted(list((app / "routers").glob("extension*.py")) + list((app / "services").glob("extension*.py")))
    offenders = [f"{p.relative_to(app)}" for p in files if re.search(r"\bget_user_org_ids\b", p.read_text())]
    assert files and offenders == [], "an add-on surface admitted any membership instead of orgs_the_user_runs"
