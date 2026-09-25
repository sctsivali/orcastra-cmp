"""
Recorded exec output costs `instance.exec` in both of LXD's log layouts.

LXD before 5.21 wrote a recorded exec's output beside the instance logs, as logs/exec_<uuid>.stdout.
LXD 5.21 and later write it under logs/exec-output/<file>, serve the listing at logs/exec-output,
and allow both only to a can_exec entitlement. The raw proxy's guest gate priced only the old
layout, so a read key without `instance.exec` listed logs/exec-output and read another caller's
stdout (`DB_PASSWORD=...` in the probe). Dev's own recorded exec (services/lxd/guest_output.py)
reads and deletes its output at that path.

Every test drives the real raw_proxy handler or the real gate. The session records the request it
would have sent, so "refused" means nothing reached the cluster.
"""

import ast
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from app.core.api_key_capabilities import guest_access_capability_for
from app.routers import integrations
from tests._raw_proxy_fakes import (  # noqa: F401 - fixtures are registered by import
    CLUSTER_ID,
    ENDPOINT,
    _clean_breaker,
    call_raw_proxy,
    fence_calls,
)

GUEST_OUTPUT = Path(__file__).resolve().parents[1] / "app" / "services" / "lxd" / "guest_output.py"

FAMILIES = ("instances", "containers", "virtual-machines")
UUID = "d0a89537-0617-4ed6-a79b-c2e88a970965"

# LXD 5.21+ (instance_logs.go: instanceExecOutputsCmd and instanceExecOutputCmd).
NEW_LAYOUT = (
    "logs/exec-output",
    "logs/exec-output/",
    f"logs/exec-output/exec_{UUID}.stdout",
    f"logs/exec-output/exec_{UUID}.stderr",
    "logs/exec-output/anything-lxd-may-name",
)
# LXD before 5.21: the output files sit beside the instance logs, and the listing names them.
OLD_LAYOUT = (
    "logs",
    f"logs/exec_{UUID}.stdout",
    f"logs/exec_{UUID}.stderr",
)
# Spellings the canonical parser reads as the same path.
SPELLINGS = (
    "logs/%65xec-output/exec_1.stdout",
    "logs/exec%2Doutput/exec_1.stdout",
    "logs/%2565xec-output",
    "logs/EXEC-OUTPUT/exec_1.stdout",
    "logs/exec-output/exec_1.stdout?project=default",
)

NO_EXEC = ["inventory.read", "lxd.proxy", "instance.files"]
WITH_EXEC = ["inventory.read", "lxd.proxy", "instance.exec"]


def _key(capabilities: List[str], projects: Tuple[str, ...] = ("*",)) -> Dict[str, Any]:
    return {
        "id": "oak_exec_output",
        "app_name": "qa-exec-output",
        "capabilities": list(capabilities),
        "cluster_permissions": [
            {"cluster_id": CLUSTER_ID, "permissions": ["read", "write", "admin"], "projects": list(projects)}
        ],
    }


def _paths(tails: Tuple[str, ...]) -> List[str]:
    return [f"/1.0/{family}/web-1/{tail}" for family in FAMILIES for tail in tails]


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """What raw_proxy writes to the audit trail, as the real _audit_key_action receives it."""
    recorded: List[Dict[str, Any]] = []
    monkeypatch.setattr(integrations, "_audit_key_action", lambda api_key, **fields: recorded.append(fields))
    return recorded


@pytest.mark.parametrize("path", _paths(NEW_LAYOUT + OLD_LAYOUT + SPELLINGS))
def test_every_exec_output_path_costs_exec(path: str) -> None:
    assert guest_access_capability_for(path) == "instance.exec"


@pytest.mark.parametrize(
    "path",
    _paths(("logs/lxc.log", "logs/console.log", "logs/qemu.log", "logs/snapshot_1.log", "state"))
    + ["/1.0/instances/exec-output", "/1.0/instances/exec-output/logs/lxc.log"],
)
def test_an_ordinary_log_and_an_instance_named_exec_output_stay_open(path: str) -> None:
    """Dev's split stays: a monitoring key reads a crash log at `read`."""
    assert guest_access_capability_for(path) is None


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize("method", ["GET", "DELETE"])
@pytest.mark.parametrize("path", _paths(NEW_LAYOUT[:3] + OLD_LAYOUT[1:2]))
async def test_the_raw_proxy_refuses_exec_output_without_exec(
    path: str, method: str, audits: List[Dict[str, Any]]
) -> None:
    outcome, session = await call_raw_proxy(_key(NO_EXEC), method, path)

    assert getattr(outcome, "status_code", None) == 403, outcome
    assert "instance.exec" in outcome.detail
    assert session.calls == []
    assert [(record["result"], record["details"]["refused"]) for record in audits] == [
        ("failure", "capability_not_granted")
    ]


@pytest.mark.parametrize("path", _paths(NEW_LAYOUT[:3]))
async def test_a_key_holding_exec_reads_exec_output(path: str, audits: List[Dict[str, Any]]) -> None:
    outcome, session = await call_raw_proxy(_key(WITH_EXEC), "GET", path)

    assert outcome.status_code == 200, outcome
    family = path.split("/")[2]
    assert session.urls == [f"{ENDPOINT}/{family}/web-1/{path.split('/web-1/', 1)[1].rstrip('/')}"]


async def test_a_legacy_key_never_inherits_exec_output(audits: List[Dict[str, Any]]) -> None:
    legacy = _key(NO_EXEC)
    del legacy["capabilities"]

    outcome, session = await call_raw_proxy(legacy, "GET", f"/1.0/instances/web-1/logs/exec-output/exec_{UUID}.stdout")

    assert getattr(outcome, "status_code", None) == 403
    assert session.calls == []


# ── ratchets ───────────────────────────────────────────────────────────────────────────────────


def _log_directories_guest_output_reads() -> List[str]:
    """Every `<api>.logs["<dir>"]` subscript in the recorded-exec module."""
    found: List[str] = []
    for node in ast.walk(ast.parse(GUEST_OUTPUT.read_text())):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "logs"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            found.append(node.slice.value)
    return found


def test_ratchet_the_directory_the_recorded_exec_reads_costs_exec() -> None:
    """New layout: wherever dev's recorded exec reads its output from, a key pays exec to read it."""
    directories = _log_directories_guest_output_reads()
    assert directories, "guest_output.py no longer reads exec output through .logs[...]; update this ratchet"
    for directory in set(directories):
        for family in FAMILIES:
            for tail in (directory, f"{directory}/exec_{UUID}.stdout", f"{directory}/other"):
                path = f"/1.0/{family}/web-1/logs/{tail}"
                assert guest_access_capability_for(path) == "instance.exec", path


# The exec output paths LXD names in a finished record-output exec's metadata.output, per version.
LXD_OUTPUT_MAPS = {
    "lxd-5.0": {"1": f"/1.0/instances/web-1/logs/exec_{UUID}.stdout", "2": f"/1.0/instances/web-1/logs/exec_{UUID}.stderr"},
    "lxd-5.21": {
        "1": f"/1.0/instances/web-1/logs/exec-output/exec_{UUID}.stdout",
        "2": f"/1.0/instances/web-1/logs/exec-output/exec_{UUID}.stderr",
    },
}


@pytest.mark.parametrize("version", sorted(LXD_OUTPUT_MAPS))
def test_ratchet_every_output_path_an_exec_operation_names_costs_exec(version: str) -> None:
    """Old and new layout: any path LXD hands back as exec output, and its directory, costs exec."""
    for path in LXD_OUTPUT_MAPS[version].values():
        assert guest_access_capability_for(path) == "instance.exec", path
        directory = path.rsplit("/", 1)[0]
        assert guest_access_capability_for(directory) == "instance.exec", directory
