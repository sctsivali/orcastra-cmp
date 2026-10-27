"""A buyer founder OrcaHub promoted to role_partner is held to the purchase's paid term too.

A Partner has no tenant ACL, so the clamp on the purchase's own policies never reached it: during
retention the founder kept operate, console and delete on what it bought (security review
2026-10-27). On a cluster it reaches only through purchases past their term it may now read and
not change anything, a console or terminal socket included (core/sale_term.py, core/org_scope.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.core import org_scope
from app.core.request_context import RequestContext, set_request_context
from app.core.sale_term import ACTIVE, ENDED, READ_ONLY, partner_read_only_clusters, sale_phase
from app.models.db_models import (
    Organization,
    OrganizationCluster,
    OrganizationMember,
    TenantAccessPolicy,
    TenantPolicyRule,
)
from tests._sqlite_session import org_tables, sqlite_session, with_json_tables

FOUNDER = "Founder@Buyer.test"
NOW = datetime.now(timezone.utc)
PAST, FUTURE = NOW - timedelta(days=1), NOW + timedelta(days=5)


@pytest.mark.parametrize("terms, phase", [
    ([], ENDED),
    ([(None, None)], ACTIVE),
    ([(FUTURE, FUTURE + timedelta(days=1))], ACTIVE),
    ([(PAST, FUTURE)], READ_ONLY),
    ([(PAST, FUTURE), (None, None)], ACTIVE),
    ([(PAST - timedelta(days=2), PAST)], ENDED),
])
def test_the_phase_of_a_purchase(terms, phase) -> None:
    assert sale_phase(terms, NOW) == phase


async def _world(*, term_ended_at=PAST, access_ends_at=FUTURE, registered_elsewhere=False):
    db = with_json_tables(sqlite_session(*org_tables()), TenantAccessPolicy, TenantPolicyRule)
    db.add(Organization(id=7, name="Buyer", slug="buyer", source="orcahub", created_by="minter@seller.test"))
    db.add(Organization(id=8, name="Founder's own", slug="own", source="local", created_by="x@y.test"))
    await db.commit()
    db.add_all([
        OrganizationMember(organization_id=7, user_email="founder@buyer.test", role="partner"),
        OrganizationCluster(organization_id=7, cluster_id="sold", registered_by=None),
        TenantAccessPolicy(id=70, organization_id=7, name="auto:shop", source="orcahub",
                           created_by="orcahub-provisioning", term_ended_at=term_ended_at, access_ends_at=access_ends_at),
    ])
    if registered_elsewhere:
        db.add_all([
            OrganizationMember(organization_id=8, user_email="founder@buyer.test", role="member"),
            OrganizationCluster(organization_id=8, cluster_id="sold", registered_by="x@y.test"),
        ])
    await db.commit()
    db.add(TenantPolicyRule(policy_id=70, cluster_id="sold", project_name="shop", instance_name="*",
                            permissions=["view", "operate", "console"]))
    await db.commit()
    return db


async def test_a_cluster_reached_only_by_a_purchase_in_retention_is_read_only() -> None:
    assert await partner_read_only_clusters(await _world(), FOUNDER) == {"sold"}


@pytest.mark.parametrize("kwargs", [
    {"term_ended_at": None},
    {"term_ended_at": FUTURE, "access_ends_at": FUTURE + timedelta(days=1)},
    {"registered_elsewhere": True},
], ids=["no-term", "term-running", "also-registered-elsewhere"])
async def test_otherwise_nothing_is_read_only(kwargs) -> None:
    assert await partner_read_only_clusters(await _world(**kwargs), FOUNDER) == set()


@pytest.fixture
def scope():
    tokens = [
        org_scope._allowed_clusters_var.set({"sold", "mine"}),
        org_scope._read_only_clusters_var.set(frozenset({"sold"})),
        org_scope._interactive_var.set(False),
    ]
    yield
    for var, token in zip(
        (org_scope._allowed_clusters_var, org_scope._read_only_clusters_var, org_scope._interactive_var), tokens,
    ):
        var.reset(token)


@pytest.mark.parametrize("method, path, refused", [
    ("GET", "/api/v1/instances", False),
    ("POST", "/api/v1/instances/vm/start", True),
    ("DELETE", "/api/v1/projects/sold/shop", True),
    ("PATCH", "/api/v1/instances/vm", True),
    ("POST", "/api/v1/ws-tickets", False),
    ("POST", "/api/v1/operations/abc-123/wait", False),
    # Re-review 2026-10-27: the exemption is the exact route, never a suffix or another method.
    ("DELETE", "/api/v1/ws-tickets", True),
    ("DELETE", "/api/v1/profiles/ws-tickets", True),
    ("POST", "/api/v1/instances/ws-tickets", True),
    ("PUT", "/api/v1/operations/abc-123/wait", True),
])
def test_a_change_on_a_read_only_cluster_is_refused(scope, method, path, refused) -> None:
    set_request_context(RequestContext(method=method, path=path))
    if refused:
        with pytest.raises(HTTPException) as denied:
            org_scope.verify_cluster_access("sold")
        assert denied.value.status_code == 403 and "paid term" in denied.value.detail
    else:
        org_scope.verify_cluster_access("sold")
    org_scope.verify_cluster_access("mine")


def test_a_console_or_terminal_socket_is_a_change(scope) -> None:
    set_request_context(None)
    org_scope._interactive_var.set(True)
    with pytest.raises(HTTPException):
        org_scope.verify_cluster_access("sold")
    org_scope._interactive_var.set(False)
    org_scope.verify_cluster_access("sold")


def test_the_console_and_terminal_sockets_declare_themselves_interactive() -> None:
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[1] / "app"
    for name in ("routers/console.py", "routers/terminal.py", "services/ws_session_guard.py"):
        # The guard re-checks an open console or terminal: a term that ends while one is open closes it.
        assert "set_org_scope_for_ws(user, interactive=True)" in (app_dir / name).read_text(encoding="utf-8"), name


async def test_a_founder_cannot_bind_a_cluster_it_reaches_only_by_purchase() -> None:
    """Re-review 2026-10-27: bound to a new organization, the row would carry the founder as
    registrant and outlive the purchase."""
    from app.core.sale_term import reaches_only_by_purchase

    assert await reaches_only_by_purchase(await _world(term_ended_at=None, access_ends_at=None), FOUNDER, "sold") is True
    assert await reaches_only_by_purchase(await _world(registered_elsewhere=True), FOUNDER, "sold") is False


async def test_the_bind_route_refuses_it(monkeypatch) -> None:
    from app.core.auth import AuthenticatedUser
    from app.routers import organizations

    db = await _world(term_ended_at=None, access_ends_at=None)

    class _Svc:
        def __init__(self, _db):
            pass

        async def user_has_access(self, *_args):
            return True

    async def owned(_user, _db):
        return {"sold"}

    monkeypatch.setattr(organizations, "OrganizationService", _Svc)
    monkeypatch.setattr(organizations, "get_owned_clusters_ignoring_admin", owned)
    founder = AuthenticatedUser(user_id="f", email=FOUNDER, groups=["role_partner"])
    with pytest.raises(HTTPException) as refused:
        await organizations.bind_cluster(8, organizations.ClusterBindRequest(cluster_id="sold"), user=founder, db=db)
    assert refused.value.status_code == 403 and "purchase" in refused.value.detail
