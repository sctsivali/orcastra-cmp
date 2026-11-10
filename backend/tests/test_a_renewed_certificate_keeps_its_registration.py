"""Everything that remembers a cluster by its pin keeps working across a public-CA renewal.

A key grant, a snapshot schedule, a migration plan and a running migration job each record the
fingerprint they were approved against, so a re-registered cluster of the same name does not
inherit them. A Let's Encrypt renewal changes that fingerprint without changing the registration,
so each of them accepts a fingerprint in the cluster's renewal lineage, and still refuses anything
else. A self-signed cluster has no lineage, so a changed fingerprint still ends each of them.
"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.db_models import BreakGlassGrant
from app.models.migration_schemas import MigrationPlanCreateRequest
from app.services.break_glass_service import _now, get_active_key_grant
from app.services.lxd_certs.pinning import (
    TRUST_PUBLIC_CA,
    TRUST_TOFU,
    LXDCertificatePinError,
    PinState,
)
from app.services.migrations import context, planning
from app.services.migrations.contracts import MigrationAuthorityError, MigrationConflict
from app.services.snapshots.schedule_runner import run_one
from tests._schedule_fakes import FakeDB, FakeSchedule, ops_rows
from tests._snapshot_fakes import FakeClient, FakeInstanceNode, service
from tests.test_a_schedule_is_reauthorized_before_every_run import SLOT

OLD = "a" * 64
NEW = "b" * 64
OTHER = "c" * 64

RENEWED = PinState(fingerprint=NEW, trust=TRUST_PUBLIC_CA, host="vdc.example.org:443", previous=(OLD,))
SELF_SIGNED = PinState(fingerprint=NEW, trust=TRUST_TOFU, host=None, previous=(OLD,))


class _Session:
    def __init__(self, grant):
        self._grant = grant

    async def execute(self, _statement):
        grant = self._grant
        return SimpleNamespace(scalar_one_or_none=lambda: grant)


def _grant(fingerprint):
    return BreakGlassGrant(
        api_key_id="oak_a",
        cluster_id="hetzner",
        cluster_fingerprint=fingerprint,
        reason="support",
        created_by="ops",
        expires_at=_now() + timedelta(hours=1),
    )


@pytest.mark.asyncio
async def test_a_key_grant_survives_a_public_ca_renewal() -> None:
    grant = _grant(OLD)
    assert await get_active_key_grant(_Session(grant), "oak_a", "hetzner", pin_state=RENEWED) is grant


@pytest.mark.asyncio
async def test_a_key_grant_for_another_registration_is_still_refused() -> None:
    grant = _grant(OTHER)
    assert await get_active_key_grant(_Session(grant), "oak_a", "hetzner", pin_state=RENEWED) is None


@pytest.mark.asyncio
async def test_a_self_signed_change_still_ends_a_key_grant() -> None:
    grant = _grant(OLD)
    assert (
        await get_active_key_grant(_Session(grant), "oak_a", "hetzner", pin_state=SELF_SIGNED)
        is None
    )


def _run_schedule(schedule, state):
    async def reader(_cluster_id):
        return state

    node = FakeInstanceNode(snapshots=ops_rows(schedule.id, 1))
    outcome = asyncio.run(
        run_one(
            FakeDB(), schedule, now=SLOT, lxd_service=service(FakeClient(node)),
            fingerprint_reader=reader,
        )
    )
    return outcome


def test_a_schedule_runs_after_a_renewal_and_follows_the_new_pin() -> None:
    schedule = FakeSchedule(due_slot=SLOT, cluster_fingerprint=OLD)
    outcome = _run_schedule(schedule, RENEWED)
    assert outcome.result == "success", outcome.error
    assert schedule.cluster_fingerprint == NEW, (
        "a schedule outlives the bounded lineage, so it must carry the current pin forward"
    )


def test_a_schedule_on_a_changed_self_signed_cluster_is_refused() -> None:
    schedule = FakeSchedule(due_slot=SLOT, cluster_fingerprint=OLD)
    outcome = _run_schedule(schedule, SELF_SIGNED)
    assert outcome.result == "refused"
    assert outcome.refusal.code == "cluster_is_a_different_registration"
    assert schedule.cluster_fingerprint == OLD


def _request(**overrides: Any) -> MigrationPlanCreateRequest:
    payload = {
        "scope": "instance",
        "intent": "copy",
        "source": {"cluster_id": "hetzner", "project": "qa-src", "resource_name": "web"},
        "target": {"cluster_id": "hetzner", "project": "qa-dst", "resource_name": "web-copy"},
        "mappings": {},
    }
    payload["source"].update(overrides.get("source", {}))
    payload["target"].update(overrides.get("target", {}))
    return MigrationPlanCreateRequest.model_validate(payload)


class _Manager:
    def __init__(self, states: dict, *, pin_on_refresh=None, refuse=False):
        self.states = states
        self.pin_on_refresh = pin_on_refresh or {}
        self.refuse = refuse
        self.refreshed: list = []

    def refresh_pin(self, cluster_id):
        self.refreshed.append(cluster_id)
        if self.refuse:
            raise LXDCertificatePinError("changed")
        if cluster_id in self.pin_on_refresh:
            self.states[cluster_id] = self.pin_on_refresh[cluster_id]
        state = self.states.get(cluster_id)
        return state.fingerprint if state else None


def _planner(monkeypatch, manager, *, pinning_on=True):
    async def pin_state(cluster_id, *, strict=False):
        return manager.states.get(cluster_id)

    monkeypatch.setattr(planning, "cluster_pin_state", pin_state)
    monkeypatch.setattr(planning, "_cluster_label", lambda cluster_id: f"<{cluster_id}>")
    return planning.MigrationPlanningMixin(
        db=object(),
        lxd_service=SimpleNamespace(connection_manager=manager),
        settings=SimpleNamespace(lxd_pin_server_certificate=pinning_on),
    )


@pytest.mark.asyncio
async def test_a_plan_records_a_missing_pin_before_refusing(monkeypatch) -> None:
    """A cluster that was never pinned is pinned on demand instead of refusing every plan."""
    manager = _Manager({}, pin_on_refresh={"hetzner": RENEWED})
    service_ = _planner(monkeypatch, manager)
    assert await service_._fingerprints(_request()) == (NEW, NEW)
    assert manager.refreshed == ["hetzner"]


@pytest.mark.asyncio
async def test_a_plan_names_the_cluster_it_cannot_prove(monkeypatch) -> None:
    manager = _Manager({"nuc2": SELF_SIGNED})
    service_ = _planner(monkeypatch, manager)
    with pytest.raises(MigrationConflict) as raised:
        await service_._fingerprints(_request(target={"cluster_id": "nuc2"}))
    message = str(raised.value)
    assert "<hetzner>" in message and "<nuc2>" not in message
    assert "re-pin" in message


@pytest.mark.asyncio
async def test_a_plan_says_when_pinning_is_off(monkeypatch) -> None:
    service_ = _planner(monkeypatch, _Manager({}), pinning_on=False)
    with pytest.raises(MigrationConflict) as raised:
        await service_._fingerprints(_request())
    assert "turned off" in str(raised.value)


@pytest.mark.asyncio
async def test_a_plan_on_a_cluster_presenting_a_changed_certificate_is_refused_plainly(
    monkeypatch,
) -> None:
    service_ = _planner(monkeypatch, _Manager({"hetzner": RENEWED}, refuse=True))
    with pytest.raises(MigrationConflict) as raised:
        await service_._fingerprints(_request())
    assert "different server certificate" in str(raised.value)


@pytest.mark.asyncio
async def test_a_selected_pre_renewal_fingerprint_is_still_the_same_cluster(monkeypatch) -> None:
    service_ = _planner(monkeypatch, _Manager({"hetzner": RENEWED}))
    request = _request(source={"cluster_fingerprint": OLD}, target={"cluster_fingerprint": OLD})
    assert await service_._fingerprints(request) == (NEW, NEW)


@pytest.mark.asyncio
async def test_a_selected_fingerprint_of_another_registration_is_refused(monkeypatch) -> None:
    service_ = _planner(monkeypatch, _Manager({"hetzner": RENEWED}))
    with pytest.raises(MigrationConflict):
        await service_._fingerprints(_request(source={"cluster_fingerprint": OTHER}))


@pytest.mark.asyncio
async def test_a_plan_approved_before_a_renewal_can_still_execute(monkeypatch) -> None:
    service_ = _planner(monkeypatch, _Manager({"hetzner": RENEWED}))
    plan = SimpleNamespace(source_cluster_fingerprint=OLD, target_cluster_fingerprint=OLD)
    assert await service_._renewed_since(_request(), plan) is True
    plan = SimpleNamespace(source_cluster_fingerprint=OTHER, target_cluster_fingerprint=OLD)
    assert await service_._renewed_since(_request(), plan) is False


def _job(fingerprint):
    return SimpleNamespace(
        source_cluster_id="hetzner",
        target_cluster_id="hetzner",
        source_cluster_fingerprint=fingerprint,
        target_cluster_fingerprint=fingerprint,
    )


def _job_pins(monkeypatch, state):
    async def pin_state(_cluster_id, *, strict=False):
        return state

    monkeypatch.setattr(context, "cluster_pin_state", pin_state)


@pytest.mark.asyncio
async def test_a_running_job_binds_to_the_renewed_pin(monkeypatch) -> None:
    _job_pins(monkeypatch, RENEWED)
    assert await context._current_job_pins(_job(OLD)) == (NEW, NEW)


@pytest.mark.asyncio
async def test_a_running_job_on_a_changed_self_signed_cluster_stops(monkeypatch) -> None:
    _job_pins(monkeypatch, SELF_SIGNED)
    with pytest.raises(MigrationAuthorityError):
        await context._current_job_pins(_job(OLD))


@pytest.mark.asyncio
async def test_a_running_job_stops_when_the_pin_cannot_be_read(monkeypatch) -> None:
    async def pin_state(_cluster_id, *, strict=False):
        return None

    monkeypatch.setattr(context, "cluster_pin_state", pin_state)
    with pytest.raises(MigrationAuthorityError):
        await context._current_job_pins(_job(OLD))


@pytest.mark.asyncio
async def test_a_plan_refresh_runs_on_the_bounded_pool(monkeypatch) -> None:
    """A plan action must not be able to tie up the shared thread pool with handshakes."""
    calls = []

    async def bounded(lxd_service, cluster_id):
        calls.append(cluster_id)
        return lxd_service.connection_manager.refresh_pin(cluster_id)

    manager = _Manager({}, pin_on_refresh={"hetzner": RENEWED})
    service_ = _planner(monkeypatch, manager)
    monkeypatch.setattr(planning, "refresh_pin_for", bounded)
    await service_._fingerprints(_request())
    assert calls == ["hetzner"]

