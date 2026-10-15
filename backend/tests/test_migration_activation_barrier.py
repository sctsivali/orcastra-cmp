"""The activation barriers refuse a target that drifted from its approved contract.

Live QA could not inject this deterministically: LXD locks the target while a transfer runs and
the gap before validation is under a second. So the barrier itself is pinned here: a target that
gained a setting, lost the job marker, or changed state is never activated.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

import pytest

from app.services.migrations import activation, executor
from app.services.migrations.contracts import MigrationUnknown
from app.services.migrations.project_policy import MIGRATION_JOB_MARKER
from app.services.migrations.target_contract import build_contract

SOURCE = {
    "name": "web",
    "type": "container",
    "architecture": "x86_64",
    "profiles": ["default"],
    "config": {"limits.cpu": "1", "volatile.uuid": "uuid-src"},
    "expanded_config": {"limits.cpu": "1", "volatile.uuid": "uuid-src"},
    "devices": {},
    "expanded_devices": {"root": {"type": "disk", "path": "/", "pool": "default"}},
    "status": "Stopped",
}
PROFILES = {"default": {"config": {}, "devices": {"root": {"type": "disk", "path": "/", "pool": "default"}}}}
CONTRACT = build_contract(
    source=SOURCE, target_profiles=PROFILES, profile_mapping={}, network_mapping={},
    usable_networks=[], root_pool="default", intent="move",
)
JOB = SimpleNamespace(
    job_id="job-1", strategy="cross_cluster_pull", intent="move",
    source_project="tenant-a", target_project="tenant-b",
)
RESOURCE = SimpleNamespace(source_ref={"name": "web"}, target_ref={"name": "web"})


def _target(**config: str) -> dict[str, Any]:
    base = {"limits.cpu": "1", "volatile.uuid": "uuid-dst", MIGRATION_JOB_MARKER: "job-1"}
    base.update(config)
    return {**SOURCE, "config": base, "expanded_config": base}


async def _validate(monkeypatch: pytest.MonkeyPatch, target: Optional[dict[str, Any]]) -> None:
    async def read(client: Any, project: str, name: str, label: str) -> Optional[dict[str, Any]]:
        return target if client == "target" else SOURCE

    monkeypatch.setattr(activation, "_read", read)
    state = {
        "target_identity_hash": executor.instance_identity_fingerprint(_target()),
        "source_identity_hash": executor.instance_identity_fingerprint(SOURCE),
    }
    await activation._validate_pair(
        JOB, RESOURCE, state, CONTRACT, "source", "target",
        target_status="stopped", source_status="stopped", label="validation_barrier",
    )


async def test_an_exact_target_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    await _validate(monkeypatch, _target())


@pytest.mark.parametrize(
    ("target", "message"),
    [
        (_target(**{"user.qa-tamper": "1"}), "drifted"),
        (_target(**{"security.privileged": "true"}), "drifted"),
        (_target(**{MIGRATION_JOB_MARKER: "another-job"}), "not the object this job created"),
        ({**_target(), "status": "Running"}, "changed before"),
        (None, "changed before"),
    ],
)
async def test_a_drifted_target_is_never_activated(
    monkeypatch: pytest.MonkeyPatch, target: Optional[dict[str, Any]], message: str
) -> None:
    with pytest.raises(MigrationUnknown, match=message):
        await _validate(monkeypatch, target)
