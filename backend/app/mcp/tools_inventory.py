"""Reads that fan out across the key's whole grant rather than acting on one cluster.

These are the tools a model reaches for first: what may I see, and what is on it. They cost
`inventory.read`, which every application-type preset seeds, so they are the tier most keys can
actually use.
"""

from typing import Annotated, Optional

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field

from app.services.lxd_service import get_lxd_service

from .dispatch import run_inventory

__all__ = ["register"]

_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

ClusterId = Annotated[
    Optional[str], Field(default=None, description="Narrow to one cluster id. Omit to fan out.")
]
Project = Annotated[Optional[str], Field(default=None, description="Narrow to one LXD project.")]


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        name="orcastra_whoami",
        annotations=_READ_ONLY,
        description=(
            "What this API key is allowed to do: its capabilities, its per-cluster grants and "
            "when it expires. Call this first when a tool is refused, so the refusal can be "
            "explained rather than retried."
        ),
    )
    async def orcastra_whoami() -> CallToolResult:
        # note: kept on the surface deliberately, against the argument that it is reconnaissance
        # for an agent under prompt injection. orcastra_list_clusters already discloses the same
        # cluster map, so withholding this buys nothing and costs the model the only way it has to
        # explain a refusal instead of retrying blindly.
        return await run_inventory("whoami")

    @mcp.tool(
        name="orcastra_list_clusters",
        annotations=_READ_ONLY,
        description="Clusters this key may reach, with the permission level held on each.",
    )
    async def orcastra_list_clusters(cluster_id: ClusterId = None) -> CallToolResult:
        return await run_inventory("list_integration_clusters", cluster_id=cluster_id)

    @mcp.tool(
        name="orcastra_list_projects",
        annotations=_READ_ONLY,
        description="LXD projects visible to this key, across its granted clusters.",
    )
    async def orcastra_list_projects(cluster_id: ClusterId = None) -> CallToolResult:
        return await run_inventory(
            "list_integration_projects", cluster_id=cluster_id, lxd_service=get_lxd_service()
        )

    @mcp.tool(
        name="orcastra_list_instances",
        annotations=_READ_ONLY,
        description=(
            "Instances across the clusters this key may reach. Paged: `limit` caps a page at "
            "1000 and `offset` skips. Filter with `status`, ex; Running."
        ),
    )
    async def orcastra_list_instances(
        cluster_id: ClusterId = None,
        project: Project = None,
        status: Annotated[
            Optional[str], Field(default=None, description="Narrow to one status, ex; Running.")
        ] = None,
        limit: Annotated[int, Field(default=200, ge=1, le=1000)] = 200,
        offset: Annotated[int, Field(default=0, ge=0)] = 0,
    ) -> CallToolResult:
        return await run_inventory(
            "list_integration_instances",
            cluster_id=cluster_id,
            project=project,
            status=status,
            limit=limit,
            offset=offset,
            lxd_service=get_lxd_service(),
        )

    @mcp.tool(
        name="orcastra_get_instance",
        annotations=_READ_ONLY,
        description="One instance in detail, searched across the clusters this key may reach.",
    )
    async def orcastra_get_instance(
        name: Annotated[str, Field(description="Instance name.")],
        cluster_id: ClusterId = None,
        project: Project = None,
    ) -> CallToolResult:
        return await run_inventory(
            "get_integration_instance",
            name=name,
            cluster_id=cluster_id,
            project=project,
            lxd_service=get_lxd_service(),
        )

    @mcp.tool(
        name="orcastra_get_operation",
        annotations=_READ_ONLY,
        description=(
            "Status of an LXD operation started earlier. This is how a tool called with "
            "wait=false is followed up: it returns an operation id, and this reports on it."
        ),
    )
    async def orcastra_get_operation(
        operation_id: Annotated[str, Field(description="The operation id to poll.")],
        cluster_id: Annotated[str, Field(description="The cluster it was started on.")],
        project: Annotated[str, Field(default="default")] = "default",
    ) -> CallToolResult:
        return await run_inventory(
            "get_integration_operation",
            operation_id=operation_id,
            cluster_id=cluster_id,
            project=project,
            lxd_service=get_lxd_service(),
        )
