"""Reads against one named cluster, through the operation proxy.

Distinct from the inventory tier: these name a cluster and cost `read` on it, so a key granted one
cluster cannot use them to look at another. The inventory tier answers "what is out there", these
answer "what does this cluster say".
"""

from typing import Annotated, Literal, Optional

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field

from app.core.api_key_capabilities import project_grant_is_unrestricted

from .auth import current_api_key
from .dispatch import call_proxy

__all__ = ["register"]

_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

Cluster = Annotated[str, Field(description="Target cluster id.")]
Project = Annotated[str, Field(default="default", description="LXD project.")]
InstanceName = Annotated[str, Field(description="Instance name.")]

# networks, storage pools and profiles share one code path and one price on the proxy, so they are
# one tool with a discriminator. Collapsing them erases no permission distinction. start, stop and
# restart also share a branch and are deliberately NOT collapsed: three verbs with three blast
# radii, and a host allow-rule can only address a tool.
_RESOURCE_OPERATIONS = {
    "networks": "list_networks",
    "storage_pools": "list_storage_pools",
    "profiles": "list_profiles",
}


def _a_granted_project(cluster_id: str) -> str:
    """A project this key may name on `cluster_id`, for a read whose answer does not depend on it.

    `proxy_operation` authorises the request's project before anything else, and `list_projects`
    then narrows its answer to the grant whichever project was named. Sending `default` therefore
    refused every key limited to other projects, the keys this tool's narrowing exists for.
    """
    for grant in current_api_key().get("cluster_permissions") or []:
        if not isinstance(grant, dict) or grant.get("cluster_id") != cluster_id:
            continue
        projects = grant.get("projects", ["default"]) or ["default"]
        if project_grant_is_unrestricted(projects) or "default" in projects:
            return "default"
        return sorted(str(p) for p in projects)[0]
    # No grant on that cluster: the proxy refuses it by name, which is the useful answer.
    return "default"


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        name="lxd_list_instances",
        annotations=_READ_ONLY,
        description="Instances on one cluster, as that cluster reports them.",
    )
    async def lxd_list_instances(cluster_id: Cluster, project: Project = "default") -> CallToolResult:
        return await call_proxy("list_instances", cluster_id, project=project)

    @mcp.tool(
        name="lxd_get_instance",
        annotations=_READ_ONLY,
        description="One instance on one cluster, in detail.",
    )
    async def lxd_get_instance(
        cluster_id: Cluster, name: InstanceName, project: Project = "default"
    ) -> CallToolResult:
        return await call_proxy("get_instance", cluster_id, project=project, params={"name": name})

    @mcp.tool(
        name="lxd_list_projects",
        annotations=_READ_ONLY,
        description=(
            "LXD projects on one cluster. Narrowed server-side to the projects this key was "
            "granted, so it lists what the key may use rather than what exists."
        ),
    )
    async def lxd_list_projects(
        cluster_id: Cluster,
        project: Annotated[
            Optional[str],
            Field(default=None, description="A project you were granted. Omit to use one of them."),
        ] = None,
    ) -> CallToolResult:
        return await call_proxy(
            "list_projects", cluster_id, project=project or _a_granted_project(cluster_id)
        )

    @mcp.tool(
        name="lxd_list_images",
        annotations=_READ_ONLY,
        description="Images available on one cluster.",
    )
    async def lxd_list_images(cluster_id: Cluster, project: Project = "default") -> CallToolResult:
        return await call_proxy("list_images", cluster_id, project=project)

    @mcp.tool(
        name="lxd_list_snapshots",
        annotations=_READ_ONLY,
        description="Snapshots of one instance.",
    )
    async def lxd_list_snapshots(
        cluster_id: Cluster, name: InstanceName, project: Project = "default"
    ) -> CallToolResult:
        return await call_proxy("list_snapshots", cluster_id, project=project, params={"name": name})

    @mcp.tool(
        name="lxd_get_operation",
        annotations=_READ_ONLY,
        description=(
            "Status of an LXD operation on one cluster, ex; one a tool called with wait=false "
            "returned. Costs `read` on that cluster, so any key that started the operation can "
            "follow it. Completed operations are reaped by LXD, so a late poll is not_found: "
            "check the resource itself then."
        ),
    )
    async def lxd_get_operation(
        cluster_id: Cluster,
        operation_id: Annotated[str, Field(description="The operation id to poll.")],
        project: Project = "default",
    ) -> CallToolResult:
        return await call_proxy(
            "get_operation", cluster_id, project=project, params={"operation_id": operation_id}
        )

    @mcp.tool(
        name="lxd_list_resources",
        annotations=_READ_ONLY,
        description=(
            "Networks, storage pools or profiles on one cluster. One tool because all three cost "
            "the same and run the same code path."
        ),
    )
    async def lxd_list_resources(
        cluster_id: Cluster,
        kind: Annotated[
            Literal["networks", "storage_pools", "profiles"],
            Field(description="Which resource family to list."),
        ],
        project: Project = "default",
    ) -> CallToolResult:
        return await call_proxy(_RESOURCE_OPERATIONS[kind], cluster_id, project=project)
