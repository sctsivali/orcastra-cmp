"""Operational events: one record per transition, with its own severity, summary and fix.

They are audit records (category "operations", action system.<code>), so they reach the deployment's
own OpenSearch in orcastra-audit-* and the console's Cluster Log. These tests pin what one record
says, that a change is reported once across workers (with Redis, and with Redis down through the
derived event id), and that a cluster event names its cluster.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import pytest

from app.services import activity_logger as audit
from app.services import ops_events
from app.services.task_journal.sink_mapping import record_to_log_row


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: List[Dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        data = getattr(record, "audit_log_data", None)
        if data is not None:
            self.records.append(data)


class _Redis:
    """SET key value EX GET, shared by every 'worker' that holds it."""

    def __init__(self) -> None:
        self.store: Dict[str, str] = {}

    def set(self, key: str, value: str, ex: Optional[int] = None, get: bool = False):
        previous = self.store.get(key)
        self.store[key] = value
        return previous.encode() if previous is not None else None


@pytest.fixture
def captured(monkeypatch):
    handler = _Capture()
    target = audit.get_activity_logger().logger
    level = target.level
    target.setLevel(logging.INFO)
    target.addHandler(handler)
    monkeypatch.setattr(ops_events, "_redis", lambda: None)
    ops_events.reset()
    yield handler.records
    target.removeHandler(handler)
    target.setLevel(level)
    ops_events.reset()


def test_a_firing_event_is_one_audit_record_with_its_fix(captured):
    assert ops_events.fire("vault.token_refused") is True
    (record,) = captured
    assert record["action"] == "system.vault.token_refused"
    assert record["category"] == "operations" and record["severity"] == "critical"
    assert record["target"]["type"] == "deployment" and record["target"]["host"] is None
    details = record["details"]
    assert details["state"] == "firing" and details["event_code"] == "vault.token_refused"
    assert "-period=720h" in details["fix"]
    assert details["docs_url"].endswith("/operations/operational-events/#vault-token-refused")


def test_the_cluster_log_row_carries_the_summary_the_fix_and_the_severity(captured):
    ops_events.fire("vault.token_refused")
    row = record_to_log_row(captured[0])
    assert row["severity"] == "error" and row["cluster_id"] is None
    assert row["message"].startswith("Vault rejects the dashboard's token")
    assert "Fix: Create a periodic token" in row["message"]


def test_a_holding_fault_is_reported_once_and_a_cleared_one_once(captured):
    assert ops_events.fire("vault.token_refused") is True
    assert ops_events.fire("vault.token_refused") is False
    assert ops_events.resolve("vault.token_refused") is True
    assert ops_events.resolve("vault.token_refused") is False
    assert [r["details"]["state"] for r in captured] == ["firing", "resolved"]
    resolved = record_to_log_row(captured[1])
    assert resolved["severity"] == "info" and resolved["message"] == "Vault accepts the dashboard's token again."


def test_resolving_what_never_fired_reports_nothing(captured):
    assert ops_events.resolve("vault.token_expiring") is False
    assert captured == []


def test_a_rising_severity_is_a_new_report(captured):
    facts = {"cluster": "qa-c1", "not_after": "2026-12-01", "days": 10}
    assert ops_events.fire("cluster.certificate_expiring", cluster_id="qa-c1", facts=facts) is True
    assert ops_events.fire("cluster.certificate_expiring", cluster_id="qa-c1", facts=facts) is False
    facts["days"] = 2
    assert ops_events.fire("cluster.certificate_expiring", cluster_id="qa-c1", severity="critical", facts=facts) is True
    rows = [record_to_log_row(r) for r in captured]
    assert [r["severity"] for r in rows] == ["warning", "error"]
    assert rows[1]["cluster_id"] == "qa-c1" and rows[1]["resource_type"] == "cluster"
    assert "cluster qa-c1 expires on 2026-12-01 (in 2 days)" in rows[1]["message"]


def test_a_cluster_event_needs_its_cluster_and_a_deployment_event_none(captured):
    with pytest.raises(ValueError):
        ops_events.fire("cluster.certificate_expired")
    with pytest.raises(ValueError):
        ops_events.fire("vault.token_refused", cluster_id="qa-c1")


def test_two_workers_sharing_redis_report_a_change_once(captured, monkeypatch):
    shared = _Redis()
    monkeypatch.setattr(ops_events, "_redis", lambda: shared)
    first = ops_events.fire("vault.token_refused")
    ops_events.reset()  # the second worker's own memory is empty
    second = ops_events.fire("vault.token_refused")
    assert (first, second) == (True, False)
    assert len(captured) == 1


def test_with_redis_down_two_workers_derive_the_same_event_id(captured):
    ops_events.fire("vault.token_refused")
    ops_events.reset()
    ops_events.fire("vault.token_refused")
    assert len(captured) == 2
    assert captured[0]["event_id"] == captured[1]["event_id"], "the journal and OpenSearch collapse the pair"


def test_a_history_only_event_is_recorded_every_time(captured):
    ops_events.record("vault.token_renewed", facts={"lease": "30 days"})
    ops_events.record("vault.token_renewed", facts={"lease": "30 days"})
    assert [r["details"]["state"] for r in captured] == ["recorded", "recorded"]
    row = record_to_log_row(captured[0])
    assert row["severity"] == "info" and row["message"] == "The dashboard renewed its Vault token; it now lasts 30 days."


def test_a_missing_fact_reads_unknown_rather_than_failing(captured):
    ops_events.fire("vault.token_expiring")
    assert "expires on unknown" in captured[0]["details"]["summary"]


def test_no_detail_key_is_one_the_audit_scrubber_would_hide(captured):
    ops_events.fire("vault.token_expiring", facts={"expire_time": "2026-12-11T00:00:00Z", "ttl_seconds": 600, "renewable": False})
    details = captured[0]["details"]
    assert "[REDACTED]" not in str(details)
    assert details["expire_time"] == "2026-12-11T00:00:00Z" and details["ttl_seconds"] == 600


def test_every_catalogue_code_has_an_audit_category_and_a_description():
    from app.services.activity_catalogue import ACTION_CATEGORIES, EventCategory
    from app.services.task_journal.vocabulary import ACTION_DESCRIPTIONS

    for code in ops_events.CATALOGUE:
        action = ops_events.action_for(code)
        assert ACTION_CATEGORIES.get(action) == EventCategory.OPERATIONS, action
        assert action in ACTION_DESCRIPTIONS, action


def test_an_expired_certificate_renewed_reports_one_recovery(captured):
    facts = {"cluster": "qa-c1", "not_after": "2026-11-11", "days": 0}
    ops_events.fire("cluster.certificate_expiring", cluster_id="qa-c1", facts=facts)
    ops_events.clear("cluster.certificate_expiring", cluster_id="qa-c1")
    ops_events.fire("cluster.certificate_expired", cluster_id="qa-c1", facts=facts)
    renewed = {"cluster": "qa-c1", "not_after": "2026-12-11", "days": 30}
    assert ops_events.resolve("cluster.certificate_expiring", cluster_id="qa-c1", facts=renewed) is False
    assert ops_events.resolve("cluster.certificate_expired", cluster_id="qa-c1", facts=renewed) is True
    states = [(r["action"], r["details"]["state"]) for r in captured]
    assert states == [
        ("system.cluster.certificate_expiring", "firing"),
        ("system.cluster.certificate_expired", "firing"),
        ("system.cluster.certificate_expired", "resolved"),
    ]
