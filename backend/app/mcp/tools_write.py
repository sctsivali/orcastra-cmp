"""Changing an instance that already exists, or creating one. `write` on the named cluster.

Every tool here is a separate tool even where the proxy shares a branch. start, stop and restart
run the same code and cost the same level, and collapsing them into one `power` tool with an
`action` would be smaller. It would also mean a host allow-rule for "start this instance" grants
"stop it", which is not a distinction an operator should lose to save one entry in a list.
"""

from typing import Annotated, Any, Dict, Literal, Optional

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field

from .dispatch import call_proxy

__all__ = ["register"]

_MUTATES = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)

Cluster = Annotated[str, Field(description="Target cluster id.")]
Project = Annotated[str, Field(default="default", description="LXD project.")]
InstanceName = Annotated[str, Field(description="Instance name.")]
Wait = Annotated[
    bool,
    Field(
        default=True,
        description=(
            "Wait for the LXD operation to finish. False returns an operation id immediately, "
            "which orcastra_get_operation then polls. Use it for work that can outlast the "
            "response budget, ex; a create that has to pull an image."
        ),
    ),
]


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        name="lxd_create_instance",
        annotations=_MUTATES,
        description=(
            "Create a container or virtual machine. `config` and `devices` are passed to LXD as "
            "given. Note that a raw.lxc hook or a host-path disk device counts as reaching inside "
            "the guest and additionally costs instance.exec or instance.files."
            " Cloud-init user data or vendor data in `config` needs the instance.exec capability."
        ),
    )
    async def lxd_create_instance(
        cluster_id: Cluster,
        name: InstanceName,
        project: Project = "default",
        source: Annotated[
            Optional[Dict[str, Any]], Field(default=None, description="LXD source block, ex; an image alias.")
        ] = None,
        config: Annotated[Optional[Dict[str, Any]], Field(default=None)] = None,
        devices: Annotated[Optional[Dict[str, Any]], Field(default=None)] = None,
        profiles: Annotated[Optional[list[str]], Field(default=None)] = None,
        instance_type: Annotated[
            Optional[Literal["container", "virtual-machine"]],
            Field(default=None, description="Defaults to LXD's own default when omitted."),
        ] = None,
        wait: Wait = True,
    ) -> CallToolResult:
        params: Dict[str, Any] = {"name": name}
        for field, value in (
            ("source", source), ("config", config), ("devices", devices),
            ("profiles", profiles), ("type", instance_type),
        ):
            if value is not None:
                params[field] = value
        return await call_proxy("create_instance", cluster_id, project=project, params=params, wait=wait)

    @mcp.tool(
        name="lxd_update_instance",
        annotations=_MUTATES,
        description=(
            "Change an existing instance's config, devices, profiles or description. Setting or "
            "changing cloud-init user data or vendor data needs the instance.exec capability."
        ),
    )
    async def lxd_update_instance(
        cluster_id: Cluster,
        name: InstanceName,
        project: Project = "default",
        config: Annotated[Optional[Dict[str, Any]], Field(default=None)] = None,
        devices: Annotated[Optional[Dict[str, Any]], Field(default=None)] = None,
        profiles: Annotated[Optional[list[str]], Field(default=None)] = None,
        description: Annotated[Optional[str], Field(default=None)] = None,
        wait: Wait = True,
    ) -> CallToolResult:
        params: Dict[str, Any] = {"name": name}
        for field, value in (
            ("config", config), ("devices", devices),
            ("profiles", profiles), ("description", description),
        ):
            if value is not None:
                params[field] = value
        return await call_proxy("update_instance", cluster_id, project=project, params=params, wait=wait)

    @mcp.tool(name="lxd_start_instance", annotations=_MUTATES, description="Start an instance.")
    async def lxd_start_instance(
        cluster_id: Cluster, name: InstanceName, project: Project = "default", wait: Wait = True
    ) -> CallToolResult:
        return await call_proxy("start_instance", cluster_id, project=project,
                                params={"name": name}, wait=wait)

    @mcp.tool(
        name="lxd_stop_instance",
        annotations=_MUTATES,
        description="Stop an instance. `force` skips the graceful shutdown and can lose unwritten data.",
    )
    async def lxd_stop_instance(
        cluster_id: Cluster,
        name: InstanceName,
        project: Project = "default",
        force: Annotated[bool, Field(default=False)] = False,
        wait: Wait = True,
    ) -> CallToolResult:
        return await call_proxy("stop_instance", cluster_id, project=project,
                                params={"name": name, "force": force}, wait=wait)

    @mcp.tool(name="lxd_restart_instance", annotations=_MUTATES, description="Restart an instance.")
    async def lxd_restart_instance(
        cluster_id: Cluster,
        name: InstanceName,
        project: Project = "default",
        force: Annotated[bool, Field(default=False)] = False,
        wait: Wait = True,
    ) -> CallToolResult:
        return await call_proxy("restart_instance", cluster_id, project=project,
                                params={"name": name, "force": force}, wait=wait)

    @mcp.tool(
        name="lxd_create_snapshot",
        annotations=_MUTATES,
        description="Snapshot an instance. `stateful` captures running memory and needs CRIU.",
    )
    async def lxd_create_snapshot(
        cluster_id: Cluster,
        name: InstanceName,
        snapshot_name: Annotated[str, Field(description="Name for the new snapshot.")],
        project: Project = "default",
        stateful: Annotated[bool, Field(default=False)] = False,
        wait: Wait = True,
    ) -> CallToolResult:
        return await call_proxy(
            "create_snapshot", cluster_id, project=project,
            params={"name": name, "snapshot_name": snapshot_name, "stateful": stateful}, wait=wait,
        )

    @mcp.tool(
        name="lxd_restore_snapshot",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False),
        description=(
            "Restore an instance to a snapshot. This discards every change made since that "
            "snapshot was taken."
        ),
    )
    async def lxd_restore_snapshot(
        cluster_id: Cluster,
        name: InstanceName,
        snapshot_name: Annotated[str, Field(description="Snapshot to restore to.")],
        project: Project = "default",
        wait: Wait = True,
    ) -> CallToolResult:
        return await call_proxy(
            "restore_snapshot", cluster_id, project=project,
            params={"name": name, "snapshot_name": snapshot_name}, wait=wait,
        )
