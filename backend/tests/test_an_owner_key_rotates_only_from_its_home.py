"""A key that owns OrcaHub organizations hands its secret only to an Admin or its home's partners.

A secret rotation hands its caller a working secret. For a key that owns organizations, that secret
is their sync, member updates and provisioning. Minting the key is a fact about the past: a partner
who left the seller still matches created_by, provisioning stamps every buyer organization with the
key's minter as created_by, and OrcaHub makes every buyer's founder a partner of it. So a rotation
of such a key needs an Administrator or a current partner of the key's home organization, the one
its sales prove (cluster_parties.home_partner_keys), the key's creator included. Every caller but
an Administrator is also held to the key's cluster scope, and a non-creator to the project bound.

These run the real rotate handler against SQLite organization rows and an in-memory Vault.
"""

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List

import pytest
from fastapi import HTTPException

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.core.vault_client import VaultCasMismatch
from app.models.db_models import Organization, OrganizationCluster, OrganizationMember
from app.routers import integrations
from app.routers.integrations import SyncDataRequest
from tests._owner_key_world import RecordingLogger
from tests._sqlite_session import org_tables, sqlite_session

KEY = "oak_" + "c" * 32
REAL_AUTHORIZE = integrations._authorize_api_key
BASE = f"secret/data/{integrations.VAULT_INTEGRATIONS_PATH}/api_keys/{KEY}"
OWNER = AuthenticatedUser(user_id="p", email="partner@example.test", groups=["role_partner"])
OTHER = AuthenticatedUser(user_id="q", email="teammate@example.test", groups=["role_partner"])
ADMIN = AuthenticatedUser(user_id="a", email="ops@example.test", groups=["role_admin"])
FOUNDER = AuthenticatedUser(user_id="f", email="founder@example.test", groups=["role_partner"])


class _Vault:
    def __init__(self, docs: Dict[str, Dict[str, Any]]) -> None:
        self.docs = docs
        self.versions: Dict[str, int] = {path: 1 for path in docs}
        self.writes: List[str] = []

    def vault_read(self, path: str, use_cache: bool = True) -> Dict[str, Any]:
        if path not in self.docs:
            raise HTTPException(status_code=404, detail="not found")
        return dict(self.docs[path])

    def vault_read_versioned(self, path: str) -> tuple:
        return (dict(self.docs[path]) if path in self.docs else None), self.versions.get(path, 0)

    def vault_kv_put(self, path: str, data: Dict[str, Any], cas: Any = None) -> None:
        if cas is not None and cas != self.versions.get(path, 0):
            raise VaultCasMismatch(path)
        self.writes.append(path)
        self.docs[path] = dict(data)
        self.versions[path] = self.versions.get(path, 0) + 1


class _Cache:
    def __init__(self) -> None:
        self.store: Dict[tuple, Any] = {}

    def get(self, ns: str, key: str) -> Any:
        return self.store.get((ns, key))

    def set(self, ns: str, key: str, value: Any, ttl: int = 0) -> None:
        self.store[(ns, key)] = value

    def delete(self, *_a: Any, **_k: Any) -> None:
        return None

    def delete_pattern(self, *_a: Any, **_k: Any) -> None:
        return None


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SECRET_KEY", "unit-test-secret")
    old_secret, old_hash = integrations.generate_api_key_secret()
    metadata = {"id": KEY, "name": "conn", "app_name": "hub", "app_type": "orcahub", "is_active": True,
                "is_revoked": False, "created_at": "2026-09-01T00:00:00", "created_by": OWNER.email,
                "cluster_permissions": [{"cluster_id": "c1", "permissions": ["read"], "projects": ["*"]}]}
    vault = _Vault({f"{BASE}/metadata": metadata, f"{BASE}/secret": {"hash": old_hash, "raw": old_secret}})
    log = RecordingLogger()

    async def authorize(_key: str, _user: Any, _db: Any) -> Dict[str, Any]:
        return dict(metadata)

    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: vault)
    monkeypatch.setattr(integrations, "get_cache_manager", lambda: _Cache())
    monkeypatch.setattr(integrations, "_authorize_api_key", authorize)
    monkeypatch.setattr(integrations, "get_user_mappings_from_vault", lambda _v, _k: [])
    monkeypatch.setattr(integrations, "_schedule_last_used_update", lambda _k: None)
    monkeypatch.setattr(integrations, "activity_logger", log)
    integrations._key_cache.clear()
    yield {"old": old_secret, "vault": vault, "metadata": metadata, "log": log}
    integrations._key_cache.clear()


@pytest.fixture
def partner_scope():
    token = org_scope._allowed_clusters_var.set({"c1"})
    yield
    org_scope._allowed_clusters_var.reset(token)


def _stored(world: Dict[str, Any], **changes: Any) -> None:
    """Change the key as Vault holds it and as the authorization read returns it."""
    world["metadata"].update(changes)
    world["vault"].docs[f"{BASE}/metadata"].update(changes)


async def _valid(secret: str) -> bool:
    try:
        await integrations.validate_api_key(KEY, secret)
        return True
    except HTTPException as exc:
        assert exc.status_code == 401
        return False


async def _rotate(user: AuthenticatedUser = OWNER, db: Any = None) -> Any:
    return await integrations.rotate_api_key_secret(KEY, None, user, db)


def _orgs(*rows: tuple) -> Any:
    """Organization rows, (slug, owner key, partner email or None), on SQLite."""
    db = sqlite_session(*org_tables())
    for slug, key_id, partner in rows:
        db.add(Organization(name=slug.title(), slug=slug, source="orcahub", external_id=f"ext-{slug}",
                            api_key_id=key_id, created_by="system", is_active=True))
        db.sync.commit()
        if partner:
            org_id = db.sync.query(Organization.id).filter(Organization.slug == slug).scalar()
            db.add(OrganizationMember(organization_id=org_id, user_email=partner, role="partner"))
            db.sync.commit()
    return db


def _denied(world: Dict[str, Any]) -> List[tuple]:
    return [(r.get("result"), r.get("error_code")) for r in world["log"].calls
            if r["action"] == integrations.API_KEY_ROTATE]


async def _seller_with_buyers(*, minter_partners: Any = None, home_partners: tuple = ()) -> Any:
    """The seller's home (1) and two buyers provisioning made (2, 3) on KEY, all bound to c1.

    The successor runs the seller and registered c1 there, so the sales prove which organization is the
    key's home; each buyer is stamped with the key's minter as created_by, and OrcaHub made the founder
    of buyer 2 a partner of it. `minter_partners` adds a partner row for the minter: "home" for one who
    still runs the seller, "buyer" for one who left it and then bought from it, so that buyer 3 is
    theirs. `home_partners` adds other partners of the seller.
    """
    db = sqlite_session(*org_tables())
    db.add_all([
        Organization(id=1, name="Acme", slug="acme", source="orcahub", external_id="ext-acme", api_key_id=KEY,
                     created_by="system", is_active=True),
        Organization(id=2, name="Buyer", slug="buyer", source="orcahub", external_id="ext-buyer", api_key_id=KEY,
                     created_by=OWNER.email, is_active=True),
        Organization(id=3, name="M Co", slug="m-co", source="orcahub", external_id="ext-mco", api_key_id=KEY,
                     created_by=OWNER.email, is_active=True),
    ])
    await db.commit()
    rows: List[Any] = [
        OrganizationMember(organization_id=1, user_email="successor@example.test", role="partner"),
        OrganizationMember(organization_id=2, user_email=FOUNDER.email, role="partner"),
        OrganizationCluster(organization_id=1, cluster_id="c1", registered_by="successor@example.test"),
        OrganizationCluster(organization_id=2, cluster_id="c1"),
        OrganizationCluster(organization_id=3, cluster_id="c1"),
    ]
    if minter_partners:
        rows.append(OrganizationMember(organization_id={"home": 1, "buyer": 3}[minter_partners],
                                       user_email=OWNER.email, role="partner"))
    rows += [OrganizationMember(organization_id=1, user_email=email, role="partner") for email in home_partners]
    db.add_all(rows)
    await db.commit()
    return db


async def _rotate_in_scope(user: AuthenticatedUser, db: Any) -> Any:
    """Rotate with the caller's real cluster scope, as the org-scope dependency would set it."""
    token = org_scope._allowed_clusters_var.set(await org_scope._query_owned_clusters(user.email, db))
    try:
        return await _rotate(user, db)
    finally:
        org_scope._allowed_clusters_var.reset(token)


# note: the home rule

async def test_a_creator_who_left_cannot_take_the_secret_of_a_key_that_owns_organizations(
    world, partner_scope
) -> None:
    db = _orgs(("acme", KEY, "successor@example.test"))
    with pytest.raises(HTTPException) as caught:
        await _rotate(OWNER, db)
    assert caught.value.status_code == 403 and "administrator" in caught.value.detail
    assert world["vault"].writes == [] and await _valid(world["old"])
    assert _denied(world) == [("denied", "outside_the_home_organization")]


async def test_the_creator_who_is_a_partner_of_its_home_rotates(world, partner_scope) -> None:
    assert (await _rotate(OWNER, await _seller_with_buyers(minter_partners="home"))).api_key_id == KEY


async def test_before_its_first_sale_only_an_administrator_rotates_a_key_that_owns_organizations(
    world, partner_scope
) -> None:
    """The creator runs the seller's organization, but nobody has bought through the key yet.

    Only a sale proves which organization is the key's home. "Not sold yet" cannot be told apart from
    a sale whose bindings were deleted since, which would let a buyer left alone on the key inherit it,
    so until then the rotation is an administrator's, and the refusal says so.
    """
    db = _orgs(("acme", KEY, OWNER.email))
    with pytest.raises(HTTPException) as caught:
        await _rotate(OWNER, db)
    assert caught.value.status_code == 403
    assert caught.value.detail.endswith("Ask an Orcastra administrator to rotate it.")
    assert world["vault"].writes == [] and await _valid(world["old"])
    assert _denied(world) == [("denied", "outside_the_home_organization")]
    token = org_scope._allowed_clusters_var.set(None)
    try:
        assert (await _rotate(ADMIN, db)).api_key_id == KEY
    finally:
        org_scope._allowed_clusters_var.reset(token)


async def test_a_buyer_left_alone_on_the_key_lends_its_founder_nothing(world, monkeypatch) -> None:
    """The seller's organization was deleted and deregistering its cluster removed every sale binding.

    The buyer provisioning made is then the oldest organization the key's sync created, its founder is
    a partner of it, and nothing on the key looks sold any more. Its founder must not take the secret.
    """
    monkeypatch.setattr(integrations, "_authorize_api_key", REAL_AUTHORIZE)
    _stored(world, created_by="someone-who-left@example.test")
    db = sqlite_session(*org_tables())
    db.add(Organization(id=2, name="Buyer", slug="buyer", source="orcahub", external_id="ext-buyer", api_key_id=KEY,
                        created_by="system", is_active=True))
    await db.commit()
    db.add_all([
        OrganizationMember(organization_id=2, user_email=FOUNDER.email, role="partner"),
        OrganizationCluster(organization_id=2, cluster_id="c1", registered_by=FOUNDER.email),
    ])
    await db.commit()
    with pytest.raises(HTTPException) as caught:
        await _rotate_in_scope(FOUNDER, db)
    assert caught.value.status_code == 403
    assert world["vault"].writes == [] and await _valid(world["old"])
    assert _denied(world) == [("denied", "outside_the_home_organization")]


async def test_a_minter_who_left_the_seller_cannot_rotate_through_the_buyers_it_provisioned(
    world, monkeypatch
) -> None:
    """Provisioning stamps a buyer organization with the key's minter as created_by.

    That kept the minter "running" the buyer and kept its cluster in their scope (org_scope counts
    created_by), so after their partner row in the seller was removed they could still rotate the
    seller's owner key and take a working secret for every organization it owns.
    """
    monkeypatch.setattr(integrations, "_authorize_api_key", REAL_AUTHORIZE)
    _stored(world, created_by=OWNER.email)
    db = sqlite_session(*org_tables())
    db.add(Organization(id=1, name="Acme", slug="acme", source="orcahub", external_id="ext-acme", api_key_id=KEY,
                        created_by="system", is_active=True))
    db.add(Organization(id=2, name="Buyer", slug="buyer", source="orcahub", external_id="ext-buyer", api_key_id=KEY,
                        created_by=OWNER.email, is_active=True))
    await db.commit()
    db.add_all([
        OrganizationMember(organization_id=1, user_email="successor@example.test", role="partner"),
        OrganizationMember(organization_id=2, user_email="buyer@example.test", role="tenant"),
        OrganizationCluster(organization_id=1, cluster_id="c1", registered_by="successor@example.test"),
        OrganizationCluster(organization_id=2, cluster_id="c1"),
    ])
    await db.commit()
    allowed = await org_scope._query_owned_clusters(OWNER.email, db)
    assert allowed == {"c1"}, "precondition: the provisioned buyer keeps c1 in the minter's scope"
    token = org_scope._allowed_clusters_var.set(allowed)
    try:
        with pytest.raises(HTTPException) as caught:
            await _rotate(OWNER, db)
    finally:
        org_scope._allowed_clusters_var.reset(token)
    assert caught.value.status_code == 403 and "administrator" in caught.value.detail
    assert world["vault"].writes == [] and await _valid(world["old"])
    assert _denied(world) == [("denied", "outside_the_home_organization")]


async def test_a_minter_who_left_and_bought_from_the_seller_cannot_rotate_its_key(world, monkeypatch) -> None:
    """The departed minter's own buyer organization carries the seller's key and their partner row."""
    monkeypatch.setattr(integrations, "_authorize_api_key", REAL_AUTHORIZE)
    _stored(world, created_by=OWNER.email)
    db = await _seller_with_buyers(minter_partners="buyer")
    with pytest.raises(HTTPException) as caught:
        await _rotate_in_scope(OWNER, db)
    assert caught.value.status_code == 403 and "administrator" in caught.value.detail
    assert world["vault"].writes == [] and await _valid(world["old"])
    assert _denied(world) == [("denied", "outside_the_home_organization")]


async def test_a_minter_who_still_runs_the_seller_rotates_after_its_sales(world, monkeypatch) -> None:
    monkeypatch.setattr(integrations, "_authorize_api_key", REAL_AUTHORIZE)
    _stored(world, created_by=OWNER.email)
    answer = await _rotate_in_scope(OWNER, await _seller_with_buyers(minter_partners="home"))
    assert answer.api_key_id == KEY and await _valid(answer.api_key_secret)
    assert not await _valid(world["old"])


async def test_a_buyer_founder_who_did_not_mint_the_key_cannot_manage_it(world, monkeypatch) -> None:
    monkeypatch.setattr(integrations, "_authorize_api_key", REAL_AUTHORIZE)
    _stored(world, created_by=OWNER.email)
    with pytest.raises(HTTPException) as caught:
        await _rotate_in_scope(FOUNDER, await _seller_with_buyers(minter_partners="buyer"))
    assert caught.value.status_code == 404
    assert world["vault"].writes == [] and await _valid(world["old"])


async def test_a_buyer_that_is_the_oldest_synced_organization_lends_nothing_once_the_key_sold(
    world, monkeypatch
) -> None:
    """The seller's home was deleted and synced again, so a synced buyer is the key's oldest one.
    Only the proof of a sale names the home, and the buyer's binding is one provisioning made, so the
    minter who left and partners that buyer gets nothing."""
    monkeypatch.setattr(integrations, "_authorize_api_key", REAL_AUTHORIZE)
    _stored(world, created_by=OWNER.email)
    db = sqlite_session(*org_tables())
    db.add_all([
        Organization(id=1, name="M Co", slug="m-co", source="orcahub", external_id="ext-mco", api_key_id=KEY,
                     created_by="system", is_active=True),
        Organization(id=5, name="Acme", slug="acme", source="orcahub", external_id="ext-acme-2", api_key_id=KEY,
                     created_by="system", is_active=True),
    ])
    await db.commit()
    db.add_all([
        OrganizationMember(organization_id=1, user_email=OWNER.email, role="partner"),
        OrganizationMember(organization_id=5, user_email="successor@example.test", role="partner"),
        OrganizationCluster(organization_id=5, cluster_id="c1", registered_by="successor@example.test"),
        OrganizationCluster(organization_id=1, cluster_id="c1"),
    ])
    await db.commit()
    with pytest.raises(HTTPException) as caught:
        await _rotate_in_scope(OWNER, db)
    assert caught.value.status_code == 403
    assert world["vault"].writes == [] and await _valid(world["old"])


async def test_a_teammate_running_the_home_rotates(world, partner_scope, monkeypatch) -> None:
    async def nobody_else(_db: Any, _email: str, _clusters: set) -> set:
        return set()

    monkeypatch.setattr(integrations, "_clusters_held_by_another_party", nobody_else)
    db = await _seller_with_buyers(home_partners=(OTHER.email,))
    assert (await _rotate(OTHER, db)).api_key_id == KEY


async def test_an_admin_rotates_whatever_the_key_owns(world) -> None:
    db = _orgs(("acme", KEY, "successor@example.test"))
    assert (await _rotate(ADMIN, db)).api_key_id == KEY


async def test_ownership_that_cannot_be_read_rotates_nothing(world, partner_scope) -> None:
    class _Down:
        async def execute(self, *_a: Any, **_k: Any) -> Any:
            raise ConnectionError("database is down: postgresql://user:pw@db/app")

    with pytest.raises(HTTPException) as caught:
        await _rotate(OWNER, _Down())
    assert caught.value.status_code == 503 and "postgresql" not in caught.value.detail
    assert world["vault"].writes == []


async def test_a_key_that_owns_nothing_keeps_the_scope_rules(world, partner_scope) -> None:
    """No owned organization, no home rule: the creator rotates as before."""
    assert (await _rotate(OWNER, _orgs())).api_key_id == KEY
    assert _denied(world) == [("success", None)]


# note: the cluster scope, now for the creator too

async def test_a_creator_whose_scope_shrank_cannot_rotate_a_key_reaching_past_it(world, partner_scope) -> None:
    """Minting the key does not skip the cluster check: the secret reaches every cluster it grants."""
    _stored(world, created_by=OWNER.email,
            cluster_permissions=[{"cluster_id": "c1", "permissions": ["read"], "projects": ["*"]},
                                 {"cluster_id": "c9", "permissions": ["read", "write"], "projects": ["*"]}])
    with pytest.raises(HTTPException) as caught:
        await _rotate(OWNER, _orgs())
    assert caught.value.status_code == 403 and "c9" in caught.value.detail
    assert world["vault"].writes == [] and await _valid(world["old"])
    assert _denied(world) == [("denied", "cluster_out_of_scope")]


async def test_a_non_creator_rotation_is_held_to_the_grant_bound(world, partner_scope, monkeypatch) -> None:
    seen: List[str] = []

    async def bound(_db: Any, email: str, grants: Any, *_a: Any) -> None:
        seen.append(email)
        raise HTTPException(status_code=403, detail="Not permitted to grant these projects")

    monkeypatch.setattr(integrations, "_refuse_a_grant_past_the_project_bound", bound)
    _stored(world, created_by="ops@example.test")
    with pytest.raises(HTTPException) as caught:
        await _rotate(OTHER, _orgs())
    assert caught.value.status_code == 403 and seen == [OTHER.email]
    assert world["vault"].writes == []
    assert _denied(world) == [("denied", "project_out_of_bound")]


async def test_a_grant_widened_while_the_rotation_waited_is_judged(world, partner_scope, monkeypatch) -> None:
    """The key is read under its lock, so a grant edit that held the lock first is the one judged."""
    async def nobody_else(_db: Any, _email: str, _clusters: set) -> set:
        return set()

    monkeypatch.setattr(integrations, "_clusters_held_by_another_party", nobody_else)
    _stored(world, created_by="teammate-who-left@example.test")
    real_lock = integrations.serialize_key_ownership

    @asynccontextmanager
    async def _widened_first(session: Any, key_id: str) -> AsyncIterator[None]:
        async with real_lock(session, key_id):
            doc = world["vault"].docs[f"{BASE}/metadata"]
            doc["cluster_permissions"] = [*doc["cluster_permissions"],
                                          {"cluster_id": "c9", "permissions": ["read"], "projects": ["*"]}]
            yield

    monkeypatch.setattr(integrations, "serialize_key_ownership", _widened_first)
    with pytest.raises(HTTPException) as caught:
        await _rotate(OTHER, _orgs())
    assert caught.value.status_code == 403 and "c9" in caught.value.detail
    assert world["vault"].writes == [] and await _valid(world["old"])
    assert _denied(world) == [("denied", "cluster_out_of_scope")]


# note: the organizations the key owns stay with it, which is the reason rotation exists

async def test_rotation_keeps_the_id_and_the_organizations_it_owns(world, monkeypatch) -> None:
    db = sqlite_session(*org_tables())
    db.add(Organization(name="Own", slug="own", source="orcahub", external_id="ext-own", api_key_id=KEY,
                        created_by="system", is_active=True))
    await db.commit()
    answer = await _rotate(ADMIN, db)
    assert not await _valid(world["old"])
    validated = await integrations.validate_api_key(KEY, answer.api_key_secret)
    body = {"include_clusters": False, "include_projects": False,
            "organizations": [{"id": "ext-own", "name": "Own Renamed", "slug": "own"}]}
    synced = await integrations.sync_data(SyncDataRequest.model_validate(body), validated, None, db,
                                          answer.api_key_secret)
    assert synced.organizations_synced == 1
    org = db.sync.query(Organization).one()
    assert (org.api_key_id, org.name) == (KEY, "Own Renamed")
