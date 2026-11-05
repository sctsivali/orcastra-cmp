"""What an API key's grant already holds, and the tenant rules a key writes, kept inside its reach.

Provisioning, and the policy sync beside it, checked only that the key's own grant covers the
project, and a '*' grant covers every project on the cluster, another organization's included. The
rule they write widens the key creator's Partner bound and hands the listed emails that project.

- A rule a key writes may not name a project another organization's policies already name, unless
  that organization is on the key's side: one the key is an owner of, or one its creator runs
  (services.cluster_parties). A project nobody names yet, which is every fresh
  purchase, still provisions.
- A rule the policy sync writes must sit inside the key's own grant.

The dashboard half, a grant past the caller's project bound, is refuse_a_grant_past_the_project_bound,
asked by API key minting, edits and rotation and by add-on installation and rescope.

Forward-ported from the v1.0.0-RC4-hotfix line (7d0d79e3, 2d2a0961) through 17215e29, with the key's
side read from the owner set (organization_owners) rather than the single api_key_id column.
"""

from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from fastapi import HTTPException
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_key_permissions import granted_level
from app.core.org_scope import get_current_allowed_clusters, project_is_visible
from app.services import cluster_parties

__all__ = [
    "already_granted",
    "refuse_a_grant_past_the_project_bound",
    "refuse_a_project_another_party_holds",
    "rule_outside_the_key_grant",
]

# cluster id -> (projects the key holds there, its permission level there)
Granted = Dict[str, Tuple[Set[str], int]]


def already_granted(metadata: Mapping[str, Any]) -> Granted:
    """What a stored key holds per cluster. The last grant per cluster wins, as it does at request time."""
    return {
        str(grant.get("cluster_id") or ""): (
            set(grant.get("projects") or ["default"]),
            granted_level(grant.get("permissions", ["read"])),
        )
        for grant in (metadata.get("cluster_permissions") or [])
        if isinstance(grant, dict)
    }


def _field(grant: Any, name: str) -> Any:
    return grant.get(name) if isinstance(grant, Mapping) else getattr(grant, name)


async def refuse_a_grant_past_the_project_bound(
    db: AsyncSession,
    caller_email: str,
    grants: Iterable[Any],
    already: Optional[Granted] = None,
    *,
    held: Callable[[AsyncSession, str, Set[str]], Awaitable[Set[str]]] = cluster_parties.clusters_held_by_another_party,
) -> None:
    """Raise 403 when a grant names '*', `default` or a project the caller cannot see on a cluster another party holds.

    important: asked after the cluster scope check and before any cluster is read. On a cluster
    another party also holds, a grant naming that party's project, or '*', lets the key act on it
    through the raw proxy, /proxy and provisioning. A cluster held only by the caller's side is
    unchanged, so the '*' grant an OrcaHub connection carries still works there. What a key already
    holds on a cluster (its projects and its permission level) is carried over when given. An
    unbounded request (admin, no cluster scope) is not bound. A grant is an object or a mapping with
    cluster_id, projects and permissions.
    """
    if get_current_allowed_clusters() is None:
        return
    grants = list(grants)
    held_elsewhere = await held(db, caller_email, {_field(grant, "cluster_id") for grant in grants})
    kept = already or {}

    def carried_over(cluster_id: str, permissions: Any, project: str) -> bool:
        projects, level = kept.get(cluster_id, (set(), -1))
        return project in projects and granted_level(permissions) <= level

    refused = sorted({
        f"{_field(grant, 'cluster_id')}/{project}"
        for grant in grants
        if _field(grant, "cluster_id") in held_elsewhere
        for project in (_field(grant, "projects") or [])
        if not carried_over(_field(grant, "cluster_id"), _field(grant, "permissions"), project)
        # important: `default` counts like '*' there. It carries the other party's instances, and a
        # key reaches them through the raw proxy and /proxy (owner decision 2026-11-05, as the LXD
        # TLS keys in services/lxd_key_bounds.py already refuse it).
        and (project in ("*", "default") or not project_is_visible(_field(grant, "cluster_id"), project))
    })
    if refused:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Not permitted to grant these projects: {refused}. Another organization also "
                "holds that cluster, so a key may name only your own projects there, never the "
                "default project or every project. "
                "A project the key already holds is kept at its current permission level. "
                "An administrator can issue a wider key."
            ),
        )


async def refuse_a_project_another_party_holds(
    db: AsyncSession, api_key: Mapping[str, Any], cluster_id: str, project_name: str
) -> None:
    """Raise 403 when another organization's policies already name this project on this cluster.

    note: org_scope.project_is_visible cannot stand in for this. The external router carries no
    user scope, so it answers True for every project there.
    """
    from app.models.db_models import TenantAccessPolicy, TenantPolicyRule
    from app.services.organization_owners import owners_of

    holders = set((await db.execute(
        select(TenantAccessPolicy.organization_id)
        .join(TenantPolicyRule, TenantPolicyRule.policy_id == TenantAccessPolicy.id)
        .where(and_(
            TenantPolicyRule.cluster_id == cluster_id,
            TenantPolicyRule.project_name == project_name,
        ))
        .distinct()
    )).scalars().all())
    if not holders:
        return
    key_id = str(api_key.get("id") or "")
    creator = str(api_key.get("created_by") or "")
    owners = await owners_of(db, holders)
    own_orgs = set(await cluster_parties.run_orgs(db, creator)) if creator else set()

    def on_the_keys_side(org_id: int) -> bool:
        # note: any owner row, an enrolled co-owner's included. The key already provisions into an
        # organization it owns, and that organization's own policies naming the project are its
        # own purchase, not another party's. Co-ownership lends no party for managing keys
        # (cluster_parties), a different question.
        keys = {row.api_key_id for row in owners.get(org_id, [])}
        return (bool(key_id) and key_id in keys) or org_id in own_orgs

    if not all(on_the_keys_side(org_id) for org_id in holders):
        raise HTTPException(
            status_code=403,
            detail=(
                f"No permission for project {project_name}: another organization holds it "
                f"on cluster {cluster_id}"
            ),
        )


def rule_outside_the_key_grant(api_key: Mapping[str, Any], cluster_id: str, project_name: str) -> Optional[str]:
    """Why a rule a key writes reaches past the key's own grant, or None when the grant covers it."""
    grants: List[Dict[str, Any]] = [g for g in api_key.get("cluster_permissions", []) or [] if isinstance(g, dict)]
    by_cluster = {str(g.get("cluster_id") or ""): g for g in grants}
    grant = by_cluster.get(cluster_id)
    if grant is None:
        return f"rule names cluster '{cluster_id}', which this key is not granted"
    projects = grant.get("projects") or ["default"]
    if "*" not in projects and project_name not in projects:
        return f"rule names project '{project_name}' on '{cluster_id}', which this key is not granted"
    return None
