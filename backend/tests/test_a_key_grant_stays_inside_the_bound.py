"""An API key's grant, and the tenant rules a key writes, stay inside what their author may reach.

Minting and editing a key checked cluster ids only, and only a secret rotation asked the project
bound. On a cluster another party also holds, a partner could name that party's project, or '*', and
the key then acted on it through the raw proxy, /proxy and provisioning. Provisioning and the policy
sync checked only the key's own grant, which '*' satisfies for every project on the cluster. The
party is read from real rows on SQLite.
"""

from typing import Any, Dict, List

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.models.db_models import TenantAccessPolicy, TenantPolicyRule
from app.routers import integrations
from app.services import api_key_grant_bound
from tests.test_whole_cluster_state_needs_the_whole_cluster import ADMIN, KEY, MIXED, OWN, SELLER, SOLO, _world


def _user(email: str, group: str = "role_partner") -> AuthenticatedUser:
    return AuthenticatedUser(user_id=f"{group}:{email}", email=email, groups=[group])


def _grant(cluster: str, *projects: str, permissions: Any = ("read",)) -> integrations.ClusterPermissionRequest:
    return integrations.ClusterPermissionRequest(cluster_id=cluster, projects=list(projects), permissions=list(permissions))


@pytest.fixture
def scope():
    clusters = org_scope._allowed_clusters_var.set({OWN, MIXED, SOLO})
    bound = org_scope._visible_projects_var.set({MIXED: {"default", "own-web"}})
    yield
    org_scope._visible_projects_var.reset(bound)
    org_scope._allowed_clusters_var.reset(clusters)


# ── the dashboard grant ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cluster,project", [
    (MIXED, "*"), (MIXED, "theirs"), (SOLO, "*"),
], ids=["wildcard-beside-another-party", "another-partys-project", "wildcard-where-another-party-holds-alone"])
async def test_a_grant_past_the_bound_is_refused(scope, cluster: str, project: str) -> None:
    db = await _world()
    with pytest.raises(HTTPException) as caught:
        await integrations._refuse_a_grant_past_the_project_bound(db, SELLER, [_grant(cluster, project)])
    assert caught.value.status_code == 403 and f"{cluster}/{project}" in caught.value.detail


@pytest.mark.parametrize("cluster,project", [
    (MIXED, "own-web"), (MIXED, "default"), (OWN, "*"),
], ids=["inside-the-bound", "default", "wildcard-on-the-sellers-own-cluster-after-a-sale"])
async def test_a_grant_inside_the_bound_passes(scope, cluster: str, project: str) -> None:
    db = await _world()
    await integrations._refuse_a_grant_past_the_project_bound(db, SELLER, [_grant(cluster, project)])


async def test_an_admin_is_not_bound_and_the_database_is_not_asked() -> None:
    db = await _world()
    await integrations._refuse_a_grant_past_the_project_bound(db, ADMIN, [_grant(MIXED, "*")])
    assert db.statements == []


async def test_an_edit_keeps_what_the_key_already_holds_at_its_level_or_lower(scope) -> None:
    db = await _world()
    held = api_key_grant_bound.already_granted(
        {"cluster_permissions": [{"cluster_id": MIXED, "projects": ["legacy"], "permissions": ["read", "operate"]}]}
    )
    await integrations._refuse_a_grant_past_the_project_bound(db, SELLER, [_grant(MIXED, "legacy")], held)
    with pytest.raises(HTTPException) as caught:
        await integrations._refuse_a_grant_past_the_project_bound(
            db, SELLER, [_grant(MIXED, "legacy", "theirs")], held
        )
    assert "theirs" in caught.value.detail and "legacy" not in caught.value.detail.split("[")[1]


@pytest.mark.parametrize("door", ["create", "update"])
async def test_both_doors_refuse_before_a_cluster_is_read(scope, monkeypatch: pytest.MonkeyPatch, door: str) -> None:
    """Minting and editing ask the bound after the scope check and before any cluster is read."""
    db = await _world()
    read: List[Any] = []

    async def _projects_exist(_service: Any, grants: Any) -> None:
        read.append(grants)

    async def _nothing(*_a: Any, **_k: Any) -> None:
        return None

    async def _key(*_a: Any, **_k: Any) -> Dict[str, Any]:
        return {"id": "oak_x", "cluster_permissions": []}

    monkeypatch.setattr(integrations, "_assert_granted_projects_exist", _projects_exist)
    monkeypatch.setattr(integrations, "_refuse_new_grants_to_unregistered_clusters", _nothing)
    monkeypatch.setattr(integrations, "_authorize_api_key", _key)
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: object())
    with pytest.raises(HTTPException) as caught:
        if door == "create":
            request = integrations.APIKeyCreateRequest(name="ci", app_name="ci", cluster_permissions=[_grant(MIXED, "*")])
            await integrations.create_api_key(request, None, _user(SELLER), db, None)
        else:
            await integrations.update_api_key_permissions("oak_x", [_grant(MIXED, "*")], None, _user(SELLER), None, db, [])
    assert caught.value.status_code == 403 and read == []


# ── rules a key writes (provisioning, the policy sync) ─────────────────────────────────────────

async def _policies(db: Any, **named: int) -> None:
    """One policy per organization naming `project` on 'mixed'."""
    for project, org_id in named.items():
        policy = TenantAccessPolicy(organization_id=org_id, name=f"p-{project}", created_by="system")
        db.add(policy)
        await db.commit()
        db.add(TenantPolicyRule(policy_id=policy.id, cluster_id=MIXED, project_name=project, permissions=["view"]))
        await db.commit()


async def test_a_project_another_party_names_is_refused() -> None:
    db = await _world(TenantAccessPolicy, TenantPolicyRule)
    await _policies(db, theirs=3)
    with pytest.raises(HTTPException) as caught:
        await api_key_grant_bound.refuse_a_project_another_party_holds(
            db, {"id": KEY, "created_by": SELLER}, MIXED, "theirs"
        )
    assert caught.value.status_code == 403


@pytest.mark.parametrize("project", ["fresh", "bought"], ids=["nobody-names-it", "the-keys-own-buyer-names-it"])
async def test_a_project_nobody_else_names_still_provisions(project: str) -> None:
    db = await _world(TenantAccessPolicy, TenantPolicyRule)
    await _policies(db, bought=2)
    await api_key_grant_bound.refuse_a_project_another_party_holds(db, {"id": KEY, "created_by": SELLER}, MIXED, project)


@pytest.mark.parametrize("project", ["*", "", "-x", "a b", "x" * 64])
def test_a_purchase_names_one_real_project(project: str) -> None:
    with pytest.raises(ValidationError):
        integrations.ProvisionTenantAccessRequest(organization_external_id="o", cluster_id=MIXED, project_name=project)


def test_a_synced_rule_must_sit_inside_the_keys_grant() -> None:
    key = {"cluster_permissions": [{"cluster_id": MIXED, "projects": ["own-web"]}, {"cluster_id": OWN, "projects": ["*"]}]}
    assert api_key_grant_bound.rule_outside_the_key_grant(key, MIXED, "own-web") is None
    assert api_key_grant_bound.rule_outside_the_key_grant(key, OWN, "anything") is None
    assert "not granted" in api_key_grant_bound.rule_outside_the_key_grant(key, MIXED, "theirs")
    assert "not granted" in api_key_grant_bound.rule_outside_the_key_grant(key, SOLO, "default")


def test_provisioning_and_the_sync_ask_who_names_the_project_before_writing() -> None:
    """Both write tenant rules; both ask before the first organization, member or rule row."""
    import inspect

    provision = inspect.getsource(integrations.provision_tenant_access)
    # once where the organization exists, once just before a new one is written
    assert provision.count("await refuse_a_project_another_party_holds(") == 2
    assert provision.index("await refuse_a_project_another_party_holds(") < provision.index("db.add(org)")
    assert provision.rindex("await refuse_a_project_another_party_holds(") < provision.index("org = Organization(")
    sync = inspect.getsource(integrations.sync_policies_from_external)
    assert sync.index("rule_outside_the_key_grant") < sync.index("refuse_a_project_another_party_holds")
    assert sync.index("refuse_a_project_another_party_holds") < sync.index("find org by external_id")


# ── an add-on installation is a key too ────────────────────────────────────────────────────────

@pytest.mark.parametrize("cluster,project", [(MIXED, "*"), (MIXED, "theirs")], ids=["wildcard", "another-partys-project"])
async def test_an_add_on_install_is_held_to_the_same_bound(scope, cluster: str, project: str) -> None:
    """A partner publishes an add-on asking for every project and installs it beside another party."""
    from app.services.extension_scope import assert_install_scope_within_partner

    db = await _world()
    grant = {"cluster_id": cluster, "projects": [project], "permissions": ["read"]}
    with pytest.raises(HTTPException) as caught:
        await assert_install_scope_within_partner(
            db, _user(SELLER), organization_id=1, grants=[grant],
            manifest_scope={"permission": "admin", "projects": "all"}, capabilities=[],
        )
    assert caught.value.status_code == 403 and f"{cluster}/{project}" in caught.value.detail


async def test_an_add_on_on_the_sellers_own_cluster_still_installs_with_a_wildcard(scope) -> None:
    from app.services.extension_scope import assert_install_scope_within_partner

    db = await _world()
    await assert_install_scope_within_partner(
        db, _user(SELLER), organization_id=1,
        grants=[{"cluster_id": OWN, "projects": ["*"], "permissions": ["read"]}],
        manifest_scope={"permission": "admin", "projects": "all"}, capabilities=[],
    )
