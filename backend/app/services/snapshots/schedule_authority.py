"""Whether a schedule may still fire, asked again every single time.

A schedule is a standing instruction that outlives the session that made it by months. Everything a
request path relies on to answer "may this happen" is absent here, and absent in the permissive
direction: `verify_cluster_access` is documented as "a no-op for admin / dev mode / background
tasks", `apply_cluster_filter` "returns all clusters if no scope is active", and
`check_tenant_permission` "returns True on a None ACL. In a worker that is unconditional, so it
fails OPEN". None of them is called from this package, and a ratchet holds that.

The shape is deliberately `dispatcher._still_authorised`: reload rather than trust the row, return a
REASON rather than raise, and record the reason rather than discard it. An operator asking why their
nightly snapshot stopped gets an answer instead of silence.

Two differences from that precedent, both chosen rather than inherited:

* It reads `organization_clusters`, the authoritative table, where the dispatcher reads a JSON copy
  a sweep reconciles on a timer. Revocation is therefore effective at the next tick rather than at
  the next reconcile.
* A refusal here distinguishes "terminal for the schedule" from "terminal for this run". A delivery
  that drops is one lost event; a months-long schedule that silently drops runs is worse than one
  that stops and says so.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.models.db_models import OrganizationCluster

logger = logging.getLogger(__name__)

# How many consecutive "the instance is not there" answers before the schedule stops asking. An
# instance can be absent because it is mid-migration or because a listing was served stale; three
# ticks of a schedule is hours, which is long enough that absence means gone.
MISSING_INSTANCE_GRACE = 3


@dataclass(frozen=True)
class Refusal:
    """Why this schedule must not fire now.

    `terminal` pauses the schedule and shows the message to an operator. Not terminal means try
    again at the next slot: the cluster was unreachable, not the grant withdrawn.
    """

    code: str
    message: str
    terminal: bool


async def db_refusal_for(db: Any, schedule: Any) -> Optional[Refusal]:
    """The refusals answerable from Postgres alone. Cheap, so it runs before any cluster call.

    important: the question is whether the OWNING ORGANIZATION still holds this cluster, not
    whether the person who created the schedule can still see it. Roles arrive only in a JWT and
    there is no users table, so "is this operator still a Partner" is not answerable here at all.
    Pretending otherwise by consulting `registered_by` would be worse than admitting it: that
    column keeps a cluster reachable by whoever first registered it even after it has been rebound
    to a different organization, which is exactly the grant a months-old schedule must not inherit.
    """
    if schedule.state != "active":
        return Refusal(
            code="schedule_not_active",
            message="This schedule is paused.",
            terminal=True,
        )

    if schedule.organization_id is None:
        return Refusal(
            code="schedule_has_no_organization",
            message=(
                "The organization that owned this schedule no longer exists, so nothing can "
                "vouch for its access to the cluster."
            ),
            terminal=True,
        )

    bound = await db.execute(
        select(OrganizationCluster.cluster_id).where(
            OrganizationCluster.organization_id == schedule.organization_id,
            OrganizationCluster.cluster_id == schedule.cluster_id,
        )
    )
    if bound.first() is None:
        return Refusal(
            code="cluster_left_the_organization",
            message=(
                f"Cluster '{schedule.cluster_id}' is no longer bound to the organization that "
                "created this schedule."
            ),
            terminal=True,
        )

    return None


def fingerprint_refusal(stored: Optional[str], current: Optional[str]) -> Optional[Refusal]:
    """Refuse when the cluster behind the name is demonstrably a different registration.

    important: `cluster_fingerprint` returns None for BOTH "nothing is pinned" and "Vault could not
    be read", and it swallows every exception to do it. Treating None as a mismatch would pause
    every schedule on the fleet during a Vault outage; treating a mismatch as None would let a
    re-registered cluster inherit another organization's schedule. So the refusal fires only when
    both values are present and differ, and the ambiguity is stated here rather than discovered
    later.

    `cluster_id` is free text with no foreign key in this schema, so the name alone cannot tell a
    cluster from its replacement.
    """
    if not stored or not current:
        return None
    if stored == current:
        return None
    return Refusal(
        code="cluster_is_a_different_registration",
        message=(
            "The cluster this schedule was created against has been re-registered since, so it "
            "is no longer the same cluster by anything other than its name."
        ),
        terminal=True,
    )


def cluster_refusal_for(
    schedule: Any,
    policy: Dict[str, Any],
    instance_config: Optional[Dict[str, Any]],
    instance_present: bool,
) -> Optional[Refusal]:
    """The refusals that need the cluster's own answer. Pure: the caller does the reading.

    Pure on purpose. Every branch here is a decision an operator will one day have to defend, and a
    function that also opens sockets cannot be exercised against the twenty states that matter.
    """
    if not instance_present:
        # Not terminal on its own. The caller counts consecutive misses and pauses at the grace,
        # because an instance can be absent from one listing and present in the next.
        return Refusal(
            code="instance_not_found",
            message=(
                f"Instance '{schedule.instance_name}' was not found in project "
                f"'{schedule.project}' on '{schedule.cluster_id}'."
            ),
            terminal=False,
        )

    stored_uuid = getattr(schedule, "instance_uuid", None)
    current_uuid = (instance_config or {}).get("volatile.uuid")
    if stored_uuid and current_uuid and stored_uuid != current_uuid:
        return Refusal(
            code="instance_is_a_different_instance",
            message=(
                f"'{schedule.instance_name}' is not the instance this schedule was created "
                "against. It was deleted and something else was created under the same name."
            ),
            terminal=True,
        )

    # The owner's decision, applied at fire time rather than only at creation. An instance can
    # acquire an upstream schedule months after ours was made, which is precisely the case a
    # creation-time check cannot see.
    if policy.get("externally_managed"):
        existing = policy.get("schedule") or "a schedule"
        return Refusal(
            code="upstream_schedule_appeared",
            message=(
                f"'{schedule.instance_name}' is now snapshotted by the metered product outside "
                f"this console ({existing}). Orcastra will not run a second schedule beside it."
            ),
            terminal=True,
        )

    return None


def refusal_is_terminal(refusal: Refusal, consecutive_failures: int) -> bool:
    """A non-terminal refusal becomes terminal once it has repeated enough to mean something."""
    if refusal.terminal:
        return True
    if refusal.code == "instance_not_found":
        return consecutive_failures + 1 >= MISSING_INSTANCE_GRACE
    return False


def forbidden_scope_helpers() -> List[str]:
    """The org-scope helpers no module in this package may name.

    Kept here as data rather than only in the ratchet so the rule is readable next to the code it
    governs. Every one of these answers "allowed" when the ContextVar it reads is unset, which is
    always, in a task the lifespan started.
    """
    return [
        "verify_cluster_access",
        "verify_tenant_permission",
        "check_tenant_permission",
        "apply_cluster_filter",
        "get_current_allowed_clusters",
        "get_scope_cache_key",
        "filter_projects_by_acl",
        "filter_instances_by_acl",
    ]
