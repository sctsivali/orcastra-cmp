"""Every path that lets go of a client certificate writes it down before Vault forgets it.

Trust entries piled up on the clusters because nothing remembered which certificates the dashboard
had replaced. The record is only useful if it is written before the overwrite: written after, a
failed overwrite leaves nothing, and a racing renewal leaves a trusted certificate no one names.
"""

import asyncio
from datetime import timedelta

import pytest

from app.core.cert_fingerprint import lxd_fingerprint
from app.models.schemas import CertificateRenewalRequest
import re

from app.services.lxd_certs import pending, rotation
from app.services.lxd_certs import trust_cleanup_store as store
from tests._cert_rotation_fakes import FakeCertManager, SessionMaker, rows


@pytest.fixture
def db():
    return SessionMaker().db


@pytest.fixture(autouse=True)
def _quiet(monkeypatch, db):
    audits = []
    # The records commit on their own short transaction; here it is the same SQLite database.
    monkeypatch.setattr(rotation, "_default_session_maker", lambda: SessionMaker(db))
    monkeypatch.setattr(rotation, "_adopt", lambda *a, **k: None)
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: None)
    def record(action, cid, result="success", **d):
        audits.append((action, d))

    monkeypatch.setattr(rotation, "audit", record)
    monkeypatch.setattr(pending, "audit", record)
    return audits


def _renew(manager, **kwargs):
    return CertificateRenewalRequest(cluster_id=manager.cluster_id, **kwargs)


async def test_an_automatic_renewal_records_the_old_certificate_and_activates_the_new(db, _quiet):
    manager = FakeCertManager()
    old_fp = lxd_fingerprint(manager.current[0])

    result = await rotation.renew(db, manager, _renew(manager))

    new_cert = manager.vault.data[manager.path("certs")]["client.crt"]
    assert result.auto_renewed and not result.requires_manual_trust
    assert result.previous_fingerprint == old_fp
    assert result.fingerprint == lxd_fingerprint(new_cert) != old_fp
    [row] = rows(db)
    assert (row.fingerprint, row.reason, row.state) == (old_fp, "renew", "pending")
    assert row.due_at - row.superseded_at >= timedelta(seconds=599), "the grace period is applied"
    assert manager.path("pending_certs") not in manager.vault.data
    assert [a for a, _ in _quiet] == ["cluster.certificate.renew", "cluster.certificate.activate"]


async def test_a_renewal_that_replaces_a_pending_one_records_the_pending_certificate(db):
    manager = FakeCertManager()
    pending_fp = lxd_fingerprint(manager.add_pending(identity_name="orca-nuc9-20261001-010101"))

    await rotation.renew(db, manager, _renew(manager))

    by_reason = {row.reason: row for row in rows(db)}
    assert by_reason["pending_replaced"].fingerprint == pending_fp
    assert by_reason["pending_replaced"].identity_name == "orca-nuc9-20261001-010101"
    assert "renew" in by_reason


async def test_a_manual_renewal_records_nothing_until_it_is_verified(db):
    manager = FakeCertManager()
    manager.trust_with_existing = False

    result = await rotation.renew(db, manager, _renew(manager))

    pending = manager.vault.data[manager.path("pending_certs")]
    assert result.requires_manual_trust and not result.auto_renewed
    assert result.fingerprint == lxd_fingerprint(pending["client.crt"])
    assert "config trust add" in result.cli_command and pending["identity_name"] in result.cli_command
    assert result.pending_cert_id == pending["pending_cert_id"]
    assert len(result.pending_cert_id.rsplit("-", 1)[-1]) == 8, "a random suffix, not the second"
    assert rows(db) == [], "the active certificate is still in use"
    assert manager.vault.data[manager.path("certs")]["client.crt"] == manager.current[0]


async def test_a_failed_activation_records_the_new_certificate_too(db):
    manager = FakeCertManager()
    old_fp = lxd_fingerprint(manager.current[0])
    manager.vault.fail_writes_to = "/certs"

    with pytest.raises(Exception):
        await rotation.renew(db, manager, _renew(manager))

    new_fp = lxd_fingerprint(manager.vault.issued[0][0])
    by_fp = {row.fingerprint: row.reason for row in rows(db)}
    assert by_fp == {old_fp: "renew", new_fp: "renew_aborted"}, (
        "trust was added for the new certificate, so it must be remembered even though it never "
        "became active; the old one is still current and the sweep cancels its row"
    )
    assert manager.vault.data[manager.path("certs")]["client.crt"] == manager.current[0]


async def test_verify_records_the_old_certificate_and_promotes_the_pending_one(db, _quiet):
    manager = FakeCertManager()
    old_fp = lxd_fingerprint(manager.current[0])
    pending_cert = manager.add_pending(pending_cert_id="pending-x")

    result = await rotation.verify(db, manager, manager.cluster_id, "pending-x")

    assert result.verified and result.activated
    assert result.fingerprint == lxd_fingerprint(pending_cert)
    assert manager.vault.data[manager.path("certs")]["client.crt"] == pending_cert
    assert manager.path("pending_certs") not in manager.vault.data
    [row] = rows(db)
    assert (row.fingerprint, row.reason) == (old_fp, "verify")


async def test_an_untrusted_pending_certificate_changes_nothing(db):
    manager = FakeCertManager()
    manager.add_pending()
    manager.verified = False

    result = await rotation.verify(db, manager, manager.cluster_id, None)

    assert not result.verified and not result.activated and result.fingerprint
    assert rows(db) == []
    assert manager.vault.data[manager.path("certs")]["client.crt"] == manager.current[0]


async def test_verify_refuses_a_pending_certificate_that_was_replaced(db):
    manager = FakeCertManager()
    manager.add_pending(pending_cert_id="pending-new")

    with pytest.raises(rotation.PendingMismatch):
        await rotation.verify(db, manager, manager.cluster_id, "pending-old")
    assert rows(db) == []


async def test_cancel_remembers_the_pending_certificate_whose_trust_may_exist(db):
    manager = FakeCertManager()
    pending_fp = lxd_fingerprint(manager.add_pending(identity_name="orca-nuc9-x"))

    assert await rotation.cancel(db, manager, manager.cluster_id) is True

    [row] = rows(db)
    assert (row.fingerprint, row.reason, row.identity_name) == (pending_fp, "cancel", "orca-nuc9-x")
    assert manager.path("pending_certs") not in manager.vault.data


async def test_cancel_without_a_pending_certificate_records_nothing(db):
    manager = FakeCertManager()
    assert await rotation.cancel(db, manager, manager.cluster_id) is False
    assert rows(db) == []


async def test_regenerate_records_the_certificate_it_overwrites(db):
    manager = FakeCertManager()
    async with store.cluster_cert_lock(db, manager.cluster_id):
        previous = await pending.record_current_superseded(db, manager, manager.cluster_id, reason="regenerate")
    assert previous == lxd_fingerprint(manager.current[0])
    assert [(r.fingerprint, r.reason) for r in rows(db)] == [(previous, "regenerate")]


async def test_a_second_change_on_the_same_cluster_waits_its_turn(db):
    manager = FakeCertManager()
    held = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with store.cluster_cert_lock(db, manager.cluster_id):
            held.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await held.wait()
    with pytest.raises(store.CertificateOperationBusy):
        await rotation.renew(db, manager, _renew(manager))
    release.set()
    await task
    assert manager.vault.issued == [], "nothing was minted while another change held the cluster"


async def test_a_missing_certificate_is_not_found(db):
    manager = FakeCertManager()
    del manager.vault.data[manager.path("certs")]
    with pytest.raises(rotation.CertificateNotFound):
        await rotation.renew(db, manager, _renew(manager))


async def test_the_same_certificate_recorded_twice_is_one_row(db):
    fp = "b" * 64
    for reason in ("cancel", "renew"):
        await store.record_superseded(db, cluster_id="c1", fingerprint=fp, reason=reason, grace_seconds=600)
    [row] = rows(db)
    assert row.reason == "renew" and row.state == "pending"


async def test_a_malformed_fingerprint_is_never_stored(db):
    assert await store.record_superseded(db, cluster_id="c1", fingerprint="7e8ffe6ae3b6", reason="renew", grace_seconds=600) is None
    assert rows(db) == []


IDENTITY = re.compile(r"^orca-nuc9-\d{8}-\d{9}-[a-z0-9]{4}$")


async def test_regenerate_issues_a_pending_certificate_and_leaves_the_active_one(db, _quiet):
    manager = FakeCertManager()
    issued = await pending.regenerate(db, manager, manager.cluster_id, cluster_type="LXD")

    active = manager.vault.data[manager.path("certs")]["client.crt"]
    waiting = manager.vault.data[manager.path("pending_certs")]
    assert active == manager.current[0], "the active certificate keeps working until Verify"
    assert waiting["origin"] == "regenerate" and waiting["pending_cert_id"] == issued.pending_cert_id
    assert issued.fingerprint == lxd_fingerprint(waiting["client.crt"]) != lxd_fingerprint(active)
    assert issued.previous_fingerprint == lxd_fingerprint(active) and not issued.activated
    assert IDENTITY.match(issued.identity_name), issued.identity_name
    assert issued.cli_command.startswith(f'cat <<EOF | lxc config trust add - --name="{issued.identity_name}"')
    assert rows(db) == [], "nothing is replaced yet"
    assert [a for a, _ in _quiet] == ["cluster.certificate.regenerate"]


async def test_verify_after_regenerate_records_the_old_certificate_as_regenerated(db):
    manager = FakeCertManager()
    old_fp = lxd_fingerprint(manager.current[0])
    issued = await pending.regenerate(db, manager, manager.cluster_id, cluster_type="Incus")
    assert "incus config trust add-certificate" in issued.cli_command

    result = await rotation.verify(db, manager, manager.cluster_id, issued.pending_cert_id)

    assert result.activated and result.fingerprint == issued.fingerprint
    assert [(r.fingerprint, r.reason) for r in rows(db)] == [(old_fp, "regenerate")]


async def test_a_second_regenerate_remembers_the_pending_one_it_replaces(db):
    manager = FakeCertManager()
    first = await pending.regenerate(db, manager, manager.cluster_id, cluster_type="LXD")
    second = await pending.regenerate(db, manager, manager.cluster_id, cluster_type="LXD")
    assert first.pending_cert_id != second.pending_cert_id
    assert [(r.fingerprint, r.reason) for r in rows(db)] == [(first.fingerprint, "pending_replaced")]


async def test_regenerate_with_no_certificate_at_all_activates_at_once(db):
    manager = FakeCertManager()
    del manager.vault.data[manager.path("certs")]
    issued = await pending.regenerate(db, manager, manager.cluster_id, cluster_type="LXD")
    assert issued.activated and issued.previous_fingerprint is None
    assert manager.vault.data[manager.path("certs")]["client.crt"] == issued.certificate
    assert manager.path("pending_certs") not in manager.vault.data


async def test_renew_names_its_trust_entry_the_way_registration_does(db):
    manager = FakeCertManager()
    manager.trust_with_existing = False
    result = await rotation.renew(db, manager, _renew(manager))
    name = manager.vault.data[manager.path("pending_certs")]["identity_name"]
    assert IDENTITY.match(name) and f'--name="{name}"' in result.cli_command


def test_the_identity_name_shape_is_stable():
    from datetime import datetime, timezone

    name = rotation.trust_identity_name("nuc9", datetime(2026, 10, 7, 9, 41, 54, 353000, tzinfo=timezone.utc))
    assert name.startswith("orca-nuc9-20261007-094154353-") and len(name.rsplit("-", 1)[-1]) == 4
