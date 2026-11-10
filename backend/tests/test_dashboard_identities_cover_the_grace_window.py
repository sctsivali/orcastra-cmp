"""The task journal keeps recognising the dashboard across a certificate rotation.

The identity set refreshes on an interval, so operations under a renewed certificate are not
attributed to an external actor. It keeps the superseded fingerprints still tracked for the
cluster, because workers that have not switched yet, and operations started before the switch,
still present the old one.
"""

from app.services.task_journal import follower_writer
from app.services.task_journal.follower_writer import DashboardIdentities
from tests._cert_rotation_fakes import make_cert
from app.core.cert_fingerprint import lxd_fingerprint

OLD_PEM, _ = make_cert("old")
NEW_PEM, _ = make_cert("new")
OLD, NEW = lxd_fingerprint(OLD_PEM), lxd_fingerprint(NEW_PEM)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


async def test_a_rotation_is_picked_up_after_the_refresh_interval(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(follower_writer.time, "monotonic", clock.monotonic)
    current = {"pem": OLD_PEM}
    tracked = set()

    async def read_tracked(host):
        return set(tracked)

    identities = DashboardIdentities(
        read_certificate=lambda host: {"client.crt": current["pem"]}, read_tracked=read_tracked
    )

    assert await identities.for_host("nuc1") == frozenset({OLD})
    current["pem"] = NEW_PEM
    tracked.add(OLD)  # the renewal records the old certificate as superseded
    assert await identities.for_host("nuc1") == frozenset({OLD}), "memoised inside the interval"

    clock.now += follower_writer.IDENTITY_REFRESH_S + 1
    assert await identities.for_host("nuc1") == frozenset({OLD, NEW}), (
        "the new certificate is recognised and the old one is kept while it is in its grace period"
    )

    tracked.clear()  # the old entry leaves the grace window as failed, so it is not claimed
    clock.now += follower_writer.IDENTITY_REFRESH_S + 1
    assert await identities.for_host("nuc1") == frozenset({NEW}), "rebuilt, not only grown"


async def test_tracked_superseded_fingerprints_count_as_the_dashboard():
    async def tracked(host):
        return {OLD}

    identities = DashboardIdentities(
        read_certificate=lambda host: {"client.crt": NEW_PEM}, read_tracked=tracked
    )
    assert await identities.for_host("nuc1") == frozenset({OLD, NEW})


async def test_a_failed_refresh_keeps_the_last_known_set(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(follower_writer.time, "monotonic", clock.monotonic)
    state = {"fail": False}

    def read(host):
        if state["fail"]:
            raise RuntimeError("vault sealed")
        return {"client.crt": OLD_PEM}

    identities = DashboardIdentities(read_certificate=read)
    assert await identities.for_host("nuc1") == frozenset({OLD})
    state["fail"] = True
    clock.now += follower_writer.IDENTITY_REFRESH_S + 1
    assert await identities.for_host("nuc1") == frozenset({OLD})


async def test_a_failed_superseded_certificate_is_not_counted_as_the_dashboard():
    from tests._cert_rotation_fakes import SessionMaker
    from app.services.lxd_certs import trust_cleanup_store as store

    db = SessionMaker().db
    for fp, state in (("a" * 64, "pending"), ("b" * 64, "failed"), ("c" * 64, "removed"), ("d" * 64, "cancelled")):
        row = await store.record_superseded(db, cluster_id="nuc1", fingerprint=fp, reason="renew", grace_seconds=600)
        row.state = state
        if state == "removed":
            row.removed_at = store.utcnow()
    db.sync.commit()
    assert await store.tracked_fingerprints(db, "nuc1") == {"a" * 64, "c" * 64}
