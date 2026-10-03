"""Render an offline Phase B proposal; this module never provisions or downloads."""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from pathlib import Path

from phase_b_source import source_inventory

LAB = "orcastra-qa-hardening"
ROOT = "/srv/" + LAB
IMAGE_NAMES = ("postgres", "redis", "vault", "authentik", "backend_base", "frontend_supported_candidate")
SCRIPTS = Path(__file__).parent


def digest(document: dict) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def reviewed_uuid(value: str) -> str:
    parsed = uuid.UUID(value)
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("An exact canonical UUID version 4 is required")
    return value


def render_plan(repository: Path, *, vm_run_id: str, phase_a_digest: str, phase_b_id: str,
                node24_approved: bool = False, bundle: dict | None = None) -> dict:
    for run_id in [vm_run_id, phase_b_id]:
        reviewed_uuid(run_id)
    if len(phase_a_digest) != 64 or any(value not in "0123456789abcdef" for value in phase_a_digest):
        raise ValueError("A reviewed canonical Phase A digest is required")
    pins_path = SCRIPTS / "phase_b_public_pins.json"
    image_pins = json.loads(pins_path.read_text())["images"]
    tools = [json.loads((SCRIPTS / "phase_b_docker_static_provenance.json").read_text())]
    tools.extend(json.loads((SCRIPTS / "phase_b_plugins_provenance.json").read_text())["plugins"].values())
    inventory = source_inventory(repository)
    dependencies_path = SCRIPTS / "phase_b_dependencies.json"
    dependencies = json.loads(dependencies_path.read_text()) if dependencies_path.exists() else None
    images = {name: image_pins[name] for name in IMAGE_NAMES}
    layers = {layer["digest"]: layer["size"] for image in images.values() for layer in image["layers"]}
    reasons = ["B2 application/Vault/SSO/database bootstrap executor is not implemented or approved"]
    if not node24_approved:
        reasons.append("Owner decision on Node 24.21.0 remains pending; Node 20 is EOL")
    if dependencies is None:
        reasons.append("The complete wheel/npm/APK closure has not been frozen")
    else:
        reasons.extend(dependencies.get("unverified_boundaries", []))
    if bundle is None:
        reasons.append("Verified staged Docker archive/source/package/tool output hashes are not available")
    plan = {
        "schema_version": 1,
        "scope": "Offline public dependency preparation and separately approved owned guest bootstrap",
        "phase_b_id": phase_b_id,
        "ready_for_guest_execution": not reasons,
        "remaining_gates": reasons,
        "target": {"vm_name": LAB, "vm_run_id": vm_run_id, "phase_a_digest": phase_a_digest,
                   "project": "default", "daemon_identity": "Bind to the private approved Phase A wrapper",
                   "required_devices": ["root"], "profiles": [], "outer_nic_count": 0},
        "capacity": {"outer_ram_gib": 22, "outer_vcpu": 4, "outer_root_gib": 120,
                     "host_ram_reserve_gib": 8, "host_pool_reserve_gib": 100,
                     "service_memory_mib": {"postgres": 512, "authentik-postgres": 512,
                                            "redis": 128, "vault": 384, "authentik-server": 1024,
                                            "authentik-worker": 1024, "backend": 1536, "frontend": 512},
                     "guest_docker_slice_max_gib": 14, "guest_os_browser_budget_gib": 4,
                     "single_build_worker_budget_gib": 6, "steady_service_budget_gib": 6,
                     "nested_engines_during_build": "Stopped; provisioning requires a separate Phase C proposal",
                     "controller_staging_reserve_gib": 14, "guest_free_disk_before_bootstrap_gib": 80,
                     "guest_free_disk_before_nested_engines_gib": 60},
        "public_staging": {"path": f"/tmp/{LAB}-phase-b-{phase_b_id}", "mode": "0700",
                           "registry_blobs": len(layers), "registry_compressed_bytes": sum(layers.values()),
                           "tool_bytes": sum(tool["size"] for tool in tools),
                           "registry_images": images, "tools": tools,
                           "requirements": ["New exact directory only; retain partial files on failure",
                                            "No host Docker daemon, Docker socket, pip config, npm config, or stored auth",
                                            "HTTPS public allowlist; verify every recorded size and digest",
                                            "Convert layers to Docker archives without extracting layer contents",
                                            "Save config image IDs and origin platform digests; use local tags with pull disabled"]},
        "source": inventory,
        "dependencies": dependencies,
        "bundle": bundle,
        "guest_paths": {"incoming": f"{ROOT}/incoming/{phase_b_id}", "bin": ROOT + "/bin",
                        "plugins": ROOT + "/docker-config/cli-plugins", "docker_data": ROOT + "/docker-data",
                        "docker_exec": ROOT + "/docker-exec", "docker_socket": ROOT + "/docker.sock",
                        "configuration": ROOT + "/config", "source": ROOT + "/source",
                        "state": ROOT + "/state", "artifacts": ROOT + "/artifacts",
                        "ledger": f"{ROOT}/artifacts/phase-b-{phase_b_id}.jsonl"},
        "guest_bootstrap": [
            {"stage": "read_only_preflight", "checks": ["Exact lab/run markers; expanded root-only VM readback",
                                                          "No non-loopback guest interface before Docker starts",
                                                          "x86_64, kernel cgroup v2, overlay, iptables, AppArmor, procps, xz, systemd",
                                                          "Fresh disk/memory capacity and UTC clock; required service/path IDs absent",
                                                          "Reviewed incoming bundle hashes and regular-file paths"]},
            {"stage": "agent_transfer", "effect": "Only exact reviewed regular bundle files into incoming/<phase-b-id>; mode 0600"},
            {"stage": "runtime", "effect": "Install Docker static, Compose, Buildx into owned guest paths only; no host runtime changes",
             "new_systemd_ids": [LAB + ".slice", LAB + "-docker.service"],
             "docker_policy": {"hosts": ["unix://" + ROOT + "/docker.sock"], "data-root": ROOT + "/docker-data",
                               "exec-root": ROOT + "/docker-exec", "exec-opts": ["native.cgroupdriver=systemd"],
                               "cgroup-parent": LAB + ".slice", "features": {"containerd-snapshotter": False},
                               "log-driver": "local", "log-opts": {"max-size": "10m", "max-file": "2"},
                               "live-restore": False}},
            {"stage": "images", "effect": "docker image load of exact staged archives; check local tag image IDs; never pull"},
            {"stage": "offline_build", "effect": "One Buildx docker-driver build at a time with --network=none --pull=false",
             "differences": ["Backend builder skips gcc/libffi development packages only when all pinned wheels are available",
                             "pip uses the reviewed wheel directory without an index; no sdists",
                             "Frontend npm uses a reviewed private cache with --offline; verified local APK closure",
                             "The shipped runtime stage remains byte-for-byte; FROM resolves the reviewed loaded base tag"]},
            {"stage": "fresh_private_state", "effect": "Generate lab-only database, Vault, OIDC, encryption and synthetic identity material inside the guest",
             "constraints": ["0600 files beneath 0700 state; never stdout, shell arguments, tracked source, controller bundle, or raw logs",
                             "New Vault init/unseal, mounts/PKI and scoped service token only",
                             "New Authentik DB/bootstrap, application/provider/groups/users only; no shared IdP",
                             "No Docker socket mounted into any application or Authentik container"]},
            {"stage": "services", "effect": "Create exact labeled guest Docker network/volumes/containers and apply migrations to fresh QA PostgreSQL only"},
            {"stage": "readback", "checks": ["Runtime versions/image IDs/source digests; Compose health with bounded wait",
                                             "Fresh migration head and PostgreSQL data readback; Redis encrypted cache round-trip",
                                             "Vault mount/role/scoped-token denial; OIDC issuer/audience/signature and synthetic role login",
                                             "No outer NIC; no public route; loopback published ports only; no nested or registered fleet targets"]},
        ],
        "compose_resources": {"project": LAB, "network": LAB + "-application", "bridge": "qa-app0",
                              "subnet": "172.28.66.0/24", "internal": True,
                              "services": ["postgres", "authentik-postgres", "redis", "vault", "authentik-server",
                                           "authentik-worker", "backend", "frontend"],
                              "volumes": [LAB + "-" + value for value in ["app-postgres", "auth-postgres", "redis-data",
                                                                           "vault-data", "auth-data", "backend-config", "uploads"]],
                              "published_ports": {"frontend": "127.0.0.1:32025:2025", "backend": "127.0.0.1:32405:4050",
                                                  "authentik": "127.0.0.1:32090:32090", "vault": "127.0.0.1:32820:8200"},
                              "labels": {"orcastra.qa.lab": LAB, "orcastra.qa.vm_run": vm_run_id,
                                         "orcastra.qa.phase_b": phase_b_id}, "pull_policy": "never", "restart": "no"},
        "cleanup": {"automatic": False, "approval": "Separate exact approval after evidence export",
                    "targets": "Exact created IDs in this phase ledger with matching VM run and Phase B labels",
                    "exclusions": ["Shared host Docker/systemd/firewall", "Registered fleet", "Existing image/pool",
                                   "Other VM run", "Broad prune, wildcard deletion, volume blanket cleanup"]},
        "evidence": {"ledger": "Attempt, stage, accepted operation ID, completion and readback; no secrets or raw outputs",
                     "uncertain_operation": "Stop at the recorded stage; no automatic retry, overwrite, forced stop or cleanup"},
        "artifact_hashes": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in [
            pins_path, SCRIPTS / "phase_b_source.py", SCRIPTS / "phase_b_images.py",
            SCRIPTS / "phase_b_docker_static_provenance.json", SCRIPTS / "phase_b_plugins_provenance.json",
            SCRIPTS / "phase_b_guest_prerequisites.json",
        ]},
    }
    plan["proposal_digest"] = digest(plan)
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--vm-run-id", required=True)
    parser.add_argument("--phase-a-digest", required=True)
    parser.add_argument("--phase-b-id", required=True)
    parser.add_argument("--node24-approved", action="store_true", help="Record an existing owner decision; never infer permission")
    parser.add_argument("--bundle-inventory", type=Path, help="Optional already reviewed nonsecret bundle inventory")
    args = parser.parse_args()
    bundle = json.loads(args.bundle_inventory.read_text()) if args.bundle_inventory else None
    print(json.dumps(render_plan(args.repository, vm_run_id=args.vm_run_id, phase_a_digest=args.phase_a_digest,
                                 phase_b_id=args.phase_b_id, node24_approved=args.node24_approved, bundle=bundle), indent=2))


if __name__ == "__main__":
    main()
