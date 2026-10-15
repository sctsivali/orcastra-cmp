"""The browser fixtures in frontend/e2e speak the backend's real plan, job and settings contract.

The golden file is generated from the production presenters and planner helpers, never written
by hand, so a backend contract change fails here instead of leaving the synthetic browser suite
green against a shape the server no longer sends. After an intended change, regenerate it:

    UPDATE_MIGRATION_E2E_CONTRACT=1 python -m pytest tests/test_migration_e2e_contract.py
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.core.config import Settings
from app.models.migration_schemas import (
    MigrationJobResponse,
    MigrationPlanCreateRequest,
    MigrationPlanResponse,
)
from app.routers import system
from app.services.migrations import plan_contracts, presenters, target_contract
from app.services.migrations.preflight_instance import source_status as normalized_status
from app.services.migrations.preflight import PreflightResult

GOLDEN = Path(__file__).resolve().parents[2] / "frontend/e2e/support/migration-contract.json"
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
FUTURE = datetime(2099, 9, 22, 12, 0, tzinfo=timezone.utc)
FINGERPRINT = "a" * 64

SOURCE_INSTANCE: dict[str, Any] = {
    "name": "source-vm",
    "type": "container",
    "architecture": "x86_64",
    "profiles": ["default"],
    "config": {"limits.cpu": "2"},
    "expanded_config": {"limits.cpu": "2"},
    "devices": {},
    "expanded_devices": {
        "eth0": {"type": "nic", "network": "lxdbr0", "name": "eth0"},
        "root": {"type": "disk", "path": "/", "pool": "default"},
    },
}
TARGET_PROFILES = {
    "default": {"config": {}, "devices": {"root": {"type": "disk", "path": "/", "pool": "default"}}}
}
DAEMON = {
    "engine": "lxd",
    "family": "LXD",
    "version": "5.21.6",
    "clustered": True,
    "architectures": ["x86_64"],
    "extensions": [],
}


def _defaults() -> SimpleNamespace:
    return SimpleNamespace(
        **{
            name: field.get_default(call_default_factory=True)
            for name, field in Settings.model_fields.items()
        }
    )


def _settings(monkeypatch: Any) -> dict[str, Any]:
    monkeypatch.setattr(system, "get_settings", _defaults)
    return asyncio.run(system.get_system_settings()).model_dump(mode="json")


def _request(intent: str, mappings: dict[str, Any] | None = None) -> MigrationPlanCreateRequest:
    return MigrationPlanCreateRequest.model_validate(
        {
            "scope": "instance",
            "intent": intent,
            "source": {
                "cluster_id": "cluster-a",
                "project": "default",
                "resource_name": "source-vm",
                "member": "member-a",
                "cluster_fingerprint": FINGERPRINT,
            },
            "target": {
                "cluster_id": "cluster-b",
                "project": "target-project",
                "resource_name": "source-vm",
                "cluster_fingerprint": FINGERPRINT,
            },
            "retention_hours": 24,
            "checkpoint_requested": False,
            "external_cutover_required": False,
            "mappings": mappings or {},
        }
    )


def _plan(
    plan_id: str,
    *,
    intent: str,
    status: str,
    usable_networks: list[str],
    source_status: str,
) -> dict[str, Any]:
    request = _request(intent)
    status_value = normalized_status({"status": source_status})
    contract = target_contract.build_contract(
        source=SOURCE_INSTANCE,
        target_profiles=TARGET_PROFILES,
        profile_mapping={},
        network_mapping={},
        usable_networks=usable_networks,
        root_pool="default",
        intent=intent,
    )
    result = PreflightResult(
        strategy="cross_cluster_pull",
        downtime="cold" if status_value == "running" else "none",
        capabilities={
            "source": DAEMON,
            "target": DAEMON,
            "checkpoint": True,
            "rollback": intent == "move",
            "live": False,
            "incremental_refresh": False,
            "atomic_shadow_metadata_cutover": True,
            "project_dependencies_locked": True,
            "daemon_operation_inventory_verified": True,
            "daemon_operations_quiescent": True,
            "automatic_cleanup": True,
            "source_project_fingerprint": FINGERPRINT,
            "target_project_fingerprint": FINGERPRINT,
        },
    )
    result.resources.append(
        {
            "resource_type": "instance",
            "source_name": "source-vm",
            "target_name": "source-vm",
            "source_fingerprint": FINGERPRINT,
            "source_identity_fingerprint": FINGERPRINT,
            "source_status": status_value,
            "target_fingerprint": None,
            "target_identity_fingerprint": None,
            "contract": contract.summary(),
        }
    )
    for item in contract.unmapped_networks:
        result.block(
            "network_mapping_required",
            f"NIC '{item['device']}' uses '{item['source']}', which the target project "
            f"cannot use. Available networks: {', '.join(usable_networks) or 'none'}.",
            "Map the network to one of the available target networks.",
        )
    row = SimpleNamespace(
        plan_id=plan_id,
        revision=1,
        scope="instance",
        intent=intent,
        status=status,
        retention_hours=24,
        checkpoint_requested=False,
        external_cutover_required=False,
        preflight_payload=plan_contracts._preflight_payload(result, request),
        request_payload=request.model_dump(mode="json"),
        expires_at=FUTURE,
        created_at=NOW,
        updated_at=NOW,
        **{
            f"{side}_{key}": value
            for side, ref in (("source", request.source), ("target", request.target))
            for key, value in ref.model_dump().items()
        },
    )
    return presenters.plan_response(row).model_dump(mode="json")


def _job(phase: str, status: str, monkeypatch: Any) -> dict[str, Any]:
    monkeypatch.setattr(presenters, "get_settings", _defaults)
    row = SimpleNamespace(
        scope="instance",
        intent="move",
        strategy="cross_cluster_pull",
        phase=phase,
        status=status,
        lease_expires_at=None,
    )
    return MigrationJobResponse(
        job_id=f"job-{phase}",
        plan_id="plan-ready",
        plan_revision=1,
        revision=1,
        scope="instance",
        intent="move",
        source=_request("move").source,
        target=_request("move").target,
        strategy="cross_cluster_pull",
        downtime_class="cold",
        phase=phase,
        status=status,
        retention_hours=24,
        retention_deadline=FUTURE,
        external_cutover_required=False,
        available_actions=presenters._available_actions(row, []),
        created_at=NOW,
        updated_at=NOW,
        started_at=NOW,
    ).model_dump(mode="json")


def _contract(monkeypatch: Any) -> dict[str, Any]:
    return {
        "_generated_by": "backend/tests/test_migration_e2e_contract.py",
        "settings": _settings(monkeypatch),
        "plans": {
            "move_ready": _plan(
                "plan-ready", intent="move", status="ready",
                usable_networks=["lxdbr0"], source_status="Stopped",
            ),
            "move_network_mapping": _plan(
                "plan-network", intent="move", status="blocked",
                usable_networks=["lxdbr1"], source_status="Stopped",
            ),
            "copy_running": _plan(
                "plan-copy", intent="copy", status="ready",
                usable_networks=["lxdbr0"], source_status="Running",
            ),
        },
        "jobs": {
            "source_retained": _job("source_retained", "waiting", monkeypatch),
            "cleanup_blocked": _job("cleanup_blocked", "waiting", monkeypatch),
            "manual_recovery": _job("manual_recovery", "manual_recovery", monkeypatch),
        },
    }


def test_e2e_migration_contract_matches_the_backend(monkeypatch: Any) -> None:
    contract = _contract(monkeypatch)
    rendered = json.dumps(contract, indent=2, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_MIGRATION_E2E_CONTRACT") == "1":
        GOLDEN.write_text(rendered, encoding="utf-8")
    assert GOLDEN.exists(), "Run with UPDATE_MIGRATION_E2E_CONTRACT=1 to create the golden file."
    assert json.loads(GOLDEN.read_text(encoding="utf-8")) == contract, (
        "frontend/e2e/support/migration-contract.json is stale. Regenerate it with "
        "UPDATE_MIGRATION_E2E_CONTRACT=1 and realign the browser fixtures."
    )


def test_golden_contract_validates_against_the_response_models(monkeypatch: Any) -> None:
    contract = _contract(monkeypatch)
    for plan in contract["plans"].values():
        MigrationPlanResponse.model_validate(plan)
    for job in contract["jobs"].values():
        MigrationJobResponse.model_validate(job)
    system.SystemSettings.model_validate(contract["settings"])


def test_retained_move_offers_rollback_and_finalize(monkeypatch: Any) -> None:
    jobs = _contract(monkeypatch)["jobs"]
    assert jobs["source_retained"]["available_actions"] == ["rollback", "cleanup"]
    assert jobs["cleanup_blocked"]["available_actions"] == ["rollback", "cleanup", "release"]
    assert jobs["manual_recovery"]["available_actions"] == ["recheck", "release"]
