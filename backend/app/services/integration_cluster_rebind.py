"""Which personal clusters an integration push may rebind, and the record each rebind leaves.

A member sync ran reconcile_personal_clusters for every pushed email. A key that pushed another
partner's email into its own organization moved that partner's unbound clusters into it, where the
key's own partner is a member. The share audit on that path checks a cluster's configuration, not
anyone's consent.

The rule: a push rebinds personal clusters only for the key's creator, the `created_by` that
create_api_key writes into the key's metadata when a partner mints it. Any other pushed email
triggers no rebind, and neither does a key without a creator or an add-on credential. Every
rebind is recorded with the cluster, the organization it left, the one it joined and the key id.
Nothing else of the key reaches the record.

note: the match is exact. `registered_by` holds the registrant's login email and the creator is
that same login, so another spelling would find no row to move anyway.
"""

import logging
from typing import Any, List, Mapping, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.org_scope import reconcile_personal_clusters
from app.services.activity_logger import get_activity_logger

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

REBIND_ACTION = "integration.cluster.rebind"


def creator_of(api_key: Mapping[str, Any]) -> Optional[str]:
    """The login that minted the key, or None for a key whose metadata does not say.

    important: None for an add-on credential. Its created_by is whoever installed or rotated it,
    an Admin included, while a third party holds it. A push through it naming that login could
    make an organization the add-on controls that person's primary one and move their clusters
    there, which is the move this module exists to prevent.
    """
    if api_key.get("extension_install_id") is not None:
        return None
    creator = api_key.get("created_by")
    return creator if isinstance(creator, str) and creator.strip() else None


def may_rebind(email: str, creator: Optional[str]) -> bool:
    return creator is not None and email == creator


def _record(cluster_id: str, from_org: Optional[int], to_org: int, email: str, api_key_id: str) -> None:
    try:
        activity_logger.log_activity(
            action=REBIND_ACTION,
            target_type="cluster",
            target_id=cluster_id,
            target_host=cluster_id,
            target_organization_id=to_org,
            details={
                "cluster_id": cluster_id,
                "from_organization_id": from_org,
                "to_organization_id": to_org,
                "registered_by": email,
                "api_key_id": api_key_id,
            },
        )
    except Exception:  # pragma: no cover - a failed audit write must not fail the sync
        logger.warning("Could not write the audit record for %s", REBIND_ACTION, exc_info=True)


async def rebind_creator_clusters(
    db: AsyncSession, email: str, *, api_key_id: str, creator: Optional[str]
) -> int:
    """Bind the creator's unbound clusters to their organization. Any other email moves nothing."""
    if not may_rebind(email, creator):
        return 0
    moved: List[Tuple[str, Optional[int], int]] = []
    rebound = await reconcile_personal_clusters(
        db, email, on_rebind=lambda cluster_id, from_org, to_org: moved.append((cluster_id, from_org, to_org))
    )
    # note: recorded after reconcile_personal_clusters committed, so a rolled-back move leaves no record
    for cluster_id, from_org, to_org in moved:
        _record(cluster_id, from_org, to_org, email, api_key_id)
    return rebound
