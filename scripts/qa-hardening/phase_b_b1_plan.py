"""Render the exact B1 static-runtime proposal without live reads or writes."""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from pathlib import Path

from phase_b_b1_guest import LAB, canonical_digest, runtime_policy, systemd_units

SCRIPTS = Path(__file__).parent
GUEST_ROOT = "/srv/" + LAB


def render_b1(*, vm_run_id: str, phase_a_digest: str, phase_b_id: str) -> dict:
    for value in [vm_run_id, phase_b_id]:
        parsed = uuid.UUID(value)
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError("An exact canonical UUID version 4 is required")
    if len(phase_a_digest) != 64 or any(value not in "0123456789abcdef" for value in phase_a_digest):
        raise ValueError("The approved canonical Phase A digest is required")
    docker = json.loads((SCRIPTS / "phase_b_docker_static_provenance.json").read_text())
    plugins = json.loads((SCRIPTS / "phase_b_plugins_provenance.json").read_text())["plugins"]
    tools = [dict(docker, filename="docker-29.8.1.tgz"), dict(plugins["compose"], filename="docker-compose"),
             dict(plugins["buildx"], filename="docker-buildx")]
    unit = LAB + "-docker.service"
    incoming = f"{GUEST_ROOT}/incoming/{phase_b_id}"
    policy = runtime_policy()
    units = systemd_units(vm_run_id, phase_b_id)
    plan = {
        "schema_version": 1, "phase": "B1", "phase_b_id": phase_b_id,
        "scope": "Transfer and install static Docker/Compose/Buildx inside the approved no-NIC QA VM only",
        "target": {"vm_name": LAB, "vm_run_id": vm_run_id, "phase_a_digest": phase_a_digest,
                   "project": "default", "trusted_connection": "The approved private Phase A target wrapper",
                   "expanded_devices": ["root"], "profiles": [], "outer_nic_count": 0},
        "approval": "New exact owner approval required; Phase A approval does not authorize B1",
        "controller_staging": {"path": f"/tmp/{LAB}-b1-{phase_b_id}", "mode": "0700",
                               "minimum_free_bytes": 512 * 1024 * 1024,
                               "expected_tool_bytes": sum(item["size"] for item in tools),
                               "effect": "Public HTTPS downloads and reviewed B1 helper/proposal copies only; no installation, host Docker, registry layers or application source"},
        "tools": tools,
        "guest_script_sha256": hashlib.sha256((SCRIPTS / "phase_b_b1_guest.py").read_bytes()).hexdigest(),
        "agent_transfers": {"path": incoming, "files": ["docker-29.8.1.tgz", "docker-compose", "docker-buildx",
                                                        "phase_b_b1_guest.py", "b1-proposal.json"],
                            "mode": "0600", "parent_mode": "0700", "existing_path": "Refuse, never adopt or overwrite"},
        "docker_policy": policy, "systemd_units": units,
        "guest_entrypoint": ["python3", incoming + "/phase_b_b1_guest.py", "--proposal", incoming + "/b1-proposal.json"],
        "approval_arguments_required": {"--approved-digest": "proposal_digest from this canonical document",
                                        "--approval-reference": "New exact owner approval reference, supplied by trusted wrapper"},
        "guest_commands_internal": [[GUEST_ROOT + "/bin/dockerd", "--validate", "--config-file", GUEST_ROOT + "/runtime-config/daemon.json"],
                                    ["systemctl", "daemon-reload"], ["systemctl", "start", unit]],
        "created_paths": [GUEST_ROOT + "/" + directory for directory in ["bin", "docker-config", "docker-config/cli-plugins",
                                                                          "docker-data", "docker-exec", "runtime-config"]]
                         + ["/etc/systemd/system/" + name for name in units]
                         + [GUEST_ROOT + "/artifacts/b1-" + phase_b_id + ".jsonl"],
        "resource_budget": {"vm_ram_gib": 22, "vm_root_gib": 120, "host_ram_reserve_gib": 8,
                            "host_pool_reserve_gib": 100, "docker_slice_max_gib": 14,
                            "steady_future_application_budget_gib": 6, "single_future_build_worker_gib": 6,
                            "os_browser_budget_gib": 4, "guest_free_disk_before_b1_gib": 80},
        "preflight": ["Private wrapper rechecks trusted alias/daemon/default project and exact VM run/root-only expanded devices",
                      "Guest root, exact ownership markers, only loopback, x86_64, cgroup v2, loaded or loadable guest OverlayFS, enabled AppArmor",
                      "Pinned-image prerequisite commands, >=80GiB free disk, all B1 runtime/unit/socket IDs absent",
                      "Exact approved helper hash, three regular runtime artifact files with matching hashes and sizes"],
        "success": ["Docker client/server29.8.1, Compose5.5.1, Buildx0.37.1",
                    "Exact owned Unix socket/data root/overlay2 driver; zero images and zero containers; only loopback",
                    "Unix socket root:root0660 beneath root:root0700; loaded owned units match exact files without drop-ins",
                    "Actual validated systemd ControlGroup enforces memory15032385536 bytes, CPUquota ratio3, tasks8192; service placement and ancestors verified",
                    "Sanitized finite guest ledger plus outer file/exec attempts and accepted operation IDs"],
        "excluded": ["Outer host changes", "NIC/bridge/ACL/trust changes", "OCI layer download/load/build/push",
                     "App source/test runner export", "Application containers", "Database migrations or data",
                     "Vault initialization/secrets/PKI", "SSO/account/organization/cluster registration",
                     "Nested engines", "Cleanup/retry/overwrite/forced stop"],
        "failure": "Stop at recorded stage, retain files and runtime state, no implicit retry or cleanup",
        "cleanup": "Separate exact approval for ledger-created IDs only; never shared host Docker or broad prune",
        "b2": "Not implemented or approved; Node24 decision remains separate and B1 does not depend on it",
    }
    plan["proposal_digest"] = canonical_digest(plan)
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm-run-id", required=True)
    parser.add_argument("--phase-a-digest", required=True)
    parser.add_argument("--phase-b-id", required=True)
    args = parser.parse_args()
    print(json.dumps(render_b1(vm_run_id=args.vm_run_id, phase_a_digest=args.phase_a_digest, phase_b_id=args.phase_b_id), indent=2))


if __name__ == "__main__":
    main()
