"""Destruction, and infrastructure no project contains. `admin` on the named cluster.

Registered only when MCP_ADMIN_TOOLS_ENABLED. A key's own grant always decides whether these RUN;
the flag decides whether a model is ever shown them, which is a different question and worth
answering separately for a surface a model drives.

Every delete carries `destructive_hint=True` so a host can prompt for confirmation, and so does
creating a storage pool, which can format the device it is given. Creating a network or a project
is marked non-destructive but never read-only. Six of these tools target a resource the per-key
project allow-list cannot bound: the handler refuses those to a key not granted the wildcard, and
`tools/list` does not offer them to one.
"""

from typing import Annotated, Any, Dict, Optional

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field

from .dispatch import call_proxy

__all__ = ["register"]

_DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False
)
# Creating infrastructure is not destruction, but it is priced `admin` because it is not bounded
# by a project grant, and it should still not be auto-approved.
_CLUSTER_WIDE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False
)

Cluster = Annotated[str, Field(description="Target cluster id.")]
Project = Annotated[str, Field(default="default", description="Project on the cluster (LXD and Incus alike).")]
Wait = Annotated[bool, Field(default=True, description="Wait for the operation to finish.")]


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        name="lxd_delete_instance",
        annotations=_DESTRUCTIVE,
        description="Delete an instance and its storage. This cannot be undone.",
    )
    async def lxd_delete_instance(
        cluster_id: Cluster,
        name: Annotated[str, Field(description="Instance to delete.")],
        project: Project = "default",
        wait: Wait = True,
    ) -> CallToolResult:
        return await call_proxy("delete_instance", cluster_id, project=project,
                                params={"name": name}, wait=wait)

    @mcp.tool(
        name="lxd_delete_snapshot",
        annotations=_DESTRUCTIVE,
        description="Delete one snapshot of an instance.",
    )
    async def lxd_delete_snapshot(
        cluster_id: Cluster,
        name: Annotated[str, Field(description="Instance the snapshot belongs to.")],
        snapshot_name: Annotated[str, Field(description="Snapshot to delete.")],
        project: Project = "default",
        wait: Wait = True,
    ) -> CallToolResult:
        return await call_proxy(
            "delete_snapshot", cluster_id, project=project,
            params={"name": name, "snapshot_name": snapshot_name}, wait=wait,
        )

    @mcp.tool(
        name="lxd_create_network",
        annotations=_CLUSTER_WIDE,
        description="Create a network. Cluster-wide: a key scoped to named projects cannot use this.",
    )
    async def lxd_create_network(
        cluster_id: Cluster,
        name: Annotated[str, Field(description="Network name.")],
        network_type: Annotated[Optional[str], Field(default=None, description="ex; bridge, ovn.")] = None,
        description: Annotated[Optional[str], Field(default=None)] = None,
        config: Annotated[Optional[Dict[str, Any]], Field(default=None)] = None,
        wait: Wait = True,
    ) -> CallToolResult:
        params: Dict[str, Any] = {"name": name}
        for field, value in (("type", network_type), ("description", description), ("config", config)):
            if value is not None:
                params[field] = value
        return await call_proxy("create_network", cluster_id, params=params, wait=wait)

    @mcp.tool(
        name="lxd_delete_network",
        annotations=_DESTRUCTIVE,
        description="Delete a network. Cluster-wide. Instances still attached to it will break.",
    )
    async def lxd_delete_network(
        cluster_id: Cluster, name: Annotated[str, Field(description="Network to delete.")]
    ) -> CallToolResult:
        return await call_proxy("delete_network", cluster_id, params={"name": name})

    @mcp.tool(
        name="lxd_create_storage_pool",
        # important: destructive. A zfs or lvm pool given `config.source` as a block device formats
        # it, and a host may only auto-approve a tool that says it cannot do that.
        annotations=_DESTRUCTIVE,
        description=(
            "Create a storage pool. Cluster-wide. A pool on an existing block device formats it."
        ),
    )
    async def lxd_create_storage_pool(
        cluster_id: Cluster,
        name: Annotated[str, Field(description="Pool name.")],
        driver: Annotated[str, Field(description="ex; zfs, btrfs, dir, lvm.")],
        description: Annotated[Optional[str], Field(default=None)] = None,
        config: Annotated[Optional[Dict[str, Any]], Field(default=None)] = None,
        wait: Wait = True,
    ) -> CallToolResult:
        params: Dict[str, Any] = {"name": name, "driver": driver}
        for field, value in (("description", description), ("config", config)):
            if value is not None:
                params[field] = value
        return await call_proxy("create_storage_pool", cluster_id, params=params, wait=wait)

    @mcp.tool(
        name="lxd_delete_storage_pool",
        annotations=_DESTRUCTIVE,
        description="Delete a storage pool. Cluster-wide. Volumes on it are lost.",
    )
    async def lxd_delete_storage_pool(
        cluster_id: Cluster, name: Annotated[str, Field(description="Pool to delete.")]
    ) -> CallToolResult:
        return await call_proxy("delete_storage_pool", cluster_id, params={"name": name})

    @mcp.tool(
        name="lxd_create_project",
        annotations=_CLUSTER_WIDE,
        description="Create an LXD project. Cluster-wide.",
    )
    async def lxd_create_project(
        cluster_id: Cluster,
        name: Annotated[str, Field(description="Project name.")],
        description: Annotated[Optional[str], Field(default=None)] = None,
        config: Annotated[Optional[Dict[str, Any]], Field(default=None)] = None,
    ) -> CallToolResult:
        params: Dict[str, Any] = {"name": name}
        for field, value in (("description", description), ("config", config)):
            if value is not None:
                params[field] = value
        return await call_proxy("create_project", cluster_id, params=params)

    @mcp.tool(
        name="lxd_delete_project",
        annotations=_DESTRUCTIVE,
        description="Delete an LXD project. Cluster-wide. It must be empty.",
    )
    async def lxd_delete_project(
        cluster_id: Cluster, name: Annotated[str, Field(description="Project to delete.")]
    ) -> CallToolResult:
        return await call_proxy("delete_project", cluster_id, params={"name": name})
