"""Narrow operator executor draft for an explicitly approved disconnected VM.

No CLI or connection discovery is provided. The caller must obtain fresh owner
approval for the exact plan, then supply the already trusted selected client.
An approval reference records that decision; a string does not itself authorize it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from outer_plan import LAB_ID, plan_digest, render_plan

GIB = 1024 ** 3
VM_PATH = f"/1.0/instances/{LAB_ID}"


class PhaseAFailure(RuntimeError):
    """Stopped without automatic retry, deletion, or host configuration changes."""


class PylxdTransport:
    """Reuse a selected trusted client; never load certificates or registry data."""

    def __init__(self, client, *, selected_cluster_alias: str):
        if client.api._project not in (None, "default"):
            raise ValueError("Phase A requires the default project")
        self.client = client
        self.selected_cluster_alias = selected_cluster_alias

    def _node(self, path: str):
        if not re.fullmatch(r"/1\.0(?:/[A-Za-z0-9_.-]+)*", path) or ".." in path:
            raise ValueError("Refusing an unexpected daemon path")
        node = self.client.api
        for segment in path.split("/")[2:]:
            if segment:
                node = node[segment]
        return node

    def _output_node(self, path: str):
        parsed = urlsplit(path)
        if (
            parsed.scheme or parsed.netloc or parsed.fragment
            or not parsed.path.startswith(f"{VM_PATH}/logs/exec-output/")
            or parse_qsl(parsed.query, keep_blank_values=True) not in ([], [("project", "default")])
        ):
            raise ValueError("Refusing an output URL outside the owned VM/default project")
        return self._node(parsed.path)

    def get(self, path: str) -> dict:
        return self._node(path).get(
            params={"project": "default"}, timeout=(5, 15), allow_redirects=False,
        ).json()["metadata"]

    def exists(self, path: str) -> bool:
        try:
            self.get(path)
            return True
        except Exception as error:
            if getattr(getattr(error, "response", None), "status_code", None) == 404:
                return False
            raise

    def send(self, method: str, path: str, body: dict) -> str | None:
        allowed = {(request["method"], request["path"]) for request in render_plan(None)["requests"]}
        if (method, path) not in allowed:
            raise ValueError("Refusing a write outside the two Phase A requests")
        response = getattr(self._node(path), method.lower())(
            json=body, params={"project": "default"}, timeout=(5, 30), allow_redirects=False,
        ).json()
        operation = response.get("operation")
        if not operation:
            if response.get("type") != "sync":
                raise RuntimeError("Missing asynchronous operation ID")
            return None
        self._operation_path(operation)
        return operation

    @staticmethod
    def _operation_path(operation: str) -> str:
        if not re.fullmatch(r"/1\.0/operations/[0-9a-f-]{36}", operation):
            raise ValueError("Unexpected operation URL")
        return operation

    def wait(self, operation: str | None, timeout: int) -> dict:
        if operation is None:
            return {}
        path = self._operation_path(operation)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.get(path)
            status = state.get("status_code")
            if status == 200:
                return state
            if isinstance(status, int) and status >= 400:
                raise RuntimeError("Daemon operation failed")
            time.sleep(0.5)
        raise TimeoutError("Daemon operation completion was not verified")

    def wait_agent(self, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.get(f"{VM_PATH}/state")
            if state.get("status") == "Running" and state.get("processes", -1) > 0:
                return
            time.sleep(0.5)
        raise TimeoutError("Guest agent readiness was not verified")

    def upload_probe(self, body: bytes, *, record: Callable) -> None:
        path = f"/srv/{LAB_ID}/probe_nested_kvm.py"
        record("guest_file_attempt", stage="probe_upload", path=path,
               sha256=hashlib.sha256(body).hexdigest())
        response = self._node(f"{VM_PATH}/files").post(
            params={"project": "default", "path": path},
            data=body, timeout=(5, 30), allow_redirects=False,
            headers={"X-LXD-mode": "0600", "X-LXD-uid": "0", "X-LXD-gid": "0", "X-LXD-type": "file"},
        ).json()
        if response.get("type") != "sync":
            raise RuntimeError("Owned file upload completion was not verified")
        record("guest_file_completed", stage="probe_upload", path=path)

    def execute(
        self, command: list[str], timeout: int, capture: bool = False, *, record: Callable,
    ) -> tuple[int, str]:
        allowed = (
            command == ["cloud-init", "status", "--wait"]
            or command[:2] == ["python3", f"/srv/{LAB_ID}/probe_nested_kvm.py"]
            and len(command) == 4 and command[2] == "--run-id"
        )
        if not allowed:
            raise ValueError("Refusing a guest command outside Phase A")
        stage = "cloud_init_wait" if command[0] == "cloud-init" else "nested_kvm_probe"
        record("guest_command_attempt", stage=stage, command=command)
        response = self._node(f"{VM_PATH}/exec").post(
            json={"command": command, "interactive": False, "wait-for-websocket": False,
                  "record-output": True, "environment": {}},
            params={"project": "default"}, timeout=(5, 30), allow_redirects=False,
        ).json()
        operation = self._operation_path(response["operation"])
        record("guest_command_accepted", stage=stage, operation_id=operation)
        state = self.wait(operation, timeout)
        result = state.get("metadata", {})
        returncode = result.get("return", -1)
        if not isinstance(returncode, int) or isinstance(returncode, bool):
            raise TypeError("Guest command return code was not verified")
        record("guest_command_completed", stage=stage, operation_id=operation, returncode=returncode)
        output = ""
        if capture:
            path = result.get("output", {}).get("1")
            if not isinstance(path, str):
                raise RuntimeError("Missing owned guest command output")
            response = self._output_node(path).get(
                params={"project": "default"}, timeout=(5, 15), allow_redirects=False, is_api=False,
            )
            if len(response.content) > 8192:
                raise RuntimeError("Guest probe output exceeded limit")
            output = response.text
        return returncode, output


def _verify_vm(metadata: dict, plan: dict) -> None:
    expected = plan["requests"][0]["body"]
    if (
        metadata.get("name") != LAB_ID or metadata.get("architecture") != "x86_64"
        or metadata.get("type") != "virtual-machine" or metadata.get("profiles") != []
    ):
        raise ValueError("VM type/profiles did not match the approved proposal")
    if metadata.get("devices") != expected["devices"]:
        raise ValueError("VM devices did not match the sole approved root disk")
    if metadata.get("expanded_devices") != expected["devices"]:
        raise ValueError("VM inherited an unexpected expanded device")
    config = metadata.get("config", {})
    if metadata.get("expanded_config") != config:
        raise ValueError("VM inherited unapproved expanded configuration")
    if {
        key: value for key, value in config.items()
        if not key.startswith(("volatile.", "image."))
    } != expected["config"]:
        raise ValueError("VM contains configuration outside the approved proposal")
    for key, value in expected["config"].items():
        if config.get(key) != value:
            raise ValueError("VM configuration/ownership readback mismatch")
    if config.get("volatile.base_image") != expected["source"]["fingerprint"]:
        raise ValueError("VM base image readback mismatch")


def execute_phase_a(
    transport, plan: dict, *, approval_reference: str, approved_plan_digest: str,
    expected_server_name: str, ledger_path: Path,
) -> dict:
    """Execute only after fresh explicit approval; ambiguous writes are never retried."""
    if not approval_reference or not expected_server_name or not plan.get("ready_for_execution"):
        raise ValueError("Explicit approval and a complete selected proposal are required")
    if plan_digest(plan) != approved_plan_digest:
        raise ValueError("The proposal changed after approval")
    fingerprint = plan["requests"][0]["body"]["source"]["fingerprint"]
    if expected_server_name != plan["target"]["server_name"]:
        raise ValueError("Expected daemon identity changed after approval")
    if transport.selected_cluster_alias != plan["target"]["cluster_alias"]:
        raise ValueError("Selected trusted client does not match the approved registry alias")
    if plan != render_plan(fingerprint, plan["run_id"], plan["target"]["cluster_alias"], expected_server_name):
        raise ValueError("Refusing a proposal outside the current narrow Phase A contract")
    provenance = json.loads(Path(__file__).with_name("ubuntu-image-provenance.json").read_text())
    if fingerprint != provenance["fingerprint"] or provenance["instance_type"] != "virtual-machine":
        raise ValueError("The approved image is not the recorded official Ubuntu VM")
    if not ledger_path.is_absolute() or ledger_path.name != f"{LAB_ID}-{plan['run_id']}.jsonl":
        raise ValueError("An exact new run ledger path is required")
    fd = os.open(ledger_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as ledger:
        def record(event: str, **details) -> None:
            ledger.write(json.dumps({"event": event, "run_id": plan["run_id"],
                                    "recorded_at": datetime.now(UTC).isoformat(), **details}) + "\n")
            ledger.flush()
            os.fsync(ledger.fileno())

        record("approved", target=plan["target"], proposal_digest=approved_plan_digest,
               approval_reference=approval_reference)
        stage = "daemon_identity"
        try:
            server = transport.get("/1.0")
            environment = server.get("environment", {})
            if environment.get("server_name") != expected_server_name:
                raise ValueError("Selected daemon identity did not match the approved target")
            if "x86_64" not in environment.get("architectures", []):
                raise ValueError("Selected daemon does not match the pinned image architecture")
            stage = "memory_cpu_capacity"
            resources = transport.get("/1.0/resources")
            memory = resources["memory"]
            if memory["total"] - memory["used"] < 30 * GIB:
                raise ValueError("Insufficient RAM for 22 GiB guest plus 8 GiB host reserve")
            if resources["cpu"]["total"] < 8:
                raise ValueError("CPU baseline no longer meets the reviewed eight-thread host")
            stage = "storage_capacity"
            space = transport.get("/1.0/storage-pools/default/resources")["space"]
            if space["total"] - space["used"] < 232 * GIB:
                raise ValueError("Insufficient default-pool space for root, image, and host reserves")
            stage = "baseline_absence"
            if transport.exists(VM_PATH):
                raise ValueError("VM ID already exists; refusing adoption or overwrite")
            record("baseline", vm_absent=True, image_cached=transport.exists(f"/1.0/images/{fingerprint}"))
            for request, timeout in zip(plan["requests"], (600, 150)):
                stage = "vm_create" if request["method"] == "POST" else "vm_start"
                record("write_attempt", method=request["method"], path=request["path"])
                operation = transport.send(request["method"], request["path"], request["body"])
                record("write_accepted", path=request["path"], operation_id=operation)
                transport.wait(operation, timeout)
                _verify_vm(transport.get(VM_PATH), plan)
                record("vm_verified", vm_id=LAB_ID, operation_id=operation)
            stage = "agent_ready"
            transport.wait_agent(180)
            stage = "cloud_init_wait"
            returncode, _ = transport.execute(plan["guest_actions"]["commands"][0], 180, record=record)
            if returncode != 0:
                raise ValueError("Guest cloud-init completion was not verified")
            probe_body = Path(__file__).with_name("probe_nested_kvm.py").read_bytes()
            if hashlib.sha256(probe_body).hexdigest() != plan["guest_actions"]["file_upload"]["sha256"]:
                raise ValueError("Probe source changed after approval")
            stage = "probe_upload"
            transport.upload_probe(probe_body, record=record)
            stage = "nested_kvm_probe"
            returncode, stdout = transport.execute(plan["guest_actions"]["commands"][1], 30,
                                                   capture=True, record=record)
            probe = json.loads(stdout)
            if probe.get("lab_id") != LAB_ID or probe.get("non_loopback_interfaces") != []:
                raise ValueError("Guest ownership/isolation proof did not match")
            available = returncode == 0 and probe.get("nested_kvm") == "available" and probe.get("api_version") == 12
            record("guest_probe", nested_kvm="available" if available else "blocked", no_nic=True)
            return {"lab_id": LAB_ID, "run_id": plan["run_id"], "vm_id": LAB_ID,
                    "nested_kvm": "available" if available else "blocked",
                    "qa_scope": "Outer guest only; application/engine QA remains unverified"}
        except Exception as error:  # noqa: BLE001 - important: redact daemon details after an ambiguous live write.
            record("stopped", stage=stage, error_type=type(error).__name__, automatic_cleanup=False)
            raise PhaseAFailure("Phase A stopped; inspect the private ledger. No automatic cleanup or retry.") from None
