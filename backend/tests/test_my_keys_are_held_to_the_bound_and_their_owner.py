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
    (MIXED, "own-web", "standard"), (OWN, "anything", "standard"), (OWN, "default", "admin"),
], ids=["operator-key-inside-the-bound", "own-cluster", "server-key-on-the-sellers-own-cluster-after-a-sale"])
async def test_a_key_inside_the_bound_is_minted(scope, minting, node, project, level) -> None:
    db = await _world()
    with pytest.raises(HTTPException) as caught:
        await my_keys.create_key(_mint(node, project, level), minting["lxd"], None, _user(ME), db)
    assert caught.value.status_code == 404 and "not connected" in caught.value.detail
    assert minting["certificates"] == ["ci@orcastra"] and minting["clients"] == [node]


async def test_the_project_provisioned_is_the_one_authorized(scope) -> None:
    assert await lxd_key_bounds.project_a_new_key_names(MIXED, "  own-web ", "standard", None, _user(ME)) == "own-web"
    assert await lxd_key_bounds.project_a_new_key_names(OWN, " ", "standard", None, _user(ME)) == "default"


async def test_an_admin_mints_a_server_key_anywhere(scope, minting) -> None:
    db = await _world()
    with pytest.raises(HTTPException) as caught:
        await my_keys.create_key(_mint(SOLO, "default", "admin"), minting["lxd"], None, _user(ADMIN, "role_admin"), db)
    assert caught.value.status_code == 404 and minting["clients"] == [SOLO]
