"""What an LXD client key (My Keys) may grant.

A My Keys record is a real LXD client certificate with its private key. An `admin` key is
provisioned server-scope, so it reaches every project on the cluster, and a `standard` key is
operator on the one project it names. Minting checked the cluster only, so a partner held to a
project bound could mint an operator key for another organization's project, or a server key on a
cluster another party holds (live on dev 2026-11-01, harness rc4hf2-23 R-28). Who may download or
delete a record is routers/my_keys._owned_by_caller.

important: the key talks to LXD directly, so nothing the dashboard enforces on its own writes
(services/instance_security_policy, the safe allowlist on a shared cluster) applies to it. On a
cluster another party holds, operator on the `default` project, which a project bound always leaves
visible, lets the key create a privileged container with a host disk on that party's host. There a
standard key may name only a project the caller's own organizations' policies name, and only once
LXD itself isolates that project (restricted, own features), which is checked against the live
project before the identity is created.
"""

from dataclasses import dataclass
from typing import Any, Set

from fastapi import HTTPException, status
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser
from app.core.lxd_path import safe_segment
from app.core.org_scope import verify_project_access
from app.core.rbac import Role, get_highest_role, resolve_roles
from app.core.whole_cluster_access import verify_whole_cluster_access
from app.services import cluster_parties

__all__ = ["KeyTarget", "authorize_new_key", "refuse_an_unisolated_project"]


@dataclass(frozen=True)
class KeyTarget:
    """The project a new key is provisioned on, and whether LXD must prove it isolated first."""

    project: str
    isolated_project_required: bool = False


async def _projects_own_policies_name(db: AsyncSession, email: str, cluster_id: str) -> Set[str]:
    from app.models.db_models import TenantAccessPolicy, TenantPolicyRule

    runs = sorted(await cluster_parties.run_orgs(db, email))
    if not runs:
        return set()
    names = (await db.execute(
        select(TenantPolicyRule.project_name)
        .join(TenantAccessPolicy, TenantAccessPolicy.id == TenantPolicyRule.policy_id)
        .where(and_(TenantPolicyRule.cluster_id == cluster_id, TenantAccessPolicy.organization_id.in_(runs)))
        .distinct()
    )).scalars().all()
    return {name for name in names if name and name != "*"}


async def authorize_new_key(
    node: str, project: str, auth_level: str, db: AsyncSession, user: AuthenticatedUser
) -> KeyTarget:
    """Authorize what a new key grants and return where to provision it.

    important: before any certificate is generated or any LXD call is made. A server key needs the
    whole cluster (core.whole_cluster_access), a standard key needs its project inside the bound,
    and on a cluster another party holds also a project of the caller's own (module docstring).
    """
    if auth_level == "admin":
        await verify_whole_cluster_access(node, db, user)
        return KeyTarget((project or "default").strip() or "default")
    project = verify_project_access(node, project)
    if get_highest_role(resolve_roles(user)) == Role.ADMIN:
        return KeyTarget(project)
    if node not in await cluster_parties.clusters_held_by_another_party(db, user.email, {node}):
        return KeyTarget(project)
    if project == "default" or project not in await _projects_own_policies_name(db, user.email, node):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Another organization also holds {node}, so a Standard key there may name only a "
                "project your own organization's access policies name, never the default project. "
                "An administrator can issue a wider key."
            ),
        )
    return KeyTarget(project, isolated_project_required=True)


def refuse_an_unisolated_project(client: Any, node: str, project: str) -> None:
    """Raise 403 unless LXD isolates `project` the way a shared cluster requires (blocking read)."""
    from app.services.instance_security_policy import shared_project_findings

    try:
        config = dict(getattr(client.projects.get(safe_segment(project)), "config", None) or {})
    except Exception as exc:  # noqa: BLE001 - an unreadable project is not a proven isolated one
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Could not confirm that project {project} on {node} is isolated, so no key was issued.",
        ) from exc
    missing = [finding.field for finding in shared_project_findings(config)]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Project {project} on {node} is not isolated ({', '.join(missing)}), so an LXD key on "
                "it would reach the host another organization's instances run on. An administrator "
                "can restrict the project first."
            ),
        )
