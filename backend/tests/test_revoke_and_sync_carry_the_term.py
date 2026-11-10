"""The revoke is audited, and the policy sync carries the paid term and keeps OrcaHub's expiry current.

revoke-tenant-access writes an audit record, so an operator can see when a buyer's access ended.
The policy sync updates the expiry of an existing assignment as well as a new one, and stores it as
an aware UTC value in the timestamptz column.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest
from sqlalchemy import select

from app.models.db_models import TenantAccessPolicy, TenantPolicyAssignment
from app.routers import integrations
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

NOW = datetime.now(timezone.utc)
REVOKE = "/api/v1/integrations/revoke-tenant-access"
SYNC = "/api/v1/integrations/external/sync-policies"


@pytest.fixture
def audits(monkeypatch) -> List[Dict[str, Any]]:
    recorded: List[Dict[str, Any]] = []
    monkeypatch.setattr(integrations.activity_logger, "log_activity", lambda **kw: recorded.append(kw))
    return recorded


def _aware(value):
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


async def test_a_revoke_leaves_one_audit_record_with_its_reason(sale, audits) -> None:
    db = world_db()
    assert (await provision(db, provision_body(BUYER1, [BUYER_LOGIN]))).status_code == 200
    audits.clear()

    body = {"organization_external_id": BUYER1, "cluster_id": SALE, "project_name": "orc-p-1", "reason": "term_ended"}
    response = await provision(db, body, path=REVOKE)

    assert response.status_code == 200, response.text
    assert response.json()["policy_deleted"] is True
    (record,) = [a for a in audits if a["action"] == "tenant.access.revoke"]
    assert record["details"]["reason"] == "term_ended"
    assert record["details"]["policy_deleted"] is True
    assert BUYER_LOGIN not in str(record), "the trail counts users, it does not copy their addresses"


async def test_a_revoke_reason_is_a_short_token_not_free_text(sale) -> None:
    db = world_db()
    body = {"organization_external_id": BUYER1, "cluster_id": SALE, "project_name": "orc-p-1",
            "reason": "<script>alert(1)</script>"}
    assert (await provision(db, body, path=REVOKE)).status_code == 422


async def test_the_sync_records_the_term_and_updates_orcahubs_expiry(sale) -> None:
    db = world_db()
    assert (await provision(db, provision_body(BUYER1, [BUYER_LOGIN]))).status_code == 200
    expiry = (NOW + timedelta(days=30)).replace(microsecond=0)
    body = {"policies": [{
        "external_id": BUYER1, "name": "auto:orc-p-1", "org_external_id": BUYER1,
        "rules": [{"cluster_id": SALE, "project_name": "orc-p-1", "permissions": ["view"]}],
        "assignments": [{"user_email": BUYER_LOGIN, "expires_at": expiry.strftime("%Y-%m-%dT%H:%M:%SZ")}],
        "term_ended_at": (NOW - timedelta(days=1)).isoformat(),
        "access_ends_at": expiry.isoformat(),
    }]}

    response = await provision(db, body, path=SYNC)

    assert response.status_code == 200, response.text
    assert response.json()["errors"] == []
    policy = (await db.execute(select(TenantAccessPolicy))).scalars().one()
    assert _aware(policy.access_ends_at) == expiry
    assignment = (await db.execute(select(TenantPolicyAssignment))).scalars().one()
    assert _aware(assignment.expires_at) == expiry, "an existing OrcaHub assignment follows OrcaHub's expiry"


async def test_a_sync_with_an_impossible_term_skips_that_policy_only(sale) -> None:
    db = world_db()
    assert (await provision(db, provision_body(BUYER1, [BUYER_LOGIN]))).status_code == 200
    body = {"policies": [{
        "external_id": BUYER1, "name": "auto:orc-p-1", "org_external_id": BUYER1,
        "rules": [{"cluster_id": SALE, "project_name": "orc-p-1", "permissions": ["view"]}],
        "term_ended_at": NOW.isoformat(), "access_ends_at": (NOW - timedelta(days=1)).isoformat(),
    }]}

    response = await provision(db, body, path=SYNC)

    assert response.status_code == 200
    assert response.json()["synced"] == 0 and "access_ends_at" in response.json()["errors"][0]
    policy = (await db.execute(select(TenantAccessPolicy))).scalars().one()
    assert policy.term_ended_at is None, "a refused term touches nothing"


def test_an_expiry_without_an_offset_is_read_as_utc() -> None:
    assert integrations._assignment_expiry("2026-12-01T00:00:00") == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert integrations._assignment_expiry("2026-12-01T07:00:00+07:00") == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert integrations._assignment_expiry(None) is None
