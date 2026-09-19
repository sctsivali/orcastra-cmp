"""A lifecycle frame from an `lxc` shell names the shell user; the dashboard's own certificate is
the dashboard; a browser never sees the requestor at all.

LXD reports who asked in the event's `requestor`: `unix` for the local socket, `tls` with the
client's certificate fingerprint for a remote client. The follower writes the log row with that
actor and attaches it to the operation the stream already recorded, so an `lxc start` on the
host reads `root` / `cli` in the panel while a dashboard start keeps the audit record's actor.
"""

import base64
import hashlib
import json
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.services.lxd import events_hub  # noqa: E402
from app.services.task_journal import follower_frames  # noqa: E402
from app.services.task_journal.follower_writer import (  # noqa: E402
    DashboardIdentities,
    JournalWriter,
    fingerprint,
)
from app.services.task_journal.repository_types import CorrelationKey  # noqa: E402
from tests._journal_fakes import FakeSessionMaker  # noqa: E402

DER = b"\x30\x82\x01\x00fake-der-certificate-bytes"
PEM = (
    "-----BEGIN CERTIFICATE-----\n"
    + base64.encodebytes(DER).decode("ascii")
    + "-----END CERTIFICATE-----\n"
)
DASHBOARD_FP = hashlib.sha256(DER).hexdigest()


def _frame(requestor, action="instance-started", name="web-01"):
    md = {"action": action, "name": name, "project": "cust-a", "source": f"/1.0/instances/{name}",
          "requestor": requestor, "context": {"command": ["sh"], "type": "container"}}
    raw = json.dumps({"type": "lifecycle", "timestamp": "2026-09-19T10:00:02.5Z", "metadata": md})
    return events_hub.LXDEventsHub._parse_event("nuc1", raw)


def test_the_fingerprint_is_the_sha256_of_the_der_bytes_lower_cased():
    assert fingerprint(PEM) == DASHBOARD_FP
    assert fingerprint(PEM) == fingerprint(PEM).lower()


def test_a_unix_requestor_is_an_external_cli_actor_named_by_its_username():
    row, key, actor = follower_frames.lifecycle_row(
        "nuc1", _frame({"protocol": "unix", "username": "root", "address": "@"}), frozenset({DASHBOARD_FP})
    )
    assert actor == ("external", "cli", "root")
    assert row["actor_kind"] == "external" and row["actor_email"] == "root"
    assert row["kind"] == "lifecycle" and row["severity"] == "info"
    assert row["message"] == "Instance web-01 started"
    assert row["details"]["requestor"] == {"protocol": "unix", "username": "root", "address": "@"}
    assert row["details"]["source"] == "/1.0/instances/web-01"
    assert row["details"]["context"] == {"type": "container"}, "never the command line"
    assert row["event_id"].startswith("lifecycle:nuc1:")
    assert key == CorrelationKey("nuc1", "cust-a", "instance", "web-01", "instance.start")


def test_the_dashboard_certificate_is_the_dashboard_itself():
    requestor = {"protocol": "tls", "username": DASHBOARD_FP.upper(), "address": "10.0.0.5:1234"}
    row, key, actor = follower_frames.lifecycle_row("nuc1", _frame(requestor), frozenset({DASHBOARD_FP}))
    assert actor == ("system", "events", None)
    assert row["actor_email"] is None, "the audit record supplies the person"
    assert key is not None


def test_a_foreign_certificate_is_an_external_client():
    requestor = {"protocol": "tls", "username": "abcd" * 16, "address": "10.0.0.9:2"}
    _row, _key, actor = follower_frames.lifecycle_row("nuc1", _frame(requestor), frozenset({DASHBOARD_FP}))
    assert actor == ("external", "events", "abcd" * 16)


def test_a_lifecycle_without_a_requestor_is_a_system_event_with_no_key_when_unmapped():
    row, key, actor = follower_frames.lifecycle_row(
        "nuc1", _frame(None, action="cluster-member-joined", name=None), frozenset()
    )
    assert actor == ("system", "events", None)
    assert key is None and row["resource_name"] is None
    assert row["message"] == "Cluster member joined"


async def test_the_writer_inserts_the_row_then_attaches_the_requestor_on_the_mapped_key(monkeypatch):
    identities = DashboardIdentities(read_certificate=lambda host: {"client.crt": PEM})
    maker = FakeSessionMaker()
    writer = JournalWriter(maker, identities=identities)

    await writer.write_batch([_frame({"protocol": "unix", "username": "root", "address": "@"})])

    calls = maker.sessions[0].calls
    insert = next(p for sql, p in calls if sql.startswith("INSERT INTO cluster_log"))
    assert insert[0]["actor_kind"] == "external" and insert[0]["actor_email"] == "root"
    attach_sql, attach = next((sql, p) for sql, p in calls if "jsonb_build_object('requestor'" in sql)
    assert "actor_email IS NULL" in attach_sql and "kind = 'lxd_operation'" in attach_sql
    assert attach["actor_kind"] == "external" and attach["source"] == "cli"
    assert attach["actor_email"] == "root" and attach["window_s"] == 120.0
    assert attach["cluster_id"] == "nuc1" and attach["action"] == "instance.start"
    assert attach["resource_name"] == "web-01" and attach["project"] == "cust-a"
    assert maker.sessions[0].commits == 1


async def test_the_dashboard_identity_is_read_once_off_the_loop_and_a_failure_is_not_fatal():
    reads = []

    def read(host):
        reads.append(host)
        if host == "broken":
            raise RuntimeError("vault sealed")
        return {"client.crt": PEM}

    identities = DashboardIdentities(read_certificate=read)
    assert await identities.for_host("nuc1") == frozenset({DASHBOARD_FP})
    assert await identities.for_host("nuc1") == frozenset({DASHBOARD_FP})
    assert reads == ["nuc1"], "memoised per cluster"
    assert await identities.for_host("broken") == frozenset()
    assert await identities.for_host("broken") == frozenset()
    assert reads == ["nuc1", "broken"], "a failed read is not retried inside the minute"


def test_the_browser_projection_never_carries_the_requestor():
    payload = _frame({"protocol": "unix", "username": "root", "address": "@"})
    browser = {k: v for k, v in payload.items() if k in events_hub.BROWSER_FRAME_KEYS}
    assert "requestor" not in browser and "context" not in browser and "name" not in browser
    assert browser["instance"] == "web-01"


@pytest.mark.parametrize("bad", [None, "", "not-a-mapping"])
def test_a_missing_requestor_is_a_system_event(bad):
    _row, _key, actor = follower_frames.lifecycle_row("nuc1", _frame(bad), frozenset())
    assert actor[0] == "system"
