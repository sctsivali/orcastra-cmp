from __future__ import annotations

from types import SimpleNamespace

from app.models.migration_schemas import MigrationPlanCreateRequest
from app.services.migrations import plan_contracts, preflight
from app.services.migrations.security import (
    approved_build,
    approved_lxd_native_cross_project_move,
)


def _identity(*, engine: str = "lxd", version: str = "6.10") -> dict[str, str]:
    return {"engine": engine, "version": version}


def test_general_migration_build_baseline_has_no_disable_switch() -> None:
    # A patched build passes the series policy, an unpatched one has no switch to pass it.
    assert approved_build(
        _identity(version="6.9"),
        SimpleNamespace(
            migration_require_approved_build=False,
            migration_approved_builds="",
        ),
    ) is False
    assert approved_build(_identity(version="6.10"), SimpleNamespace()) is True
    assert approved_build(
        _identity(version="6.9"),
        SimpleNamespace(migration_approved_builds="lxd:6.9"),
    ) is True
    assert approved_build(
        _identity(version="6.10"),
        SimpleNamespace(migration_build_deny="lxd:6.10"),
    ) is False


def test_native_lxd_cross_project_move_requires_its_own_exact_build_approval() -> None:
    settings = SimpleNamespace(
        migration_approved_builds="lxd:6.10",
        migration_lxd_native_cross_project_move_approved_builds="",
    )

    assert approved_lxd_native_cross_project_move(_identity(), settings) is False


def test_native_lxd_cross_project_move_does_not_guess_from_advisory_version() -> None:
    settings = SimpleNamespace(
        migration_lxd_native_cross_project_move_approved_builds="lxd:5.21.6"
    )

    assert approved_lxd_native_cross_project_move(_identity(version="6.10"), settings) is False
    assert approved_lxd_native_cross_project_move(_identity(version="5.21.6"), settings) is True
    assert (
        approved_lxd_native_cross_project_move(
            _identity(engine="incus", version="5.21.6"), settings
        )
        is False
    )
    assert (
        approved_lxd_native_cross_project_move(
            _identity(version=""),
            SimpleNamespace(
                migration_lxd_native_cross_project_move_approved_builds="lxd:"
            ),
        )
        is False
    )


def _plan_request() -> MigrationPlanCreateRequest:
    return MigrationPlanCreateRequest.model_validate(
        {
            "scope": "instance",
            "intent": "move",
            "source": {
                "cluster_id": "cluster-a",
                "project": "source-project",
                "resource_name": "workload",
            },
            "target": {
                "cluster_id": "cluster-a",
                "project": "target-project",
                "resource_name": "workload",
            },
        }
    )


def _result(*, security_approved: bool) -> preflight.PreflightResult:
    return preflight.PreflightResult(
        strategy="same_cluster_copy",
        downtime="cold",
        capabilities={
            "source": {"engine": "lxd"},
            "target": {"engine": "lxd"},
            "checkpoint": True,
            "rollback": True,
            "live": False,
            "incremental_refresh": False,
            "lxd_native_cross_project_move_applicable": True,
            "lxd_native_cross_project_move_security_approved": security_approved,
            "lxd_native_cross_project_move_selected": False,
        },
    )


def test_plan_never_selects_native_move_even_for_an_approved_build() -> None:
    payload = plan_contracts._preflight_payload(
        _result(security_approved=True), _plan_request()
    )
    capability = next(
        item
        for item in payload["capabilities"]
        if item["code"] == "lxd_native_cross_project_move"
    )

    assert payload["strategy"] == "same_cluster_copy"
    assert capability["supported"] is False
    assert "still selects staged copy" in capability["reason"]
    assert (
        payload["daemon_capabilities"][
            "lxd_native_cross_project_move_selected"
        ]
        is False
    )


def test_plan_explains_the_fail_closed_unapproved_baseline() -> None:
    payload = plan_contracts._preflight_payload(
        _result(security_approved=False), _plan_request()
    )
    capability = next(
        item
        for item in payload["capabilities"]
        if item["code"] == "lxd_native_cross_project_move"
    )

    assert capability["supported"] is False
    assert "fail-closed" in capability["reason"]


def test_preflight_gate_keeps_staged_copy_when_native_move_is_unapproved() -> None:
    result = _result(security_approved=False)

    preflight._apply_lxd_native_cross_project_move_gate(
        result,
        intent="move",
        same_cluster=True,
        source_project="source-project",
        target_project="target-project",
        source_identity={"engine": "lxd", "version": "6.10"},
        settings=SimpleNamespace(
            migration_lxd_native_cross_project_move_approved_builds=""
        ),
    )

    assert result.strategy == "same_cluster_copy"
    assert result.capabilities["lxd_native_cross_project_move_selected"] is False
    assert result.capabilities["lxd_native_cross_project_move_security_approved"] is False
    # The native path is never selected, so its build gate does not warn on every plan.
    assert result.warnings == []


def test_preflight_gate_is_quiet_outside_native_lxd_cross_project_move() -> None:
    result = _result(security_approved=False)

    preflight._apply_lxd_native_cross_project_move_gate(
        result,
        intent="move",
        same_cluster=False,
        source_project="source-project",
        target_project="target-project",
        source_identity={"engine": "lxd", "version": "6.10"},
        settings=SimpleNamespace(
            migration_lxd_native_cross_project_move_approved_builds=""
        ),
    )

    assert result.capabilities["lxd_native_cross_project_move_applicable"] is False
    assert result.warnings == []
