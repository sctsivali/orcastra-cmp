"""State that belongs to a whole cluster is read or changed only where no other party holds it.

LXD auth groups and identities, a server-scope key, a storage pool and the cluster's own trust
belong to no project. A server `admin` key, a group with any permission list, an identity added to
or removed from a group, or a pool reconfigured or deleted reaches every project on the cluster,
and a group or identity listing carries every project's URLs and client certificates. A non-admin
may touch them only when nobody outside their side holds the cluster. Decided by party
(services.cluster_parties), not by binding count, so a partner's own buyer organizations do not
make the partner's cluster foreign, which is the rule the v1.0.0-RC4-hotfix line enforces.

important: the party is asked for every non-admin, not only where org_scope.visible_projects_for
has a bound. On this line the bound counts bindings (org_scope._clusters_with_several_orgs), so a
cluster one other organization holds alone is unbounded for a partner who reaches it through a
membership there, and an early return on "unbounded" would hand that partner the whole cluster.
"""

import logging
from typing import Iterable, List, Optional, Set

from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser
from app.core.org_scope import get_current_allowed_clusters, verify_cluster_access
from app.core.rbac import Role, get_highest_role, resolve_roles
from app.services import cluster_parties

__all__ = [
    "SHARED_CLUSTER_DETAIL",
    "clusters_held_elsewhere",
    "refuse_unless_the_party_holds_it_whole",
    "the_party_holds_it_whole",
    "verify_default_project_write",
    "verify_whole_cluster_access",
    "whole_clusters",
]

logger = logging.getLogger(__name__)

# important: the harness matches this sentence (rc4hf2 specs) and the hotfix line sends it, so a
# client sees the same refusal whichever release it talks to.
SHARED_CLUSTER_DETAIL = "Not permitted on a cluster shared with another organization"


def _unrestricted(user: Optional[AuthenticatedUser]) -> bool:
    # note: an admin passes without a database read.
    if user is None:
        # Dev mode answers no user, and org_scope_dependency then sets no scope either. A request
        # with a scope and no user is refused by the party read below, which knows no email.
        return get_current_allowed_clusters() is None
    return get_highest_role(resolve_roles(user)) == Role.ADMIN


async def verify_whole_cluster_access(cluster_id: str, db: AsyncSession, user: Optional[AuthenticatedUser]) -> None:
    """Raise 403 unless this request may read or change state that belongs to the whole cluster."""
    verify_cluster_access(cluster_id)
    if _unrestricted(user):
        return
    await _refuse_where_another_party_holds(cluster_id, db, user)


async def verify_default_project_write(
    cluster_id: str, owner: Optional[str], db: AsyncSession, user: Optional[AuthenticatedUser]
) -> None:
    """Raise 403 when a network write lands in the default project of a cluster another party holds.

    important: a project bound always leaves `default` visible, and the default project holds what
    every project on the cluster leans on: bridge networks (only `default` can hold them), the OVN
    uplinks, and the ACLs those networks use. Changing one is a change to the whole cluster.
    """
    if (owner or "default") == "default":
        await verify_whole_cluster_access(cluster_id, db, user)


async def refuse_unless_the_party_holds_it_whole(cluster_id: str, db: AsyncSession, user: Optional[AuthenticatedUser]) -> None:
    """Raise 403 unless no holder outside this user's party holds the cluster, whatever the request scope.

    important: for a write that decides who holds the cluster (binding it into an organization,
    registering it again). A tenant row reaches a cluster but never owns it, so the party is asked
    even where the request scope would let the caller through.
    """
    if _unrestricted(user):
        return
    await _refuse_where_another_party_holds(cluster_id, db, user)


async def the_party_holds_it_whole(cluster_id: str, db: AsyncSession, user: Optional[AuthenticatedUser]) -> bool:
    """Whether no holder outside this user's party holds the cluster, asked of every role, an admin's too."""
    return not await _another_party_holds(cluster_id, db, user)


async def _refuse_where_another_party_holds(cluster_id: str, db: AsyncSession, user: Optional[AuthenticatedUser]) -> None:
    if await _another_party_holds(cluster_id, db, user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=SHARED_CLUSTER_DETAIL)


async def _another_party_holds(cluster_id: str, db: AsyncSession, user: Optional[AuthenticatedUser]) -> bool:
    try:
        held = await cluster_parties.clusters_held_by_another_party(db, getattr(user, "email", ""), {cluster_id})
    except SQLAlchemyError as exc:
        # A database that cannot say who holds the cluster changes nothing, and says so plainly
        # rather than as a 500.
        logger.warning("Could not read who holds %s: %s", cluster_id, type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not confirm who holds {cluster_id}, retry shortly",
        ) from exc
    return cluster_id in held


async def clusters_held_elsewhere(nodes: Iterable[str], db: AsyncSession, user: Optional[AuthenticatedUser]) -> Set[str]:
    """The clusters among `nodes` whose whole-cluster objects this request may not read."""
    wanted = sorted(set(nodes))
    if not wanted or _unrestricted(user):
        return set()
    allowed = get_current_allowed_clusters()
    if allowed is not None:
        # note: a cluster outside the request scope is filtered by the listing itself; asking the
        # party about it only costs queries.
        wanted = [node for node in wanted if node in allowed]
    try:
        return await cluster_parties.clusters_held_by_another_party(db, getattr(user, "email", ""), wanted)
    except SQLAlchemyError as exc:
        # note: a fleet listing narrows rather than fails: every cluster it could not judge is
        # left out, which is never more than the caller may see.
        logger.warning("Could not read who holds %d cluster(s), leaving them out: %s", len(wanted), type(exc).__name__)
        return set(wanted)


async def whole_clusters(nodes: Iterable[str], db: AsyncSession, user: Optional[AuthenticatedUser]) -> List[str]:
    """`nodes` less the clusters held elsewhere, in their order."""
    nodes = list(nodes)
    held = await clusters_held_elsewhere(nodes, db, user)
    return [node for node in nodes if node not in held]
