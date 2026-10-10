"""Deregistering a cluster revokes the dashboard's own access to it, best effort.

The dashboard used to leave its trust entries behind on deletion (the endpoint's docstring said so),
which is how a host collected entries for clusters no dashboard had registered for months. Its own
current certificate goes last: it is the one that authenticates the removals before it.
"""

import pytest

from app.services.lxd_certs import trust_cleanup
from app.services.lxd_certs import trust_cleanup_store as store
from app.services.lxd_certs.trust_cleanup import ClusterCertState
from app.services.lxd_certs.trust_http import IdentityProbe, RemovalOutcome
from tests._cert_rotation_fakes import SessionMaker

CLUSTER = "nuc9"
CURRENT, PENDING, OLD = "c" * 64, "d" * 64, "a" * 64


@pytest.fixture
def db():
    return SessionMaker().db


@pytest.fixture
def lxd(monkeypatch):
    class Script:
        state = ClusterCertState("https://h:8443", "Incus", None, "PEM", "KEY", CURRENT, PENDING)
        probe = IdentityProbe(True, True, False, "trusted")
        outcomes = {}
        sent = []

    def prove(cluster_id, state, fps):
        Script.sent.append(list(fps))
        if not Script.probe.may_remove:
            return Script.probe, {}
        return Script.probe, {fp: Script.outcomes.get(fp, (RemovalOutcome.REMOVED, "removed")) for fp in fps}

    monkeypatch.setattr(trust_cleanup, "read_cluster_state", lambda cid: Script.state)
    monkeypatch.setattr(trust_cleanup, "prove_and_remove", prove)
    monkeypatch.setattr(trust_cleanup, "_audit_deregistration", lambda *a, **k: None)
    return Script


async def test_tracked_then_pending_then_its_own_entry(db, lxd):
    await store.record_superseded(db, cluster_id=CLUSTER, fingerprint=OLD, reason="renew", grace_seconds=600)

    result = await trust_cleanup.remove_trust_for_deregistration(db, CLUSTER)

    assert lxd.sent == [[OLD, PENDING, CURRENT]]
    assert result.commands == [] and result.complete is True


async def test_what_could_not_be_removed_comes_back_as_commands(db, lxd):
    lxd.outcomes = {CURRENT: (RemovalOutcome.FORBIDDEN, "403")}
    result = await trust_cleanup.remove_trust_for_deregistration(db, CLUSTER)
    assert result.commands == [f"incus config trust remove {CURRENT}"] and result.complete is False


async def test_an_unreachable_cluster_returns_every_command(db, lxd):
    lxd.probe = IdentityProbe(False, False, None, "cluster unreachable")
    result = await trust_cleanup.remove_trust_for_deregistration(db, CLUSTER)
    assert result.commands == [f"incus config trust remove {fp}" for fp in (PENDING, CURRENT)]
    assert result.complete is False


async def test_without_vault_only_the_tracked_entries_can_be_named(db, lxd):
    lxd.state = None
    await store.record_superseded(db, cluster_id=CLUSTER, fingerprint=OLD, reason="renew", grace_seconds=600)
    result = await trust_cleanup.remove_trust_for_deregistration(db, CLUSTER)
    assert result.commands == [f"lxc config trust remove {OLD}"] and lxd.sent == []
    assert result.complete is False, "the dashboard's own entry could not even be named"


async def test_the_rows_go_with_the_cluster(db, lxd):
    await store.record_superseded(db, cluster_id=CLUSTER, fingerprint=OLD, reason="renew", grace_seconds=600)
    await store.record_superseded(db, cluster_id="other", fingerprint=OLD, reason="renew", grace_seconds=600)
    assert await store.delete_for_cluster(db, CLUSTER) == 1
    assert await store.tracked_fingerprints(db, "other") == {OLD}


async def test_a_cluster_that_does_not_trust_the_current_certificate_needs_no_command_for_it(db, lxd):
    lxd.probe = IdentityProbe(True, False, None, "the cluster does not trust the current certificate yet")
    result = await trust_cleanup.remove_trust_for_deregistration(db, CLUSTER)
    assert result.commands == [f"incus config trust remove {PENDING}"], (
        "the pending certificate cannot be checked from an untrusted session, the current one has no entry"
    )
    assert result.complete is False


async def test_an_untrusted_cluster_with_nothing_else_tracked_is_complete(db, lxd):
    lxd.state = ClusterCertState("https://h:8443", "Incus", None, "PEM", "KEY", CURRENT, None)
    lxd.probe = IdentityProbe(True, False, None, "the cluster does not trust the current certificate yet")
    result = await trust_cleanup.remove_trust_for_deregistration(db, CLUSTER)
    assert result.commands == [] and result.complete is True
