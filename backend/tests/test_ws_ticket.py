"""Unit tests for single-use WebSocket auth tickets (app.core.ws_ticket).

Pure, DB-free tests of the ticket lifecycle: round-trip, single-use consumption,
and binding (scope/instance/host/project) enforcement. The CacheManager falls back
to its in-memory store when Redis is unavailable, so these run standalone too.
"""

from app.core.auth import AuthenticatedUser
from app.core.ws_ticket import create_ticket, redeem_ticket


def _user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="fp-abc123",
        email="qa@sivali.co.id",
        name="QA",
        groups=["role_tenant"],
        roles=["role_tenant"],
        raw_claims={"sub": "fp-abc123"},
    )


def test_ticket_roundtrip_returns_bound_user():
    ticket, ttl = create_ticket(_user(), scope="instance", instance="vm1", host="nuc-2", project="default")
    assert ttl > 0
    user = redeem_ticket(ticket, scope="instance", instance="vm1", host="nuc-2", project="default")
    assert user is not None
    assert user.user_id == "fp-abc123"
    assert user.email == "qa@sivali.co.id"
    assert user.groups == ["role_tenant"]


def test_ticket_is_single_use():
    ticket, _ = create_ticket(_user(), scope="instance", instance="vm1", host="nuc-2", project="default")
    first = redeem_ticket(ticket, scope="instance", instance="vm1", host="nuc-2", project="default")
    second = redeem_ticket(ticket, scope="instance", instance="vm1", host="nuc-2", project="default")
    assert first is not None
    assert second is None  # consumed on first redeem


def test_ticket_rejects_instance_mismatch():
    ticket, _ = create_ticket(_user(), scope="instance", instance="vm1", host="nuc-2", project="default")
    user = redeem_ticket(ticket, scope="instance", instance="vm2", host="nuc-2", project="default")
    assert user is None


def test_ticket_rejects_host_and_project_mismatch():
    ticket, _ = create_ticket(_user(), scope="instance", instance="vm1", host="nuc-2", project="proj-a")
    assert redeem_ticket(ticket, scope="instance", instance="vm1", host="other", project="proj-a") is None
    ticket2, _ = create_ticket(_user(), scope="instance", instance="vm1", host="nuc-2", project="proj-a")
    assert redeem_ticket(ticket2, scope="instance", instance="vm1", host="nuc-2", project="proj-b") is None


def test_ticket_rejects_scope_mismatch():
    ticket, _ = create_ticket(_user(), scope="instance", instance="vm1", host="nuc-2", project="default")
    # A per-instance ticket must not be redeemable on the monitoring channel.
    assert redeem_ticket(ticket, scope="monitoring") is None


def test_monitoring_ticket_roundtrip():
    ticket, _ = create_ticket(_user(), scope="monitoring")
    user = redeem_ticket(ticket, scope="monitoring")
    assert user is not None
    assert user.user_id == "fp-abc123"


def test_unknown_ticket_returns_none():
    assert redeem_ticket("does-not-exist", scope="instance", instance="vm1", host="nuc-2", project="default") is None
    assert redeem_ticket(None, scope="monitoring") is None


if __name__ == "__main__":
    test_ticket_roundtrip_returns_bound_user()
    test_ticket_is_single_use()
    test_ticket_rejects_instance_mismatch()
    test_ticket_rejects_host_and_project_mismatch()
    test_ticket_rejects_scope_mismatch()
    test_monitoring_ticket_roundtrip()
    test_unknown_ticket_returns_none()
    print("ws_ticket tests passed")
