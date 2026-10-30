"""Keep registered-cluster vocabulary stable at backend operator boundaries."""

from pathlib import Path


ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"

REGISTERED_CLUSTER_ROUTERS = (
    "cert_manager.py",
    "console.py",
    "events.py",
    "instance_file_transfer.py",
    "instance_files.py",
    "images.py",
    "instances.py",
    "ipam.py",
    "lxd_operations.py",
    "lxd_server.py",
    "lxd_warnings.py",
    "my_keys.py",
    "network_acls.py",
    "networks.py",
    "permissions.py",
    "profiles.py",
    "projects.py",
    "storage_pools.py",
    "storage_volumes.py",
    "vms.py",
)

# Wire fields remain node/host. These phrases are only copy that would tell an API consumer
# that a registered endpoint is a node or host instead of a Cluster.
FORBIDDEN_OPERATOR_COPY = (
    "Target host node",
    "Filter by specific host node",
    "Host node",
    "LXD host node",
    "Target node/cluster",
    "Node name where",
    "List of nodes",
    "from LXD nodes",
    "connected LXD nodes",
    "Invalid host. Valid hosts",
    "Source node that reported",
    "node connectivity warnings",
    "on this node",
    "specific LXD node",
)


def test_registered_endpoints_use_cluster_in_operator_copy() -> None:
    failures: list[str] = []
    for router_name in REGISTERED_CLUSTER_ROUTERS:
        source = (ROUTERS / router_name).read_text(encoding="utf-8")
        stale = [phrase for phrase in FORBIDDEN_OPERATOR_COPY if phrase in source]
        if stale:
            failures.append(f"{router_name}: {', '.join(stale)}")

    assert not failures, (
        "Registered endpoint copy still says node/host: "
        + "; ".join(failures)
        + ". Preserve wire fields, but call the endpoint a Cluster."
    )
