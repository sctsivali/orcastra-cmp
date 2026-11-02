"""What an LXD client key (My Keys) may grant.

A My Keys record is a real LXD client certificate with its private key. An `admin` key is
provisioned server-scope, so it reaches every project on the cluster, and a `standard` key is
operator on the one project it names. Minting checked the cluster only, so a partner held to a
project bound could mint an operator key for another organization's project, or a server key on a
cluster another party holds (live on dev 2026-11-01, harness rc4hf2-23 R-28). Who may download or
delete a record is routers/my_keys._owned_by_caller.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser
from app.core.org_scope import verify_project_access
from app.core.whole_cluster_access import verify_whole_cluster_access

__all__ = ["project_a_new_key_names"]


async def project_a_new_key_names(
    node: str, project: str, auth_level: str, db: AsyncSession, user: AuthenticatedUser
) -> str:
    """Authorize what a new key grants and return the project to provision and record.

    important: before any certificate is generated or any LXD call is made. A server key needs the
    whole cluster (core.whole_cluster_access), a standard key needs its project inside the bound.
    """
    if auth_level == "admin":
        await verify_whole_cluster_access(node, db, user)
        return (project or "default").strip() or "default"
    return verify_project_access(node, project)
