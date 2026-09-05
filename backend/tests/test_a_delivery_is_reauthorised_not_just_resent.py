"""Revocation has to be real, and a payload has to be narrow.

Two properties, both of which are easy to believe are true without being true.

The first: an installation's stored grant records what an operator consented to. If a delivery
consulted only that, an add-on would keep hearing about a cluster after the organization lost it,
and on a platform where clusters are rented that means one tenant's add-on watching the next
tenant's workload on hardware that changed hands. So the dispatcher reloads the installation
immediately before sending and drops the delivery if it no longer reaches.

The second: what leaves this deployment lands on somebody else's infrastructure. The payload is
built from a typed object with named fields and a filter, not from a dict a caller shapes, because
the alternative is a rule enforced at review time and review is where this class of leak comes
from. A field that is obviously useful is often also identifying.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Optional

import pytest

from app.core.api_key_expiry import now_utc
from app.services.webhooks import dispatcher
from app.services.webhooks.envelope import (
    ALLOWED_DATA_FIELDS,
    ResourceEvent,
    assert_payload_is_clean,
    build_envelope,
)


# ── the payload ────────────────────────────────────────────────────────────────────────────────

def _event(**overrides: Any) -> ResourceEvent:
    base = dict(
        event_id="evt_1",
        event_type="orcastra.instance.created.v1",
        occurred_at="2026-09-05T00:00:00Z",
        cluster_id="nuc2",
        resource_type="INSTANCE",
        resource_name="web-01",
        project="tenant-a",
    )
    base.update(overrides)
    return ResourceEvent(**base)


@pytest.mark.parametrize(
    "leak",
    [
        {"ipv4_address": "10.0.0.5"},
        {"mac_address": "aa:bb:cc:dd:ee:ff"},
        {"config": {"user.data": "#cloud-config\\npassword: hunter2"}},
        {"devices": {"root": {"path": "/"}}},
        {"certificate": "-----BEGIN CERTIFICATE-----"},
        {"api_key_id": "oak_abc"},
        {"exec_output": "root@host:~#"},
        {"memory_total": 137438953472},
        {"other_organization": "tenant-b"},
    ],
)
def test_a_field_outside_the_allow_list_never_leaves(leak: Dict[str, Any]) -> None:
    """Passed through `extra`, which exists so a future event can add a field without a new class.

    Filtered rather than trusted, so putting an address in there yields nothing rather than a
    delivery carrying it.
    """
    envelope = build_envelope(
        _event(extra=leak),
        deployment_id="dev", subscription_id=1, installation_id=7, sequence=1,
    )
    for key in leak:
        assert key not in envelope["data"], f"{key} reached a third party's infrastructure"
    assert_payload_is_clean(envelope)


def test_the_fields_that_do_leave_are_the_ones_named() -> None:
    envelope = build_envelope(
        _event(state="RUNNING", operation_id="op-1"),
        deployment_id="dev", subscription_id=1, installation_id=7, sequence=1,
    )
    assert set(envelope["data"]) <= ALLOWED_DATA_FIELDS
    assert envelope["data"]["cluster_id"] == "nuc2"
    assert envelope["data"]["resource_name"] == "web-01"
    assert envelope["data"]["state"] == "RUNNING"


def test_the_envelope_carries_a_stable_idempotency_key() -> None:
    """Delivery is at-least-once, so a receiver has to be able to dedupe.

    The id is generated once for the event, not per attempt, which is the property that makes
    that possible and the one easiest to lose when adding a retry loop.
    """
    event = _event()
    first = build_envelope(event, deployment_id="d", subscription_id=1, installation_id=7, sequence=1)
    second = build_envelope(event, deployment_id="d", subscription_id=1, installation_id=7, sequence=2)
    assert first["id"] == second["id"] == "evt_1"


def test_a_dirty_payload_is_refused_on_the_row_not_only_in_a_test() -> None:
    with pytest.raises(ValueError) as exc:
        assert_payload_is_clean({"data": {"cluster_id": "nuc2", "ipv4_address": "10.0.0.5"}})
    assert "ipv4_address" in str(exc.value)


# ── re-authorisation ───────────────────────────────────────────────────────────────────────────

@dataclass
class FakeInstall:
    id: int = 7
    status: str = "active"
    granted_scope: List[Dict[str, Any]] = field(
        default_factory=lambda: [{"cluster_id": "nuc2", "permissions": ["read"]}]
    )


@dataclass
class FakeSubscription:
    id: int = 1
    installation_id: int = 7
    endpoint_url: str = "https://example.com/h"
    event_types: List[str] = field(default_factory=lambda: ["orcastra.instance.created.v1"])
    status: str = "active"
    consecutive_failures: int = 0
    last_failure_at: Any = None
    last_failure_reason: Optional[str] = None
    last_success_at: Any = None
    suspended_reason: Optional[str] = None


class FakeResult:
    def __init__(self, value): self._value = value
    def scalar_one_or_none(self): return self._value


class FakeDB:
    def __init__(self, install): self.install = install
    async def execute(self, _stmt): return FakeResult(self.install)
    async def commit(self): return None


def _payload(cluster_id: str = "nuc2") -> Dict[str, Any]:
    return {"data": {"cluster_id": cluster_id, "resource_name": "web-01"}}


def test_a_delivery_is_dropped_when_the_installation_is_gone() -> None:
    reason = asyncio.run(
        dispatcher._still_authorised(FakeDB(None), FakeSubscription(), _payload())
    )
    assert reason and "no longer exists" in reason


def test_a_delivery_is_dropped_when_the_installation_is_suspended() -> None:
    reason = asyncio.run(
        dispatcher._still_authorised(
            FakeDB(FakeInstall(status="suspended")), FakeSubscription(), _payload()
        )
    )
    assert reason and "suspended" in reason


def test_a_delivery_is_dropped_when_the_cluster_left_the_grant() -> None:
    """The one a stored grant cannot notice.

    The subscription is unchanged, the installation is still active, and the event is still one
    it asked for. What changed is that the organization no longer reaches that cluster.
    """
    reason = asyncio.run(
        dispatcher._still_authorised(
            FakeDB(FakeInstall()), FakeSubscription(), _payload("pandora")
        )
    )
    assert reason and "no longer reaches" in reason


def test_an_authorised_delivery_is_not_dropped() -> None:
    assert asyncio.run(
        dispatcher._still_authorised(FakeDB(FakeInstall()), FakeSubscription(), _payload())
    ) is None


# ── retry, dead letter, auto-suspend ───────────────────────────────────────────────────────────

@dataclass
class FakeDelivery:
    id: int = 1
    attempt: int = 0
    state: str = "inflight"
    last_error: Optional[str] = None
    next_attempt_at: Any = None
    locked_by: Optional[str] = "w1"
    locked_at: Any = None


def test_backoff_uses_full_jitter() -> None:
    """Equal jitter leaves a synchronised backlog synchronised. Full jitter is the variant that
    actually spreads a queue resuming after an outage, so the floor has to be able to reach zero.
    """
    samples = [dispatcher._next_attempt_delay(2) for _ in range(200)]
    base = dispatcher.RETRY_SCHEDULE_SECONDS[2]
    assert all(0 <= s <= base for s in samples)
    assert min(samples) < base * 0.2, "the delay never approaches zero, so it is not full jitter"
    assert max(samples) > base * 0.8, "the delay never approaches the base"


def test_a_failure_reschedules_until_the_attempts_run_out() -> None:
    now = now_utc()
    subscription = FakeSubscription()
    delivery = FakeDelivery(attempt=1)
    state = asyncio.run(
        dispatcher._record_failure(FakeDB(None), delivery, subscription, "boom", now)
    )
    assert state == "pending"
    assert delivery.next_attempt_at > now
    assert delivery.locked_by is None, "a failed attempt kept its lock, so nobody can retry it"

    delivery = FakeDelivery(attempt=dispatcher.MAX_ATTEMPTS)
    state = asyncio.run(
        dispatcher._record_failure(FakeDB(None), delivery, subscription, "boom", now)
    )
    assert state == "dead"


def test_a_subscription_that_keeps_failing_is_suspended() -> None:
    """Continuing to POST at an endpoint that has refused twenty in a row is us behaving like a
    reflector rather than a sender."""
    now = now_utc()
    subscription = FakeSubscription(consecutive_failures=dispatcher.AUTO_SUSPEND_AFTER - 1)
    asyncio.run(
        dispatcher._record_failure(FakeDB(None), FakeDelivery(), subscription, "gone", now)
    )
    assert subscription.status == "suspended"
    assert subscription.suspended_reason and "consecutive failures" in subscription.suspended_reason

