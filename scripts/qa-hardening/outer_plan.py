"""Render a disconnected QA VM proposal offline; this module never provisions."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import uuid
from pathlib import Path

LAB_ID = "orcastra-qa-hardening"
IMAGE_SERVER = "https://cloud-images.ubuntu.com/releases"


def plan_digest(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def render_plan(
    fingerprint: str | None, run_id: str | None = None, cluster_alias: str | None = None,
    server_name: str | None = None,
) -> dict:
    if fingerprint is not None and not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("The image fingerprint must be exactly 64 lowercase hex characters")
    if run_id is not None:
        try:
            parsed = uuid.UUID(run_id)
        except (ValueError, AttributeError) as error:
            raise ValueError("The run ID must be a canonical UUID version 4") from error
        if parsed.version != 4 or str(parsed) != run_id:
            raise ValueError("The run ID must be a canonical UUID version 4")
    if cluster_alias is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", cluster_alias):
        raise ValueError("The selected cluster must be a registry alias, never a URL")
    if server_name is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,252}", server_name):
        raise ValueError("The expected daemon identity must be an exact server name")
    source = {
        "type": "image", "mode": "pull", "protocol": "simplestreams", "server": IMAGE_SERVER,
    }
    source.update({"fingerprint": fingerprint} if fingerprint else {"alias": "24.04"})
    owner_run = run_id or "UNAPPROVED-RUN-ID"
    cloud_init = Path(__file__).with_name("cloud-init.yaml").read_text().replace(
        "RUN_ID_WILL_BE_SET_BY_RENDERER", owner_run,
    )
    return {
        "schema_version": 2,
        "lab_id": LAB_ID,
        "run_id": run_id,
        "target": {"cluster_alias": cluster_alias, "project": "default", "server_name": server_name},
        "executor": {
            "source": "scripts/qa-hardening/phase_a.py",
            "sha256": hashlib.sha256(Path(__file__).with_name("phase_a.py").read_bytes()).hexdigest(),
        },
        "image_provenance_sha256": hashlib.sha256(
            Path(__file__).with_name("ubuntu-image-provenance.json").read_bytes(),
        ).hexdigest(),
        "execution": "preview-only; no network or daemon access",
        "ready_for_execution": all((fingerprint, run_id, cluster_alias, server_name)),
        "approval_required": True,
        "preconditions": [
            "Explicit approval naming the selected registered cluster, default project, run UUID, and exact proposal digest",
            "Fresh RAM/storage headroom check and absence of this VM ID",
            "Verified official Ubuntu 24.04 amd64 VM fingerprint and matching x86_64 host architecture",
            "Bounded operation waits and readback; no automatic retry or cleanup after an ambiguous write",
        ],
        "requests": [
            {"method": "POST", "path": "/1.0/instances", "body": {
                "name": LAB_ID, "type": "virtual-machine", "profiles": [],
                "architecture": "x86_64",
                "description": "Disposable disconnected Orcastra QA controller and nested test fleet",
                "source": source,
                "config": {
                    "limits.cpu": "4", "limits.memory": "22GiB", "boot.autostart": "false",
                    "user.qa.lab": LAB_ID, "user.qa.run": owner_run,
                    "cloud-init.user-data": cloud_init,
                    "cloud-init.network-config": "version: 2\nethernets: {}\n",
                },
                "devices": {
                    "root": {"type": "disk", "path": "/", "pool": "default", "size": "120GiB"},
                },
            }},
            {"method": "PUT", "path": f"/1.0/instances/{LAB_ID}/state",
             "body": {"action": "start", "timeout": 120, "force": False}},
        ],
        "guest_actions": {
            "transport": "daemon agent over vsock only; no guest IP, SSH, or external NIC",
            "commands": [
                ["cloud-init", "status", "--wait"],
                ["python3", f"/srv/{LAB_ID}/probe_nested_kvm.py", "--run-id", owner_run],
            ],
            "file_upload": {
                "path": f"/srv/{LAB_ID}/probe_nested_kvm.py", "uid": 0, "gid": 0, "mode": "0600",
                "source": "scripts/qa-hardening/probe_nested_kvm.py",
                "sha256": hashlib.sha256(Path(__file__).with_name("probe_nested_kvm.py").read_bytes()).hexdigest(),
            },
        },
        "isolation_assertions": [
            "profiles remain empty and expanded_devices contains only the owned root disk",
            "No outer network, NIC, ACL, host mount, proxy, routed device, or raw configuration",
            "Agent is available via vsock and guest reports no non-loopback physical network interface",
            "Nested KVM absence is blocked, never counted as a successful VM test",
        ],
        "cleanup_preview": [
            {"method": "PUT", "path": f"/1.0/instances/{LAB_ID}/state",
             "body": {"action": "stop", "timeout": 120, "force": False}},
            {"method": "DELETE", "path": f"/1.0/instances/{LAB_ID}"},
        ],
        "cleanup_preconditions": [
            "Separate explicit cleanup approval immediately before execution",
            "Ledger binds selected registry alias/default project/run UUID, baseline absence, created VM ID, and operation ID",
            "Current user.qa.lab and user.qa.run both match; a recreated same-name VM is never adopted",
            "Do not delete the existing default pool, any pre-existing image, or unrelated host resource",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-fingerprint", help="Verified official Ubuntu 24.04 VM fingerprint")
    parser.add_argument("--run-id", help="Pinned canonical UUID version 4 for this run")
    parser.add_argument("--cluster-alias", help="Selected registry alias, stored only in the private ledger")
    parser.add_argument("--server-name", help="Exact observed daemon identity; a registry alias is not identity evidence")
    args = parser.parse_args()
    try:
        plan = render_plan(args.image_fingerprint, args.run_id, args.cluster_alias, args.server_name)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
