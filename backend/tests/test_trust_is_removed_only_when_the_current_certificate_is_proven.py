"""The sweep removes a superseded trust entry only when that cannot lock the dashboard out.

After a regenerate, Vault holds a certificate the cluster does not trust yet while every worker
still talks through the old one. Removing the old entry then would cut the dashboard off the
cluster entirely. So the sweep proves the CURRENT certificate first, re-arms the grace period
until it can, and never touches a fingerprint that is still current or pending.
"""

from datetime import timedelta

import pytest

from app.services.lxd_certs import trust_cleanup
from app.services.lxd_certs import trust_cleanup_store as store
from app.services.lxd_certs.trust_cleanup import ClusterCertState
from app.services.lxd_certs.trust_http import IdentityProbe, RemovalOutcome
from tests._cert_rotation_fakes import SessionMaker, rows

CLUSTER = "nuc9"
CURRENT = "c" * 64
PENDING = "d" * 64
OLD = "a" * 64
OLDER = "b" * 64

TRUSTED = IdentityProbe(True, True, False, "trusted")
UNTRUSTED = IdentityProbe(True, False, None, "the cluster does not trust the current certificate yet")
UNREACHABLE = IdentityProbe(False, False, None, "cluster unreachable: ConnectTimeout")
RESTRICTED = IdentityProbe(True, True, True, "restricted certificate cannot manage trust")


def _naive(value):
    # SQLite hands timestamps back without a zone; both sides are UTC.
    return value.replace(tzinfo=None)


def _state(pending=None):
    return ClusterCertState(
        endpoint="https://10.0.0.9:8443", cluster_type="LXD", server_fingerprint="ff" * 32,
        current_pem="PEM", current_key="KEY", current_fp=CURRENT, pending_fp=pending,
    )


@pytest.fixture
def maker():
    return SessionMaker()


@pytest.fixture
def lxd(monkeypatch):
    """Scripted cluster: the probe answer and per-fingerprint outcomes; records what was sent."""

    class Script:
        state = _state()
        probe = TRUSTED
        outcomes = {}
        removed_calls = []
        audits = []

    def read(cluster_id):
        return Script.state

    def prove_and_remove(cluster_id, state, fingerprints):
        Script.removed_calls.append(list(fingerprints))
        if not Script.probe.may_remove:
            return Script.probe, {}
        return Script.probe, {fp: Script.outcomes.get(fp, (RemovalOutcome.REMOVED, "removed")) for fp in fingerprints}

    monkeypatch.setattr(trust_cleanup, "read_cluster_state", read)
    monkeypatch.setattr(trust_cleanup, "prove_and_remove", prove_and_remove)
    monkeypatch.setattr(trust_cleanup, "_audit", lambda cid, row, trigger, actor: Script.audits.append((row.fingerprint, row.state, trigger, actor)))
    return Script


async def _record(maker, fp, *, due_in=-1, reason="renew", requested_by="ops@example.com"):
    db = maker.db
    row = await store.record_superseded(
        db, cluster_id=CLUSTER, fingerprint=fp, reason=reason, grace_seconds=600, requested_by=requested_by
    )
    row.due_at = store.utcnow() + timedelta(seconds=due_in)
    db.sync.commit()
    return row


async def test_nothing_happens_before_the_grace_period_ends(maker, lxd):
    await _record(maker, OLD, due_in=300)
    report = await trust_cleanup.sweep_cluster(maker, CLUSTER)
    assert report.skipped == "nothing due" and lxd.removed_calls == []


async def test_an_untrusted_current_certificate_rearms_the_grace_and_removes_nothing(maker, lxd):
    lxd.probe = UNTRUSTED
    await _record(maker, OLD)

    await trust_cleanup.sweep_cluster(maker, CLUSTER)

    [row] = rows(maker.db)
    assert row.state == "pending" and row.attempts == 0, "waiting for trust is not a failed attempt"
    assert _naive(row.due_at) > _naive(store.utcnow()) + timedelta(seconds=590), (
        "the grace starts again once removal becomes safe"
    )
    assert "does not trust" in row.last_error


async def test_an_unreachable_cluster_backs_off(maker, lxd):
    lxd.probe = UNREACHABLE
    await _record(maker, OLD)

    await trust_cleanup.sweep_cluster(maker, CLUSTER)

    [row] = rows(maker.db)
    assert row.state == "pending" and row.attempts == 1
    assert "unreachable" in row.last_error


async def test_a_restricted_dashboard_certificate_fails_with_its_reason(maker, lxd):
    lxd.probe = RESTRICTED
    await _record(maker, OLD)

    await trust_cleanup.sweep_cluster(maker, CLUSTER)

    [row] = rows(maker.db)
    assert row.state == "failed" and "restricted" in row.last_error


@pytest.mark.parametrize(
    "outcome, state, already_gone",
    [
        (RemovalOutcome.REMOVED, "removed", False),
        (RemovalOutcome.ALREADY_GONE, "removed", True),
        (RemovalOutcome.FORBIDDEN, "failed", False),
        (RemovalOutcome.FAILED, "failed", False),
        (RemovalOutcome.TRANSIENT, "pending", False),
    ],
)
async def test_each_outcome_settles_the_row_honestly(maker, lxd, outcome, state, already_gone):
    lxd.outcomes = {OLD: (outcome, f"said {outcome.value}")}
    await _record(maker, OLD)

    report = await trust_cleanup.sweep_cluster(maker, CLUSTER)

    [row] = rows(maker.db)
    assert row.state == state and bool(row.already_gone) is already_gone
    assert report.outcomes[OLD] == outcome.value
    if state == "removed":
        assert row.removed_at is not None and row.last_error is None
    if outcome is RemovalOutcome.TRANSIENT:
        assert row.attempts == 1


async def test_a_fingerprint_that_is_still_current_or_pending_is_cancelled_never_sent(maker, lxd):
    lxd.state = _state(pending=PENDING)
    await _record(maker, CURRENT, reason="renew_aborted")
    await _record(maker, PENDING, reason="cancel")
    await _record(maker, OLD)

    await trust_cleanup.sweep_cluster(maker, CLUSTER)

    states = {row.fingerprint: row.state for row in rows(maker.db)}
    assert states == {CURRENT: "cancelled", PENDING: "cancelled", OLD: "removed"}
    assert lxd.removed_calls == [[OLD]]


async def test_vault_not_answering_leaves_every_row_alone(maker, lxd):
    lxd.state = None
    await _record(maker, OLD)

    report = await trust_cleanup.sweep_cluster(maker, CLUSTER)

    [row] = rows(maker.db)
    assert report.skipped and row.state == "pending" and lxd.removed_calls == []


async def test_remove_now_skips_the_grace_and_retries_failed_rows(maker, lxd):
    await _record(maker, OLD, due_in=600)
    failed = await _record(maker, OLDER, due_in=600)
    failed.state = "failed"
    maker.db.sync.commit()

    await trust_cleanup.sweep_cluster(maker, CLUSTER, force=True, trigger="remove_now", actor="admin@example.com")

    assert {row.fingerprint: row.state for row in rows(maker.db)} == {OLD: "removed", OLDER: "removed"}
    assert {(fp, actor) for fp, _, _, actor in lxd.audits} == {(OLD, "admin@example.com"), (OLDER, "admin@example.com")}


async def test_remove_now_can_target_one_fingerprint(maker, lxd):
    await _record(maker, OLD, due_in=600)
    await _record(maker, OLDER, due_in=600)

    await trust_cleanup.sweep_cluster(maker, CLUSTER, force=True, fingerprint=OLDER)

    assert {row.fingerprint: row.state for row in rows(maker.db)} == {OLD: "pending", OLDER: "removed"}


async def test_the_timed_sweep_audits_as_the_operator_who_superseded_it(maker, lxd):
    await _record(maker, OLD, requested_by="renewer@example.com")
    await trust_cleanup.sweep_cluster(maker, CLUSTER)
    assert lxd.audits == [(OLD, "removed", "sweep", "renewer@example.com")]


async def test_a_busy_cluster_is_skipped_by_the_sweep_and_refused_for_remove_now(maker, lxd):
    await _record(maker, OLD)
    async with store.cluster_cert_lock(maker.db, CLUSTER):
        report = await trust_cleanup.sweep_cluster(maker, CLUSTER)
        assert report.skipped and lxd.removed_calls == []
        with pytest.raises(store.CertificateOperationBusy):
            await trust_cleanup.sweep_cluster(maker, CLUSTER, force=True)


async def test_the_api_view_carries_a_manual_command_only_while_the_entry_remains(maker, lxd):
    lxd.outcomes = {OLD: (RemovalOutcome.FORBIDDEN, "HTTP 403")}
    await _record(maker, OLD)
    await _record(maker, OLDER, due_in=600)
    await trust_cleanup.sweep_cluster(maker, CLUSTER)

    view = {item.fingerprint: item for item in trust_cleanup.superseded_view(rows(maker.db), "Incus")}
    assert view[OLD].manual_command == f"incus config trust remove {OLD}"
    assert view[OLDER].manual_command == f"incus config trust remove {OLDER}" and view[OLDER].due_at


async def test_the_sweep_pass_only_visits_clusters_with_due_rows(maker, lxd, monkeypatch):
    visited, switched = [], []
    await _record(maker, OLD)  # due
    await store.record_superseded(maker.db, cluster_id="other", fingerprint=OLDER, reason="renew", grace_seconds=600)
    maker.db.sync.commit()

    async def fake_sweep(session_maker, cluster_id, **_):
        visited.append(cluster_id)
        return trust_cleanup.SweepReport(cluster_id)

    monkeypatch.setattr(trust_cleanup, "sweep_cluster", fake_sweep)
    monkeypatch.setattr(trust_cleanup, "preswitch_worker", lambda cid: switched.append(cid) or False)

    await trust_cleanup.sweep_once(maker)

    assert visited == [CLUSTER], "the other cluster is inside its grace period"
    assert sorted(switched) == [CLUSTER, "other"], "every worker pre-switches during the grace"


async def test_the_kill_switch_stops_the_timed_sweep(maker, lxd, monkeypatch):
    from app.core.config import get_settings

    await _record(maker, OLD)
    monkeypatch.setattr(get_settings(), "lxd_trust_cleanup_enabled", False)
    await trust_cleanup.sweep_once(maker)
    assert lxd.removed_calls == [] and rows(maker.db)[0].state == "pending"


async def test_a_blocked_server_identity_fails_the_row_with_its_reason(maker, lxd):
    lxd.probe = IdentityProbe(True, False, None, "server certificate pinning is disabled", blocked=True)
    await _record(maker, OLD)
    await trust_cleanup.sweep_cluster(maker, CLUSTER)
    [row] = rows(maker.db)
    assert row.state == "failed" and "pinning is disabled" in row.last_error
