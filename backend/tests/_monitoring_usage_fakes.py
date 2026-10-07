"""Fleet rows and Cluster figures for the monitoring usage tests, built the production way."""

from typing import Any, Dict, Optional

from app.models.schemas import ClusterMonitoringMetrics
from app.services.lxd.instance import _vm_info_from_row


def row(
    name: str,
    *,
    host: str = "c1",
    project: str = "default",
    kind: str = "container",
    limits_cpu: Optional[str] = "2",
    cpu_ns: Optional[int] = 0,
    memory: int = 100,
    disk: int = 10,
    rx: int = 0,
    tx: int = 0,
    pid: int = 100,
    status: str = "Running",
    read_at: Optional[float] = 1000.0,
    processes: int = 5,
):
    """A recursion=2 list row run through the real row mapper."""
    expanded: Dict[str, Any] = {}
    if limits_cpu is not None:
        expanded["limits.cpu"] = limits_cpu
    state = {
        "status": status,
        "pid": pid,
        "processes": processes,
        "cpu": {"usage": cpu_ns},
        "memory": {"usage": memory, "total": 1000},
        "disk": {"root": {"usage": disk, "total": 0}},
        "network": {
            "eth0": {"counters": {"bytes_received": rx, "bytes_sent": tx}, "addresses": []},
            "lo": {"counters": {"bytes_received": 10**12, "bytes_sent": 10**12}, "addresses": []},
        },
    }
    return _vm_info_from_row(
        {
            "name": name,
            "project": project,
            "status": status,
            "status_code": 103 if status == "Running" else 102,
            "type": kind,
            "config": {"volatile.uuid": f"uuid-{name}"},
            "expanded_config": expanded,
            "last_used_at": "2026-10-07T00:00:00Z",
            "location": "none",
            "state": state,
        },
        host,
        f"https://{host}:8443",
        read_at=read_at,
    )


def node(name: str = "c1", *, threads: int = 8, running: int = 0, online: bool = True):
    return ClusterMonitoringMetrics(
        node_name=name,
        is_online=online,
        cpu_total=threads,
        memory_total_bytes=1000,
        running_instances=running,
    )
