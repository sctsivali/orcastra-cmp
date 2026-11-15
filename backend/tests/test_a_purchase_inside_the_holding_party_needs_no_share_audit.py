"""Provisioning a buyer onto a cluster its own seller's party holds is checked, not audited whole.

Every purchase binds the buyer's organization to the seller's cluster. The shared-cluster audit
guards the moment a cluster gains another holder; it reads the whole daemon inventory and refuses
unless every project is restricted and isolated, which a stock LXD `default` project is not. Run on
each purchase, it refused every new buyer on a partner's cluster with 409, although the buyer
organization is created by the provisioning key's minter and so joins that minter's party
(services/cluster_parties.py), and every buyer-side actor stays held to the shared-cluster write
policy whatever the audit said. Inside the party the purchase is checked for host reach in what
the buyer reaches (test_a_purchase_is_checked_for_host_reach_in_what_the_buyer_reaches); the whole
audit still runs when the cluster is held outside the provisioning key's party, when the key's
minter is unknown, when the organization is not the key's own, or when no project is named.
"""

from typing import Any, List, Optional

import pytest

from app.models.db_models import Organization, OrganizationCluster
from app.routers import integrations
from app.services.shared_cluster_audit import SharedClusterGuardError, SharedClusterAuditReport
from tests._sqlite_session import org_tables, sqlite_session

MINTER = "seller-admin@example.test"
OTHER = "other-seller@example.test"


def _refusal() -> SharedClusterGuardError:
    report = SharedClusterAuditReport(
        cluster_id="c1", safe=False, complete=True,
        projects_checked=1, profiles_checked=1, instances_checked=1, findings=(),
    )
    return SharedClusterGuardError(
        "cluster_not_safe_to_share",
        "Unsafe or non-isolated resources must be remediated before this cluster can be shared.",
        report,
        status_code=409,
    )


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    """Every whole-cluster audit asked for, answered as on a stock cluster: not safe to share."""
    calls: List[Any] = []

    async def unsafe(db, cluster_id, **kwargs):
        calls.append((cluster_id, kwargs))
        raise _refusal()

    monkeypatch.setattr(integrations, "assert_cluster_safe_to_share", unsafe)
    return calls


@pytest.fixture
def reach_checks(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    """Every purchase reach check asked for, answered safe unless a test says otherwise."""
    calls: List[Any] = []

    async def safe(cluster_id, project_name):
        calls.append((cluster_id, project_name))

    monkeypatch.setattr(integrations, "assert_purchase_reach_safe", safe)
    return calls


async def _world(*, seller_created_by: str = MINTER, extra: Optional[list] = None) -> Any:
    db = sqlite_session(*org_tables())
    db.add_all([
        Organization(id=1, name="Seller", slug="seller", created_by=seller_created_by, is_active=True, source="orcahub"),
        Organization(id=2, name="Buyer", slug="buyer", created_by=MINTER, is_active=True, source="orcahub"),
        OrganizationCluster(organization_id=1, cluster_id="c1", registered_by=MINTER),
        *(extra or []),
    ])
    await db.commit()
    return db


async def _provision(db: Any, minter: Optional[str], *, carries_the_key: bool = True, project: Optional[str] = "orc-p-1") -> bool:
    return await integrations._ensure_provision_cluster_mapping(
        db, organization_id=2, cluster_id="c1", registered_by=minter, key_minter=minter,
        project_name=project, carries_the_key=carries_the_key,
    )


def _bound(db: Any) -> set:
    return {row.organization_id for row in db.sync.query(OrganizationCluster).filter_by(cluster_id="c1")}


async def test_a_buyer_of_the_holding_party_is_bound_after_a_reach_check(audits, reach_checks) -> None:
    db = await _world()
    assert await _provision(db, MINTER) is True
    assert _bound(db) == {1, 2}
    assert audits == [], "the cluster gains no holder outside the key's party"
    assert reach_checks == [("c1", "orc-p-1")], "what the buyer reaches is still checked"


async def test_a_purchase_that_reaches_the_host_is_refused_and_binds_nothing(audits, monkeypatch) -> None:
    async def unsafe(cluster_id, project_name):
        raise _refusal()

    monkeypatch.setattr(integrations, "assert_purchase_reach_safe", unsafe)
    db = await _world()
    with pytest.raises(SharedClusterGuardError) as refused:
        await _provision(db, MINTER)
    assert refused.value.status_code == 409
    assert _bound(db) == {1}


@pytest.mark.parametrize("carries_the_key, project", [(False, "orc-p-1"), (True, None), (True, "  ")])
async def test_an_organization_the_key_does_not_own_or_no_project_is_audited_whole(
    audits, reach_checks, carries_the_key, project,
) -> None:
    db = await _world()
    with pytest.raises(SharedClusterGuardError):
        await _provision(db, MINTER, carries_the_key=carries_the_key, project=project)
    assert len(audits) == 1
    assert reach_checks == []


async def test_further_buyers_of_the_same_party_are_bound_without_an_audit(audits, reach_checks) -> None:
    earlier = Organization(id=3, name="Earlier", slug="earlier", created_by=MINTER, is_active=True, source="orcahub")
    db = await _world(extra=[earlier, OrganizationCluster(organization_id=3, cluster_id="c1", registered_by=None)])
    assert await _provision(db, MINTER) is True
    assert _bound(db) == {1, 2, 3}
    assert audits == []


async def test_a_cluster_another_party_holds_is_still_audited(audits, reach_checks) -> None:
    other = Organization(id=3, name="Other", slug="other", created_by=OTHER, is_active=True, source="local")
    db = await _world(extra=[other, OrganizationCluster(organization_id=3, cluster_id="c1", registered_by=OTHER)])
    with pytest.raises(SharedClusterGuardError) as refused:
        await _provision(db, MINTER)
    assert refused.value.status_code == 409
    assert len(audits) == 1
    assert _bound(db) == {1, 3}, "nothing was bound"


async def test_a_seller_organization_of_another_party_is_still_audited(audits, reach_checks) -> None:
    db = await _world(seller_created_by=OTHER)
    with pytest.raises(SharedClusterGuardError):
        await _provision(db, MINTER)
    assert len(audits) == 1


async def test_an_unbound_registration_by_someone_else_is_still_audited(audits, reach_checks) -> None:
    db = await _world(extra=[OrganizationCluster(organization_id=None, cluster_id="c1", registered_by=OTHER)])
    with pytest.raises(SharedClusterGuardError):
        await _provision(db, MINTER)
    assert len(audits) == 1


@pytest.mark.parametrize("minter", [None, ""])
async def test_a_key_with_no_known_minter_is_still_audited(audits, reach_checks, minter) -> None:
    db = await _world()
    with pytest.raises(SharedClusterGuardError):
        await _provision(db, minter)
    assert len(audits) == 1


async def test_a_buyer_already_bound_is_left_alone(audits, reach_checks) -> None:
    db = await _world(extra=[OrganizationCluster(organization_id=2, cluster_id="c1", registered_by=None)])
    assert await _provision(db, MINTER) is False
    assert audits == []


@pytest.mark.parametrize("via, carries", [("provision", True), ("claim", True), ("backfill", True), ("enroll", False), ("admin", False)])
async def test_only_an_owner_row_that_lends_a_party_carries_the_key(via: str, carries: bool) -> None:
    from app.models.db_models import OrganizationOwnerKey

    db = await _world(extra=[OrganizationOwnerKey(organization_id=2, api_key_id="key-1", added_via=via)])
    assert await integrations._org_carries_the_key(db, 2, "key-1") is carries
    assert await integrations._org_carries_the_key(db, 2, "another-key") is False
    assert await integrations._org_carries_the_key(db, 2, None) is False
