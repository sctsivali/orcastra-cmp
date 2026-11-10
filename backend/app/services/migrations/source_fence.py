"""Park a retained source so it cannot clash with its target, and undo that on rollback.

A retained source is stopped and fenced in Orcastra, but LXD still counts it. Two fences keep it
out of the target's way:

* ``boot.autostart`` is turned off, so a host reboot cannot start it next to the target.
* On the same cluster, every NIC is masked with a ``none`` device before any target starts. LXD
  refuses to start a bridged NIC while another instance on the same member and network, stopped
  or in another project, has the same name, MAC address, or static IP (``checkAddressConflict``
  in LXD's ``nic_bridged.go``), and a move keeps both the name and the MAC.

Each fence records the original values first, so a resumed job re-derives the approved contract
from the source as it was, and rollback or abort restores it before the source starts.
"""

from __future__ import annotations

import copy
from typing import Any, Optional

from app.models.migration_models import MigrationJob, MigrationResource

from . import executor
from .context import _assert_mutation_lease, _resource_update
from .contracts import MigrationUnknown, _blocking
from .operation_wait import await_operation


def nic_fence_applies(job: MigrationJob) -> bool:
    """Only a staged move on one cluster keeps a same-named, same-MAC source beside its target."""
    return (
        job.intent == "move"
        and job.strategy != "member_relocation"
        and job.source_cluster_id == job.target_cluster_id
    )


def _nic_names(source: dict[str, Any]) -> list[str]:
    devices = source.get("expanded_devices") or source.get("devices") or {}
    return sorted(
        str(name)
        for name, device in devices.items()
        if isinstance(device, dict) and device.get("type") == "nic"
    )


def before_fences(source: dict[str, Any], state: Optional[dict[str, Any]]) -> dict[str, Any]:
    """The source as approved, with any fence this job applied to it undone in memory."""
    if not state:
        return source
    restored = copy.deepcopy(source)
    config = restored.setdefault("config", {})
    # The intent counts too: a fence that landed just before a crash is not recorded as fenced yet.
    if state.get("source_autostart_fenced") or state.get("source_autostart_fence_intent"):
        original = state.get("original_autostart_local")
        if isinstance(original, str):
            config["boot.autostart"] = original
        else:
            config.pop("boot.autostart", None)
        # Only an expanded autostart of true is ever fenced.
        restored["expanded_config"] = {**(restored.get("expanded_config") or {}), "boot.autostart": "true"}
    if state.get("source_nic_fence_intent") and isinstance(state.get("original_nic_local"), dict):
        devices = restored.setdefault("devices", {})
        expanded = restored.setdefault("expanded_devices", {})
        for name, original in state["original_nic_local"].items():
            if isinstance(original, dict):
                devices[name] = dict(original)
            else:
                devices.pop(name, None)
        for name, original in (state.get("original_nic_expanded") or {}).items():
            if isinstance(original, dict):
                expanded[name] = dict(original)
    return restored


async def _write_devices(
    job: MigrationJob,
    client: Any,
    *,
    name: str,
    devices: dict[str, Any],
    based_on: dict[str, Any],
    label: str,
    config_updates: Optional[dict[str, str]] = None,
) -> None:
    await _assert_mutation_lease()
    operation_id = await _blocking(
        executor.replace_instance_devices,
        client,
        project=job.source_project,
        name=name,
        devices=devices,
        expected_devices=based_on,
        config_updates=config_updates,
        label=f"migration.{label}[{name}]",
    )
    if operation_id:
        try:
            outcome = await await_operation(
                client,
                operation_id=operation_id,
                project=job.source_project,
                label=f"migration.wait_{label}[{name}]",
                deadline_seconds=300,
            )
        except executor.OperationForgotten:
            outcome = {"failed": False}
        if outcome.get("failed"):
            raise MigrationUnknown(outcome.get("err") or f"Could not update the devices of '{name}'.")


async def fence_source_nics(
    job: MigrationJob, resource: MigrationResource, state: dict[str, Any], client: Any
) -> dict[str, Any]:
    """Mask every NIC of one stopped, retained source. Safe to repeat after a crash."""
    name = str((resource.source_ref or {}).get("name") or "")
    source = await _blocking(
        executor.read_instance, client, project=job.source_project, name=name, label=f"migration.nic_fence_read[{name}]"
    )
    if source is None:
        raise MigrationUnknown(f"Retained source '{name}' disappeared.")
    if state.get("source_nic_fenced"):
        return state
    if not state.get("source_nic_fence_intent"):
        # important: the originals are recorded once, before the first write. A resumed run must
        # never record an already-masked source as its original.
        nics = _nic_names(source)
        if not nics:
            return state
        local = source.get("devices") or {}
        expanded = source.get("expanded_devices") or {}
        config = source.get("config") or {}
        state = {
            **state,
            "source_nic_fence_intent": True,
            "original_nic_local": {nic: (dict(local[nic]) if nic in local else None) for nic in nics},
            "original_nic_expanded": {nic: dict(expanded[nic]) for nic in nics},
            # LXD drops the generated MAC with the device, so it is put back on restore.
            "original_nic_hwaddr": {
                nic: config[f"volatile.{nic}.hwaddr"]
                for nic in nics
                if isinstance(config.get(f"volatile.{nic}.hwaddr"), str)
            },
        }
        await _resource_update(resource.resource_id, state="cutting_over", safe_result=state)
    masked = dict(source.get("devices") or {})
    for nic in state["original_nic_local"]:
        masked[nic] = {"type": "none"}
    await _write_devices(
        job, client, name=name, devices=masked, based_on=dict(source.get("devices") or {}), label="nic_fence"
    )
    fenced = await _blocking(
        executor.read_instance, client, project=job.source_project, name=name, label=f"migration.nic_fence_check[{name}]"
    )
    if fenced is None or _nic_names(fenced):
        raise MigrationUnknown(f"Could not detach the NICs of retained source '{name}'.")
    state = {
        **state,
        "source_nic_fenced": True,
        "source_definition_hash": executor.instance_fingerprint(fenced),
    }
    await _resource_update(resource.resource_id, state="cutting_over", safe_result=state)
    return state


async def unfence_source_nics(
    job: MigrationJob, resource: MigrationResource, state: dict[str, Any], client: Any
) -> dict[str, Any]:
    """Put the recorded NICs back on a source before it starts again."""
    if not state.get("source_nic_fence_intent") or not isinstance(state.get("original_nic_local"), dict):
        return state
    name = str((resource.source_ref or {}).get("name") or "")
    source = await _blocking(
        executor.read_instance, client, project=job.source_project, name=name, label=f"migration.nic_restore_read[{name}]"
    )
    if source is None:
        raise MigrationUnknown(f"Source '{name}' disappeared before its NICs were restored.")
    devices = dict(source.get("devices") or {})
    for nic, original in state["original_nic_local"].items():
        if isinstance(original, dict):
            devices[nic] = dict(original)
        else:
            devices.pop(nic, None)
    macs = {
        f"volatile.{nic}.hwaddr": mac
        for nic, mac in (state.get("original_nic_hwaddr") or {}).items()
        if isinstance(mac, str) and mac
    }
    if devices != (source.get("devices") or {}):
        await _write_devices(
            job, client, name=name, devices=devices, based_on=dict(source.get("devices") or {}), label="nic_restore"
        )
    restored = await _blocking(
        executor.read_instance, client, project=job.source_project, name=name, label=f"migration.nic_restore_check[{name}]"
    )
    config = (restored or {}).get("config") or {}
    if restored is not None and any(config.get(key) != mac for key, mac in macs.items()):
        # LXD generates a new MAC for a device added in the same write, so the original is set
        # once the NIC exists.
        current_devices = dict(restored.get("devices") or {})
        await _write_devices(
            job,
            client,
            name=name,
            devices=current_devices,
            based_on=current_devices,
            label="nic_mac_restore",
            config_updates=macs,
        )
        restored = await _blocking(
            executor.read_instance, client, project=job.source_project, name=name, label=f"migration.nic_mac_check[{name}]"
        )
    restored_config = (restored or {}).get("config") or {}
    if (
        restored is None
        or sorted(state["original_nic_local"]) != _nic_names(restored)
        or any(restored_config.get(key) != mac for key, mac in macs.items())
    ):
        raise MigrationUnknown(f"Could not restore the NICs of source '{name}'.")
    state = {**state, "source_nic_fence_intent": False, "source_nic_fenced": False}
    await _resource_update(resource.resource_id, state="cleaning", safe_result=state)
    return state


__all__ = ["before_fences", "fence_source_nics", "nic_fence_applies", "unfence_source_nics"]
