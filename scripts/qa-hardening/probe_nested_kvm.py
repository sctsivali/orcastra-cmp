"""Probe KVM inside the approved lab guest; never change host configuration."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import uuid
from pathlib import Path

LAB_ID = "orcastra-qa-hardening"
KVM_GET_API_VERSION = 0xAE00
KVM_CREATE_VM = 0xAE01


def require_lab_guest(expected_run_id: str | None) -> None:
    marker = Path("/etc/orcastra-qa-hardening")
    if not marker.is_file() or marker.is_symlink() or marker.read_text().strip() != LAB_ID:
        raise RuntimeError("Refusing to run outside the approved isolated lab guest")
    run_marker = Path("/etc/orcastra-qa-hardening-run")
    try:
        valid_uuid = expected_run_id is not None and uuid.UUID(expected_run_id).version == 4
    except (ValueError, AttributeError):
        valid_uuid = False
    if (
        not valid_uuid or not run_marker.is_file() or run_marker.is_symlink()
        or run_marker.read_text().strip() != expected_run_id
    ):
        raise RuntimeError("Refusing a guest whose run ownership does not match")


def probe(expected_run_id: str | None = None) -> dict:
    require_lab_guest(expected_run_id)
    result = {"lab_id": LAB_ID, "nested_kvm": "blocked", "api_version": None}
    interfaces = guest_interfaces()
    result["non_loopback_interfaces"] = interfaces
    if interfaces:
        result["reason"] = "Unexpected network interface in the disconnected Phase A guest"
        return result
    try:
        with open("/dev/kvm", "rb", buffering=0) as device:
            api_version = fcntl.ioctl(device.fileno(), KVM_GET_API_VERSION, 0)
            result["api_version"] = api_version
            if api_version != 12:
                result["reason"] = "Unsupported KVM API version"
                return result
            vm_fd = fcntl.ioctl(device.fileno(), KVM_CREATE_VM, 0)
            os.close(vm_fd)
            result["nested_kvm"] = "available"
            result["reason"] = "KVM opened and created an empty VM file descriptor"
    except OSError as error:
        result["reason"] = f"KVM unavailable: {error.strerror or type(error).__name__}"
    return result


def guest_interfaces() -> list[str]:
    return sorted(path.name for path in Path("/sys/class/net").iterdir() if path.name != "lo")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    outcome = probe(parser.parse_args().run_id)
    print(json.dumps(outcome, indent=2))
    raise SystemExit(0 if outcome["nested_kvm"] == "available" else 2)
