"""provision-tenant-access records the paid term OrcaHub sends, and refuses one that cannot be true.

OrcaHub narrows a buyer's access at the end of a paid term by provisioning again with fewer
permissions. Orcastra now also takes the dates of that term: `term_ended_at` (view only from then)
and `access_ends_at` (nothing from then). An absent field keeps what is stored, null clears it, an
older `as_of` is refused, and every refusal happens before an organization or member is written.
Each real change is audited; a replay that changes nothing is not.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest
from sqlalchemy import select

from app.models.db_models import Organization, TenantAccessPolicy
from app.routers import integrations
from tests.test_the_one_org_rule_holds_for_orcahubs_bodies_and_every_claim_path import (  # noqa: F401
    BUYER1,
    BUYER_LOGIN,
    hub,
    provision,
    provision_body,
    sale,
    world,
    world_db,
)

NOW = datetime.now(timezone.utc)
TERM = (NOW - timedelta(days=1)).isoformat()
ENDS = (NOW + timedelta(days=29)).isoformat()


@pytest.fixture
def audits(monkeypatch) -> List[Dict[str, Any]]:
    recorded: List[Dict[str, Any]] = []
    monkeypatch.setattr(integrations.activity_logger, "log_activity", lambda **kw: recorded.append(kw))
    return recorded


async def _policy(db) -> TenantAccessPolicy:
    return (await db.execute(select(TenantAccessPolicy))).scalars().one()


def _aware(value):
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


async def test_a_provision_with_dates_stores_them_and_answers_the_phase(sale, audits) -> None:
    db = world_db()
    response = await provision(db, provision_body(BUYER1, [BUYER_LOGIN], term_ended_at=TERM, access_ends_at=ENDS))
    assert response.status_code == 200, response.text
    assert response.json()["phase"] == "read_only"
    policy = await _policy(db)
    assert _aware(policy.term_ended_at) == datetime.fromisoformat(TERM)
    assert _aware(policy.access_ends_at) == datetime.fromisoformat(ENDS)
    assert [a["action"] for a in audits if a["action"].startswith("tenant.access")] == ["tenant.access.provision"]


async def test_absent_keeps_null_clears_and_a_replay_writes_no_record(sale, audits) -> None:
    db = world_db()
    await provision(db, provision_body(BUYER1, [BUYER_LOGIN], term_ended_at=TERM, access_ends_at=ENDS))
    audits.clear()

    replay = await provision(db, provision_body(BUYER1, [BUYER_LOGIN]))
    assert replay.status_code == 200
    assert _aware((await _policy(db)).term_ended_at) is not None, "an absent field keeps the stored date"
    assert not [a for a in audits if a["action"].startswith("tenant.access")], "nothing changed, nothing recorded"

    renewed = await provision(db, provision_body(BUYER1, [BUYER_LOGIN], term_ended_at=None, access_ends_at=None))
    assert renewed.status_code == 200 and renewed.json()["phase"] == "active"
    policy = await _policy(db)
    assert policy.term_ended_at is None and policy.access_ends_at is None
    (record,) = [a for a in audits if a["action"].startswith("tenant.access")]
    assert record["action"] == "tenant.access.update"
    assert record["details"]["term_before"]["term_ended_at"] is not None
    assert record["details"]["term_after"] == {"term_ended_at": None, "access_ends_at": None}


async def test_the_downgrade_is_recorded_as_one(sale, audits) -> None:
    db = world_db()
    await provision(db, provision_body(BUYER1, [BUYER_LOGIN]))
    audits.clear()
    narrowed = await provision(db, provision_body(BUYER1, [BUYER_LOGIN], permissions=["view", "console"]))
    assert narrowed.status_code == 200 and narrowed.json()["phase"] == "read_only"
    (record,) = [a for a in audits if a["action"].startswith("tenant.access")]
    assert record["details"]["downgrade"] is True


@pytest.mark.parametrize(
    "dates, status",
    [
        ({"term_ended_at": "2026-11-20T00:00:00"}, 422),
        ({"term_ended_at": ENDS, "access_ends_at": TERM}, 422),
        ({"access_ends_at": (NOW + timedelta(days=4000)).isoformat()}, 422),
        ({"permissions": ["view", "root"]}, 422),
    ],
    ids=["naive", "access-before-term", "too-far", "unknown-permission"],
)
async def test_an_impossible_body_writes_nothing(sale, dates, status) -> None:
    db = world_db()
    response = await provision(db, provision_body(BUYER1, [BUYER_LOGIN], **dates))
    assert response.status_code == status, response.text
    assert (await db.execute(select(Organization))).scalars().all() == [], "refused before the org was written"


async def test_an_older_decision_arriving_late_is_refused_before_any_write(sale) -> None:
    db = world_db()
    later = NOW.isoformat()
    earlier = (NOW - timedelta(minutes=10)).isoformat()
    assert (await provision(db, provision_body(BUYER1, [BUYER_LOGIN], term_ended_at=TERM, as_of=later))).status_code == 200

    stale = await provision(db, provision_body(BUYER1, [BUYER_LOGIN], term_ended_at=None, as_of=earlier))

    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "stale_provision"
    assert _aware((await _policy(db)).term_ended_at) == datetime.fromisoformat(TERM)
