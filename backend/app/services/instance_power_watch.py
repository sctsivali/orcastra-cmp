"""Tells an interactive console when its instance powered down.

LXD does not end a serial console operation when a VM's QEMU process exits. After a restart the
console websocket stays open and keeps answering pings while the new boot goes to a new QEMU
process with nothing attached, so the page would show "Connected" over a silent console. The SPICE
sockets do drop, but only as a bare connection loss. LXD's lifecycle events say what happened:
instance-shutdown (graceful) or instance-stopped (forced) as the exiting process goes, for an API
restart, a forced restart and `reboot` inside the guest alike (LXD 5.21). The console relay ends on
either and closes with 1012; the browser reconnects once the instance runs again.

instance-restarted and instance-started are not used: they arrive once the new process is up,
possibly after the browser already attached to it.
"""

from __future__ import annotations

from functools import partial
from typing import Any, Mapping

from app.services.lxd.events_hub import get_events_hub

POWER_DOWN_ACTIONS = frozenset({"instance-shutdown", "instance-stopped"})


def is_power_down(frame: Mapping[str, Any], *, instance: str, project: str) -> bool:
    return (
        frame.get("type") == "lifecycle"
        and frame.get("action") in POWER_DOWN_ACTIONS
        and frame.get("instance") == instance
        and (frame.get("project") or "default") == (project or "default")
    )


async def wait_for_power_down(host: str, instance: str, project: str) -> str:
    """The lifecycle action, once ``instance`` on ``host`` powers down. Shares the node's event stream."""
    hub = get_events_hub()
    accept = partial(is_power_down, instance=instance, project=project)
    queue = await hub.subscribe(host, accept=accept)
    try:
        while True:
            frame = await queue.get()
            if accept(frame):
                return str(frame["action"])
    finally:
        await hub.unsubscribe(host, queue)
