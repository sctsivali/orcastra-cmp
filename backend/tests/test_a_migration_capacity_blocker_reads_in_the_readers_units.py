"""The one migration blocker that names sizes reads in each reader's units, and the approval hash
does not depend on who planned or approved.

The preflight report is hashed for approval, and approval runs preflight again in the approver's
own request. So the stored message keeps decimal whatever the request asked for, and the byte
counts are stored beside it for the plan view to render per reader.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.units import reset_byte_units, use_byte_units
from app.services.migrations import plan_contracts, preflight, presenters

GiB = 1024**3


def _blocked(monkeypatch: pytest.MonkeyPatch, pool: str = "fast") -> preflight.PreflightResult:
    monkeypatch.setattr(preflight, "_instance_transfer_bytes", lambda *_a: 60 * GiB)
    monkeypatch.setattr(preflight, "_bound_unmeasured_footprints", lambda *_a, **_k: ({"dirwork": 10 * GiB}, []))
    monkeypatch.setattr(preflight, "_storage_pools", lambda _client: [pool])
    monkeypatch.setattr(preflight.mobility, "pool_headroom", lambda *_a: {"total": 50 * GiB, "used": 0, "free": 50 * GiB})
    result = preflight.PreflightResult(strategy="cross_cluster_copy", downtime="cold")
    preflight._check_capacity(result, source_client=object(), target_client=object(), source_project="qa",
                              instance_names=["workload"], target_pool=pool)
    return result


def _blocker(result: preflight.PreflightResult) -> dict:
    return next(b for b in result.blockers if b["code"] == "target_pool_insufficient_capacity")


def test_the_stored_message_is_decimal_whatever_the_request_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    token = use_byte_units("binary")
    try:
        blocker = _blocker(_blocked(monkeypatch))
    finally:
        reset_byte_units(token)
    assert blocker["message"] == (
        "Target pool 'fast' has 53.69 GB free, and the measured part of the transfer alone needs 64.42 GB."
    )
    assert blocker["sized"]["sizes"] == {"free": 50 * GiB, "needed": 60 * GiB}
    assert blocker["sized"]["texts"] == {"pool": "fast"}


def test_the_approval_hash_is_the_same_for_both_systems(monkeypatch: pytest.MonkeyPatch) -> None:
    hashes = []
    for units in ("decimal", "binary"):
        token = use_byte_units(units)
        try:
            report = {"blockers": [{**b, "severity": "blocker"} for b in _blocked(monkeypatch).blockers]}
            hashes.append(plan_contracts.approval_report_hash(report))
        finally:
            reset_byte_units(token)
    assert hashes[0] == hashes[1]


def _row(issue: dict) -> SimpleNamespace:
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    sides = {
        f"{side}_{field}": value
        for side in ("source", "target")
        for field, value in (("cluster_id", "c1"), ("project", "qa"), ("resource_name", None),
                             ("member", None), ("cluster_fingerprint", None))
    }
    return SimpleNamespace(
        plan_id="p1", revision=1, scope="instance", intent="copy", status="draft",
        preflight_payload={"strategy": "cross_cluster_copy", "blockers": [issue], "warnings": []},
        request_payload={}, retention_hours=24, checkpoint_requested=False,
        external_cutover_required=False, expires_at=now + timedelta(hours=1), created_at=now,
        updated_at=now, approved_at=None, approved_by=None, created_by="sub-1", **sides,
    )


def test_the_plan_view_renders_the_sizes_for_its_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    issue = {**_blocker(_blocked(monkeypatch, pool="{free}")), "severity": "blocker"}
    token = use_byte_units("binary")
    try:
        shown = presenters.plan_response(_row(issue)).blockers[0]
    finally:
        reset_byte_units(token)
    assert shown.message == (
        "Target pool '{free}' has 50 GiB free, and the measured part of the transfer alone needs 60 GiB."
    ), "a pool name is a name, never a size slot"

    decimal = presenters.plan_response(_row(issue)).blockers[0]
    assert "53.69 GB free" in decimal.message


def test_a_stored_issue_from_before_keeps_its_text() -> None:
    issue = {"code": "target_pool_insufficient_capacity", "severity": "blocker",
             "message": "Target pool 'fast' has 1 GB free.", "remediation": ""}
    token = use_byte_units("binary")
    try:
        shown = presenters.plan_response(_row(issue)).blockers[0]
    finally:
        reset_byte_units(token)
    assert shown.message == "Target pool 'fast' has 1 GB free."
