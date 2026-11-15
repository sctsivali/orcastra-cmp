"""One bad policy in a policy sync fails alone, and the answer matches what was saved.

/external/sync-policies replaced a policy's rules, then read its assignments; a malformed expiry
raised after the rules were already replaced, the policy was reported as failed, and the single
commit at the end saved the replaced rules anyway. A duplicate policy name failed at the flush and
left the session unusable, so every later policy failed and the whole push was lost. Each policy now
runs in its own savepoint, an assignment expiry and every rule's permissions are checked before
anything is written, and the error text never carries a database message.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.db_models import TenantAccessPolicy, TenantPolicyRule
from tests.test_the_one_org_rule_holds_for_orcahubs_bodies_and_every_claim_path import (  # noqa: F401
    BUYER1,
    BUYER_LOGIN,
    SALE,
    hub,
    provision,
    provision_body,
    sale,
    world,
    world_db,
)

SYNC = "/api/v1/integrations/external/sync-policies"


def _policy(external_id: str, name: str, **extra):
    return {"external_id": external_id, "name": name, "org_external_id": BUYER1,
            "rules": [{"cluster_id": SALE, "project_name": "orc-p-1", "permissions": ["view"]}], **extra}


async def _rules(db):
    return sorted((await db.execute(select(TenantPolicyRule.project_name, TenantPolicyRule.permissions))).all())


async def test_a_malformed_expiry_leaves_the_policy_as_it_was(sale) -> None:  # noqa: F811
    db = world_db()
    assert (await provision(db, provision_body(BUYER1, [BUYER_LOGIN]))).status_code == 200
    before = await _rules(db)
    body = {"policies": [_policy(BUYER1, "auto:orc-p-1",
                                 rules=[{"cluster_id": SALE, "project_name": "orc-p-1", "permissions": ["view", "operate", "console"]}],
                                 assignments=[{"user_email": BUYER_LOGIN, "expires_at": "not-a-date"}])]}
    response = await provision(db, body, path=SYNC)
    assert response.status_code == 200, response.text
    assert response.json()["synced"] == 0 and "expires_at" in response.json()["errors"][0]
    assert await _rules(db) == before, "the rules were not replaced"


async def test_a_duplicate_name_fails_alone_and_the_rest_is_saved(sale) -> None:  # noqa: F811
    db = world_db()
    assert (await provision(db, provision_body(BUYER1, [BUYER_LOGIN]))).status_code == 200
    body = {"policies": [
        _policy("ext-a", "qa-dup"),
        _policy("ext-b", "qa-dup"),
        _policy("ext-c", "qa-after"),
    ]}
    response = await provision(db, body, path=SYNC)
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["synced"] == 2 and len(answer["errors"]) == 1 and "qa-dup" in answer["errors"][0]
    assert "IntegrityError" not in answer["errors"][0] and "UNIQUE" not in answer["errors"][0].upper()
    names = sorted((await db.execute(select(TenantAccessPolicy.name))).scalars().all())
    assert "qa-dup" in names and "qa-after" in names and names.count("qa-dup") == 1


async def test_an_unknown_permission_fails_that_policy_before_any_write(sale) -> None:  # noqa: F811
    db = world_db()
    assert (await provision(db, provision_body(BUYER1, [BUYER_LOGIN]))).status_code == 200
    before = await _rules(db)
    body = {"policies": [_policy(BUYER1, "auto:orc-p-1",
                                 rules=[{"cluster_id": SALE, "project_name": "orc-p-1", "permissions": ["veiw"]}])]}
    response = await provision(db, body, path=SYNC)
    assert response.json()["synced"] == 0 and "veiw" in response.json()["errors"][0]
    assert await _rules(db) == before
