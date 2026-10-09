"""Two-key enrollment, the owner-key read and an owner key's own removal, through the external routes.

An owner key presents another key's id and secret; when the secret verifies, the key could own the
organization and its minter is on the organization's side, it joins the owner set. A missing key and a
wrong secret answer the same sentence, repeated failures lock the (vouching key, new key) pair out, and
the routes are limited per vouching key and organization. The secret appears in no response, log line or
audit record.

Everything here is 404 while INTEGRATION_MULTI_OWNER_ENABLED is off, and validate reports the feature
only while it is on.
"""

import asyncio
import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
import pytest
from sqlalchemy import select

from app.core.vault_client import VaultClientError
from app.models.db_models import Organization, OrganizationCluster, OrganizationMember
from app.routers import integrations
from app.services import activity_logger as activity_logger_module
from app.services import owner_key_enrollment
from app.services.integration_key_ownership import OWNERSHIP_UNKNOWN_DETAIL
from app.services.organization_owners import MAX_OWNER_KEYS, add_owner, owner_key_ids
from tests._owner_invariants import assert_owner_invariants
from tests._owner_key_world import RecordingLogger, send
from tests._sqlite_session import org_tables, sqlite_session
from tests.test_a_key_that_owns_an_organization_is_not_revoked_or_deleted import Vault, path

OWNER, NEW, OTHER = "oak_owner", "oak_second", "oak_stranger"
MINTER, OUTSIDER = "partner@example.test", "someone-else@example.test"
EXT = "ext-acme"
NEW_SECRET = "oas_the_new_secret_never_logged"
OWNER_KEYS = "/api/v1/integrations/external/organizations/owner-keys"
VALIDATE = "/api/v1/integrations/validate"
PAST = "2026-01-01T00:00:00"
SETTLED = datetime.now(timezone.utc) - owner_key_enrollment.PARTNER_MIN_AGE - timedelta(hours=1)


def hashed(secret: str) -> str:
    return hmac.new(integrations._get_hmac_key(), secret.encode(), hashlib.sha256).hexdigest()


def metadata(key_id: str, *, created_by: str = MINTER, **over: Any) -> Dict[str, Any]:
    return {"id": key_id, "name": key_id, "app_name": "OrcaHub", "app_type": "orcahub", "created_by": created_by,
            "is_active": True, "is_revoked": False, "expires_at": None, "cluster_permissions": [], **over}


class _Cache:
    def __init__(self) -> None:
        self.cleared: List[Any] = []

    def delete_pattern(self, *args: Any) -> None:
        self.cleared.append(args)

    def delete(self, *args: Any) -> None:
        self.cleared.append(args)

    redis_client = None


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """acme-cloud is owned by OWNER alone. NEW and OTHER are active OrcaHub keys minted by its partner."""
    # note: the secret hash is keyed by SECRET_KEY, read on every call
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-hmac-only")
    owner_key_enrollment.reset_limits()
    db = sqlite_session(*org_tables())
    db.add(Organization(name="Acme", slug="acme-cloud", source="orcahub", external_id=EXT, api_key_id=OWNER,
                        created_by="system", is_active=True))
    db.sync.commit()
    org_id = db.sync.query(Organization.id).filter(Organization.slug == "acme-cloud").scalar()
    # note: a partner for longer than a day, so its keys are on the organization's side (party_check)
    db.add(OrganizationMember(organization_id=org_id, user_email=MINTER, role="partner", joined_at=SETTLED))
    db.sync.commit()
    vault = Vault(*(metadata(key_id) for key_id in (OWNER, NEW, OTHER)))
    for key_id in (OWNER, OTHER):
        vault.docs[path(key_id, "secret")] = {"hash": hashed(f"secret-{key_id}")}
    vault.docs[path(NEW, "secret")] = {"hash": hashed(NEW_SECRET)}
    cache = _Cache()
    records = RecordingLogger()
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: vault)
    monkeypatch.setattr(integrations, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(integrations, "evict_api_key_cache", lambda _k: None)
    monkeypatch.setattr(activity_logger_module, "get_activity_logger", lambda: records)
    monkeypatch.setattr(integrations, "multi_owner_enabled", lambda: True)
    return {"db": db, "org": org_id, "vault": vault, "cache": cache, "records": records}


async def call(world: Dict[str, Any], key_id: str, method: str, at: str, body: Any = None) -> httpx.Response:
    """A request authenticated as `key_id`, through the external router."""
    return await send(metadata(key_id), world["db"], body, method=method, path=at)


def enroll(new_key: str = NEW, secret: str = NEW_SECRET, ext: str = EXT) -> Dict[str, Any]:
    return {"organization_external_id": ext, "new_key_id": new_key, "new_key_secret": secret}


async def test_everything_is_404_while_the_flag_is_off(world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integrations, "multi_owner_enabled", lambda: False)
    assert (await call(world, OWNER, "GET", f"/api/v1/integrations/external/organizations/{EXT}/owner-keys")).status_code == 404
    assert (await call(world, OWNER, "POST", OWNER_KEYS, enroll())).status_code == 404
    assert (await call(world, OWNER, "DELETE", f"/api/v1/integrations/external/organizations/{EXT}/owner-keys/self")).status_code == 404
    assert integrations._integration_features() is None
    assert await owner_key_ids(world["db"], world["org"]) == [OWNER]
    monkeypatch.setattr(integrations, "multi_owner_enabled", lambda: True)
    assert integrations._integration_features() == ["organization_owner_keys.v1"]


async def test_validate_reports_the_feature_only_while_it_is_on(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    good = {"api_key_id": OWNER, "api_key_secret": f"secret-{OWNER}"}
    on = (await call(world, OWNER, "POST", VALIDATE, good)).json()
    assert (on["valid"], on["features"]) == (True, ["organization_owner_keys.v1"])
    wrong = (await call(world, OWNER, "POST", VALIDATE, {**good, "api_key_secret": "wrong"})).json()
    assert (wrong["valid"], wrong["features"]) == (False, None), "a refusal says nothing about the build"
    monkeypatch.setattr(integrations, "multi_owner_enabled", lambda: False)
    off = (await call(world, OWNER, "POST", VALIDATE, good)).json()
    assert (off["valid"], off["features"]) == (True, None)


async def test_the_read_answers_each_caller_only_what_it_may_know(world: Dict[str, Any]) -> None:
    at = f"/api/v1/integrations/external/organizations/{EXT}/owner-keys"
    assert (await call(world, OWNER, "GET", at)).json() == {
        "status": "owner", "primary_key_id": OWNER, "owner_key_ids": [OWNER]}
    assert (await call(world, OTHER, "GET", at)).json() == {
        "status": "not_owner", "primary_key_id": None, "owner_key_ids": None}
    missing = await call(world, OTHER, "GET", "/api/v1/integrations/external/organizations/nope/owner-keys")
    assert missing.json()["status"] == "unclaimed"
    assert world["records"].calls == [], "a read writes nothing"


async def test_an_owner_enrolls_a_key_it_holds_once(world: Dict[str, Any], caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    response = await call(world, OWNER, "POST", OWNER_KEYS, enroll())
    assert response.status_code == 200, response.text
    assert response.json() == {"organization_external_id": EXT, "primary_key_id": OWNER,
                               "owner_key_ids": [OWNER, NEW], "added": True}
    again = await call(world, OWNER, "POST", OWNER_KEYS, enroll())
    assert again.json()["added"] is False and again.json()["owner_key_ids"] == [OWNER, NEW]
    (record,) = world["records"].calls
    assert record["action"] == "integration.organization.owner.add"
    assert (record["details"]["owner_key_id"], record["details"]["proof_key_id"]) == (NEW, OWNER)
    assert (record["details"]["trigger"], record["details"]["partner_count"]) == ("enroll", 1)
    assert NEW_SECRET not in repr(world["records"].calls) and NEW_SECRET not in caplog.text
    assert NEW_SECRET not in response.text and NEW_SECRET not in again.text
    via = world["db"].sync.execute(select(Organization).where(Organization.id == world["org"])).scalar_one()
    assert via.api_key_id == OWNER, "enrollment never moves the primary"
    assert ("api_responses", "integrations:api_keys:list*") in world["cache"].cleared
    await assert_owner_invariants(world["db"])


async def test_the_new_key_can_then_read_and_leave(world: Dict[str, Any]) -> None:
    await call(world, OWNER, "POST", OWNER_KEYS, enroll())
    read = await call(world, NEW, "GET", f"/api/v1/integrations/external/organizations/{EXT}/owner-keys")
    assert read.json()["status"] == "owner"
    left = await call(world, OWNER, "DELETE", f"/api/v1/integrations/external/organizations/{EXT}/owner-keys/self")
    assert left.status_code == 200, left.text
    assert left.json() == {"removed": True, "primary_key_id": NEW, "owner_key_ids": [NEW]}
    last = await call(world, NEW, "DELETE", f"/api/v1/integrations/external/organizations/{EXT}/owner-keys/self")
    assert last.status_code == 409 and last.json()["detail"]["error"] == "last_owner"
    stranger = await call(world, OTHER, "DELETE", f"/api/v1/integrations/external/organizations/{EXT}/owner-keys/self")
    assert stranger.json() == {"removed": False, "primary_key_id": None, "owner_key_ids": None}
    assert [c["details"]["trigger"] for c in world["records"].calls] == ["enroll", "self"]
    await assert_owner_invariants(world["db"])


async def test_a_key_does_not_leave_while_its_co_owner_cannot_sync(world: Dict[str, Any]) -> None:
    await call(world, OWNER, "POST", OWNER_KEYS, enroll())
    world["vault"].docs[path(NEW)] = metadata(NEW, is_revoked=True, is_active=False)
    response = await call(world, OWNER, "DELETE", f"/api/v1/integrations/external/organizations/{EXT}/owner-keys/self")
    assert response.status_code == 409 and response.json()["detail"]["error"] == "last_owner"
    assert await owner_key_ids(world["db"], world["org"]) == [OWNER, NEW]


async def test_a_key_does_not_leave_while_its_co_owner_is_unreadable(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    await call(world, OWNER, "POST", OWNER_KEYS, enroll())
    real = world["vault"].vault_read

    def down(at: str, use_cache: bool = True) -> Dict[str, Any]:
        if at == path(NEW):
            raise VaultClientError("Vault read failed: 503 Server Error", status_code=503)
        return real(at, use_cache)

    monkeypatch.setattr(world["vault"], "vault_read", down)
    response = await call(world, OWNER, "DELETE", f"/api/v1/integrations/external/organizations/{EXT}/owner-keys/self")
    assert (response.status_code, response.json()["detail"]) == (503, OWNERSHIP_UNKNOWN_DETAIL)
    assert await owner_key_ids(world["db"], world["org"]) == [OWNER, NEW]


@pytest.mark.parametrize("new_key, secret", [(NEW, "wrong"), ("oak_missing", NEW_SECRET)], ids=["wrong_secret", "missing_key"])
async def test_a_missing_key_and_a_wrong_secret_read_the_same(world: Dict[str, Any], new_key: str, secret: str) -> None:
    response = await call(world, OWNER, "POST", OWNER_KEYS, enroll(new_key, secret))
    assert response.status_code == 403
    assert response.json()["detail"] == {"error": "new_key_unverified", "message": owner_key_enrollment.UNVERIFIED}
    assert await owner_key_ids(world["db"], world["org"]) == [OWNER]


async def test_repeated_failures_lock_the_pair_out(world: Dict[str, Any]) -> None:
    for _ in range(owner_key_enrollment.FAILURES_BEFORE_LOCKOUT):
        owner_key_enrollment.record_failure(OWNER, NEW)
    response = await call(world, OWNER, "POST", OWNER_KEYS, enroll())
    assert response.status_code == 403 and response.json()["detail"]["error"] == "new_key_unverified"
    assert not owner_key_enrollment.locked_out(OTHER, NEW), "another vouching key is not locked out with it"


async def test_enrollment_is_rate_limited(world: Dict[str, Any]) -> None:
    for _ in range(owner_key_enrollment.PER_MINUTE):
        await call(world, OWNER, "POST", OWNER_KEYS, enroll(secret="wrong"))
    owner_key_enrollment._local.pop(f"owner-enroll:fail:{OWNER}:{NEW}", None)
    response = await call(world, OWNER, "POST", OWNER_KEYS, enroll())
    assert response.status_code == 429 and response.json()["detail"]["error"] == "rate_limited"


async def test_wrong_secrets_through_the_route_lock_the_pair_out(world: Dict[str, Any]) -> None:
    for _ in range(owner_key_enrollment.FAILURES_BEFORE_LOCKOUT):
        wrong = await call(world, OWNER, "POST", OWNER_KEYS, enroll(secret="wrong"))
        assert wrong.json()["detail"]["error"] == "new_key_unverified"
    # note: the minute budgets are spent by now; the lockout is what this is about
    for bucket in (f"owner-enroll:key:{OWNER}:m", f"owner-enroll:org:{EXT}:m"):
        owner_key_enrollment._local.pop(bucket, None)
    right = await call(world, OWNER, "POST", OWNER_KEYS, enroll())
    assert right.status_code == 403 and right.json()["detail"]["error"] == "new_key_unverified"
    assert await owner_key_ids(world["db"], world["org"]) == [OWNER]


async def test_a_key_is_limited_even_where_it_owns_nothing(world: Dict[str, Any]) -> None:
    for _ in range(owner_key_enrollment.PER_MINUTE):
        assert (await call(world, OTHER, "POST", OWNER_KEYS, enroll())).json()["detail"]["error"] == "not_an_owner"
    response = await call(world, OTHER, "POST", OWNER_KEYS, enroll())
    assert response.status_code == 429 and response.json()["detail"]["error"] == "rate_limited"


async def test_an_organization_is_limited_across_its_owner_keys(world: Dict[str, Any]) -> None:
    """Two owner keys of one organization share its budget, though neither spends its own."""
    org = world["db"].sync.get(Organization, world["org"])
    assert await add_owner(world["db"], org, NEW, via="admin", by="admin@example.test")
    await world["db"].commit()
    third = enroll(OTHER, "wrong")
    for caller in [OWNER] * 3 + [NEW] * 2:
        assert (await call(world, caller, "POST", OWNER_KEYS, third)).json()["detail"]["error"] == "new_key_unverified"
    response = await call(world, NEW, "POST", OWNER_KEYS, third)
    assert response.status_code == 429 and response.json()["detail"]["error"] == "rate_limited"


async def test_a_key_cannot_vouch_for_itself_or_for_an_org_it_does_not_own(world: Dict[str, Any]) -> None:
    # note: bounded, so a regression fails here rather than hanging: without the check the route takes
    # the same key's in-process lock twice
    same = await asyncio.wait_for(call(world, OWNER, "POST", OWNER_KEYS, enroll(OWNER, f"secret-{OWNER}")), 10)
    assert same.status_code == 422 and same.json()["detail"]["error"] == "same_key"
    stranger = await call(world, OTHER, "POST", OWNER_KEYS, enroll())
    assert stranger.status_code == 403 and stranger.json()["detail"]["error"] == "not_an_owner"
    assert await owner_key_ids(world["db"], world["org"]) == [OWNER]


@pytest.mark.parametrize("over, code", [
    ({"is_revoked": True, "is_active": False}, "target_key_revoked"),
    ({"is_active": False}, "target_key_inactive"),
    ({"expires_at": PAST}, "target_key_expired"),
    ({"app_type": "automation"}, "target_key_not_orcahub"),
], ids=["revoked", "inactive", "expired", "not_orcahub"])
async def test_a_key_that_could_not_own_is_refused(world: Dict[str, Any], over: Dict[str, Any], code: str) -> None:
    world["vault"].docs[path(NEW)] = metadata(NEW, **over)
    response = await call(world, OWNER, "POST", OWNER_KEYS, enroll())
    assert response.status_code == 409 and response.json()["detail"]["error"] == code
    assert await owner_key_ids(world["db"], world["org"]) == [OWNER]


@pytest.mark.parametrize("over", [{"is_revoked": True, "is_active": False}, {"app_type": "automation"}],
                         ids=["revoked", "not_orcahub"])
async def test_a_vouching_key_that_can_no_longer_own_is_refused(world: Dict[str, Any], over: Dict[str, Any]) -> None:
    """The key is read again in the handler, uncached: a revoke that landed after it authenticated counts."""
    world["vault"].docs[path(OWNER)] = metadata(OWNER, **over)
    response = await call(world, OWNER, "POST", OWNER_KEYS, enroll())
    assert response.status_code == 403 and response.json()["detail"]["error"] == "not_an_owner"
    assert await owner_key_ids(world["db"], world["org"]) == [OWNER]


async def test_an_unreadable_vouching_key_is_503(world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    real = world["vault"].vault_read

    def down(at: str, use_cache: bool = True) -> Dict[str, Any]:
        if at == path(OWNER):
            raise VaultClientError("Vault read failed: 503 Server Error", status_code=503)
        return real(at, use_cache)

    monkeypatch.setattr(world["vault"], "vault_read", down)
    response = await call(world, OWNER, "POST", OWNER_KEYS, enroll())
    assert (response.status_code, response.json()["detail"]) == (503, owner_key_enrollment.UNREADABLE)


def minter_joined(world: Dict[str, Any], when: Optional[datetime]) -> None:
    """MINTER's partner row in acme-cloud joined `when`, or is gone (None)."""
    member = world["db"].sync.query(OrganizationMember).filter_by(user_email=MINTER).one()
    if when is None:
        world["db"].sync.delete(member)
    else:
        member.joined_at = when
    world["db"].sync.commit()


async def test_a_key_minted_outside_the_organization_is_refused(world: Dict[str, Any]) -> None:
    world["vault"].docs[path(NEW)] = metadata(NEW, created_by=OUTSIDER)
    world["vault"].docs[path(OWNER)] = metadata(OWNER, created_by="old-minter@example.test")
    response = await call(world, OWNER, "POST", OWNER_KEYS, enroll())
    assert response.status_code == 403 and response.json()["detail"]["error"] == "new_key_outside_organization"
    # a key minted by a partner of the organization is on its side once the partner row is a day old
    world["vault"].docs[path(NEW)] = metadata(NEW, created_by=MINTER)
    minter_joined(world, datetime.now(timezone.utc))
    fresh = await call(world, OWNER, "POST", OWNER_KEYS, enroll())
    assert fresh.status_code == 403 and fresh.json()["detail"]["error"] == "new_key_outside_organization"
    minter_joined(world, datetime.now(timezone.utc) - owner_key_enrollment.PARTNER_MIN_AGE - timedelta(minutes=1))
    assert (await call(world, OWNER, "POST", OWNER_KEYS, enroll())).json()["added"] is True


@pytest.mark.parametrize("left", ["gone", "written_back_today"])
async def test_a_minter_who_left_cannot_enroll_a_key_of_its_own(world: Dict[str, Any], left: str) -> None:
    """Both keys are MINTER's, but MINTER no longer runs acme-cloud: having minted the vouching key is not enough.

    An owner key writes the organization's members, so a minter who left could also push itself back as a
    partner. Such a row is fresh, and a fresh row does not count for a day.
    """
    minter_joined(world, None if left == "gone" else datetime.now(timezone.utc))
    response = await call(world, OWNER, "POST", OWNER_KEYS, enroll())
    assert response.status_code == 403 and response.json()["detail"]["error"] == "new_key_outside_organization"
    assert await owner_key_ids(world["db"], world["org"]) == [OWNER]


@pytest.fixture
def buyer(world: Dict[str, Any]) -> Dict[str, Any]:
    """OWNER sold on a cluster of acme-cloud, its home, to buyer-co, which provisioning created for MINTER."""
    db = world["db"]
    db.add(Organization(name="Buyer", slug="buyer-co", source="orcahub", external_id="ext-buyer", api_key_id=OWNER,
                        created_by=MINTER, is_active=True))
    db.sync.commit()
    buyer_id = db.sync.query(Organization.id).filter(Organization.slug == "buyer-co").scalar()
    db.add(OrganizationCluster(organization_id=world["org"], cluster_id="c-sold", registered_by=MINTER))
    db.sync.commit()
    db.add(OrganizationCluster(organization_id=buyer_id, cluster_id="c-sold", registered_by=None))
    db.sync.commit()
    return {**world, "buyer": buyer_id}


async def test_the_minter_enrolls_into_a_buyer_while_it_runs_the_home(buyer: Dict[str, Any]) -> None:
    """MINTER is no member of buyer-co: what counts is a settled partner row in the vouching key's home."""
    response = await call(buyer, OWNER, "POST", OWNER_KEYS, enroll(ext="ext-buyer"))
    assert response.status_code == 200 and response.json()["added"] is True, response.text
    assert await owner_key_ids(buyer["db"], buyer["buyer"]) == [OWNER, NEW]


@pytest.mark.parametrize("change", ["home_row_fresh", "no_sale"])
async def test_the_minter_does_not_enroll_into_a_buyer_without_a_settled_home(buyer: Dict[str, Any], change: str) -> None:
    db = buyer["db"]
    if change == "home_row_fresh":
        minter_joined(buyer, datetime.now(timezone.utc))
    else:
        db.sync.query(OrganizationCluster).filter(OrganizationCluster.organization_id == buyer["buyer"]).delete()
        db.sync.commit()
    response = await call(buyer, OWNER, "POST", OWNER_KEYS, enroll(ext="ext-buyer"))
    assert response.status_code == 403 and response.json()["detail"]["error"] == "new_key_outside_organization"
    assert await owner_key_ids(db, buyer["buyer"]) == [OWNER]


async def test_a_failed_partner_count_keeps_the_audit_record(world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    async def down(_db: Any, _org: Any) -> List[str]:
        raise RuntimeError("connection lost")

    monkeypatch.setattr(owner_key_enrollment, "partner_emails", down)
    response = await call(world, OWNER, "POST", OWNER_KEYS, enroll())
    assert response.status_code == 200 and response.json()["added"] is True
    (record,) = world["records"].calls
    assert (record["action"], record["details"]["partner_count"]) == ("integration.organization.owner.add", None)


async def test_the_set_cap_holds(world: Dict[str, Any]) -> None:
    org = world["db"].sync.get(Organization, world["org"])
    for n in range(MAX_OWNER_KEYS - 1):
        await add_owner(world["db"], org, f"oak_filler_{n}", via="admin", by="admin@example.test")
    await world["db"].commit()
    response = await call(world, OWNER, "POST", OWNER_KEYS, enroll())
    assert response.status_code == 409 and response.json()["detail"]["error"] == "owner_set_full"


async def test_two_enrollments_of_the_same_key_add_it_once(world: Dict[str, Any]) -> None:
    first, second = await asyncio.gather(
        call(world, OWNER, "POST", OWNER_KEYS, enroll()),
        call(world, OWNER, "POST", OWNER_KEYS, enroll()),
    )
    assert sorted([first.json()["added"], second.json()["added"]]) == [False, True]
    assert await owner_key_ids(world["db"], world["org"]) == [OWNER, NEW]
