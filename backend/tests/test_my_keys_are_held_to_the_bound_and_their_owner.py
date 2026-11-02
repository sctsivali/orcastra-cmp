"""An LXD client key is minted inside the caller's bound.

A My Keys record is an LXD client certificate with its private key. Minting checked the cluster
only, so a partner held to a project bound could mint an operator key for another organization's
project, or a server key on a cluster another party holds. The party is read from real rows on
SQLite; certificate generation and the LXD client are recorders, so a refusal is proven to land
before a certificate exists. Owner checks on download and delete live in
test_a_partner_cannot_download_or_delete_another_owners_key.py.
"""

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.routers import my_keys
from app.services import lxd_key_bounds
from app.models.db_models import TenantAccessPolicy, TenantPolicyRule
from tests.test_whole_cluster_state_needs_the_whole_cluster import MIXED, OWN, SELLER, SOLO, _world

ME = SELLER
ADMIN = "ops@example.test"


def _user(email: str, group: str = "role_partner") -> AuthenticatedUser:
    return AuthenticatedUser(user_id=f"{group}:{email}", email=email, groups=[group])


@pytest.fixture
def scope():
    clusters = org_scope._allowed_clusters_var.set({OWN, MIXED, SOLO})
    bound = org_scope._visible_projects_var.set({MIXED: {"default", "own-web"}})
    yield
    org_scope._visible_projects_var.reset(bound)
    org_scope._allowed_clusters_var.reset(clusters)


# ── minting ────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def minting(monkeypatch: pytest.MonkeyPatch) -> Dict[str, List[Any]]:
    """Certificate generation and the LXD client, recorded. The client answers 'not connected'."""
    seen: Dict[str, List[Any]] = {"certificates": [], "clients": []}

    def _certificate(common_name: str, expire_days: int) -> Any:
        seen["certificates"].append(common_name)
        return b"CERT", b"PRIVATE", "f" * 64

    monkeypatch.setattr(my_keys, "generate_certificate", _certificate)
    monkeypatch.setattr(my_keys, "get_vault_keys_client", lambda: object())
    seen["lxd"] = SimpleNamespace(connection_manager=SimpleNamespace(
        get_client=lambda node: seen["clients"].append(node)))
    return seen


def _mint(node: str, project: str, level: str) -> my_keys.KeyCreateRequest:
    return my_keys.KeyCreateRequest(name="ci", node=node, project=project, auth_level=level, expire_days=30)


@pytest.mark.parametrize("node,project,level", [
    (MIXED, "foreign", "standard"), (MIXED, "default", "admin"), (SOLO, "default", "admin"),
], ids=["operator-key-for-another-orgs-project", "server-key-where-another-party-holds",
        "server-key-where-another-party-holds-alone"])
async def test_a_key_past_the_bound_is_refused_before_a_certificate_exists(scope, minting, node, project, level) -> None:
    db = await _world()
    with pytest.raises(HTTPException) as caught:
        await my_keys.create_key(_mint(node, project, level), minting["lxd"], None, _user(ME), db)
    assert caught.value.status_code == 403
    assert minting["certificates"] == [] and minting["clients"] == []


@pytest.mark.parametrize("node,project,level", [
    (OWN, "anything", "standard"), (OWN, "default", "admin"),
], ids=["own-cluster", "server-key-on-the-sellers-own-cluster-after-a-sale"])
async def test_a_key_inside_the_bound_is_minted(scope, minting, node, project, level) -> None:
    db = await _world()
    with pytest.raises(HTTPException) as caught:
        await my_keys.create_key(_mint(node, project, level), minting["lxd"], None, _user(ME), db)
    assert caught.value.status_code == 404 and "not connected" in caught.value.detail
    assert minting["certificates"] == ["ci@orcastra"] and minting["clients"] == [node]


async def test_the_project_provisioned_is_the_one_authorized(scope) -> None:
    db = await _world()
    assert (await lxd_key_bounds.authorize_new_key(OWN, " ", "standard", db, _user(ME))).project == "default"


async def test_an_admin_mints_a_server_key_anywhere(scope, minting) -> None:
    db = await _world()
    with pytest.raises(HTTPException) as caught:
        await my_keys.create_key(_mint(SOLO, "default", "admin"), minting["lxd"], None, _user(ADMIN, "role_admin"), db)
    assert caught.value.status_code == 404 and minting["clients"] == [SOLO]



# ── a cluster another party holds: a direct LXD key bypasses the dashboard's write policy ──────

async def _named(db: Any, org_id: int, project: str, cluster: str = MIXED) -> None:
    policy = TenantAccessPolicy(organization_id=org_id, name=f"p-{project}", created_by=ME)
    db.add(policy)
    await db.commit()
    db.add(TenantPolicyRule(policy_id=policy.id, cluster_id=cluster, project_name=project, permissions=["view"]))
    await db.commit()


@pytest.mark.parametrize("node,project", [
    (MIXED, "default"), (SOLO, "default"), (MIXED, "own-web"),
], ids=["default-beside-another-party", "default-where-another-party-holds-alone",
        "a-visible-project-no-own-policy-names"])
async def test_a_standard_key_on_another_partys_cluster_needs_a_project_of_ones_own(
    scope, minting, node: str, project: str
) -> None:
    db = await _world(TenantAccessPolicy, TenantPolicyRule)
    with pytest.raises(HTTPException) as caught:
        await my_keys.create_key(_mint(node, project, "standard"), minting["lxd"], None, _user(ME), db)
    assert caught.value.status_code == 403
    assert minting["certificates"] == [] and minting["clients"] == []


async def test_a_project_ones_own_policies_name_needs_lxd_to_isolate_it(scope) -> None:
    db = await _world(TenantAccessPolicy, TenantPolicyRule)
    await _named(db, 1, "own-web")
    target = await lxd_key_bounds.authorize_new_key(MIXED, "own-web", "standard", db, _user(ME))
    assert target.project == "own-web" and target.isolated_project_required

    def client(config: Dict[str, str]) -> Any:
        return SimpleNamespace(projects=SimpleNamespace(get=lambda _name: SimpleNamespace(config=config)))

    with pytest.raises(HTTPException) as caught:
        lxd_key_bounds.refuse_an_unisolated_project(client({}), MIXED, "own-web")
    assert caught.value.status_code == 403 and "config.restricted" in caught.value.detail
    isolated = {"restricted": "true", "features.images": "true", "features.networks": "true",
                "features.profiles": "true", "features.storage.volumes": "true"}
    lxd_key_bounds.refuse_an_unisolated_project(client(isolated), MIXED, "own-web")


async def test_the_sellers_own_cluster_after_a_sale_needs_no_isolation_proof(scope) -> None:
    db = await _world(TenantAccessPolicy, TenantPolicyRule)
    assert not (await lxd_key_bounds.authorize_new_key(OWN, "default", "standard", db, _user(ME))).isolated_project_required


async def test_no_direct_key_on_a_cluster_reached_only_by_purchase(scope, minting) -> None:
    """LXD knows nothing of a paid term, so a key minted during it would outlive it."""
    db = await _world(TenantAccessPolicy, TenantPolicyRule)
    token = org_scope._purchase_only_clusters_var.set(frozenset({OWN}))
    try:
        for level, project in (("standard", "default"), ("admin", "default")):
            with pytest.raises(HTTPException) as caught:
                await my_keys.create_key(_mint(OWN, project, level), minting["lxd"], None, _user(ME), db)
            assert caught.value.status_code == 403 and "purchase" in caught.value.detail
    finally:
        org_scope._purchase_only_clusters_var.reset(token)
    assert minting["certificates"] == [] and minting["clients"] == []


@pytest.mark.parametrize("override", [
    {"restricted.containers.lowlevel": "allow"}, {"restricted.containers.privilege": "allow"},
    {"restricted.devices.disk": "allow"}, {"restricted.devices.disk.paths": "/"},
    {"restricted.idmap.uid": "0-65535"}, {"restricted.devices.unix-char": "allow"},
], ids=lambda o: next(iter(o)))
def test_a_restricted_project_with_a_host_reaching_override_is_not_isolated(override: Dict[str, str]) -> None:
    isolated = {"restricted": "true", "features.images": "true", "features.networks": "true",
                "features.profiles": "true", "features.storage.volumes": "true"}
    client = SimpleNamespace(projects=SimpleNamespace(get=lambda _n: SimpleNamespace(config={**isolated, **override})))
    with pytest.raises(HTTPException) as caught:
        lxd_key_bounds.refuse_an_unisolated_project(client, MIXED, "own-web")
    assert caught.value.status_code == 403 and next(iter(override)) in caught.value.detail
