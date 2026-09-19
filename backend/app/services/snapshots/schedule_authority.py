"""Whether a schedule may still fire, asked again every single time.

A schedule is a standing instruction that outlives the session that made it by months. Everything a
request path relies on to answer "may this happen" is absent here, and absent in the permissive
direction: `verify_cluster_access` is documented as "a no-op for admin / dev mode / background
tasks", `apply_cluster_filter` "returns all clusters if no scope is active", and
`check_tenant_permission` "returns True on a None ACL. In a worker that is unconditional, so it
fails OPEN". None of them is called from this package, and a ratchet holds that.

The shape is deliberately `dispatcher._still_authorised`: reload rather than trust the row, return a
REASON rather than raise, and record the reason rather than discard it. An operator asking why their
nightly snapshot stopped gets an answer instead of silence. Unlike a request dependency, the proof
comes entirely from current database rows and cannot fail open because a ContextVar is absent.

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

from sqlalchemy import and_, or_, select

from app.models.db_models import (
    Organization,
    OrganizationCluster,
    OrganizationMember,
    TenantAccessPolicy,
    TenantPolicyRule,
)

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


async def _affiliated_organizations(
    db: Any,
    *,
    actor_email: str,
    cluster_id: str,
) -> List[int]:
    """Active organizations the actor currently belongs to and that bind the cluster.

    ``registered_by`` is deliberately insufficient here. It is a historical registration fact
    and can survive a rebind; using it as current organization affiliation would let a former
    registrant keep an unattended write after leaving the organization.
    """
    rows = await db.execute(
        select(Organization.id)
        .join(OrganizationCluster, OrganizationCluster.organization_id == Organization.id)
        .outerjoin(
            OrganizationMember,
            and_(
                OrganizationMember.organization_id == Organization.id,
                OrganizationMember.user_email == actor_email,
            ),
        )
        .where(
            Organization.is_active == True,  # noqa: E712 - SQLAlchemy expression
            OrganizationCluster.cluster_id == cluster_id,
            or_(
                Organization.created_by == actor_email,
                OrganizationMember.id.is_not(None),
            ),
        )
        .order_by(Organization.id)
        .distinct()
    )
    return list(rows.scalars().all())


async def _cluster_owner_keys(db: Any, cluster_id: str) -> set[tuple[str, int]]:
    owners = await db.execute(
        select(OrganizationCluster.organization_id, OrganizationCluster.id).where(
            OrganizationCluster.cluster_id == cluster_id
        )
    )
    return {
        ("org", org_id) if org_id is not None else ("unbound", row_id)
        for org_id, row_id in owners.all()
    }


async def authorizing_organization_id(
    db: Any,
    *,
    actor_email: Optional[str],
    cluster_id: str,
    project: str,
) -> Optional[int]:
    """Pick the actor's organization that currently authorizes this exact target.

    Selection happens after project authority, not before it. An operator may belong to two
    organizations on one shared cluster, while only the second owns the requested project. Picking
    the first cluster binding would turn that valid target into a false 403 and persist the wrong
    organization on the schedule.
    """
    if not actor_email:
        return None
    candidates = await _affiliated_organizations(
        db,
        actor_email=actor_email,
        cluster_id=cluster_id,
    )
    if not candidates:
        return None
    if len(await _cluster_owner_keys(db, cluster_id)) <= 1 or project == "default":
        return candidates[0]

    grant = await db.execute(
        select(TenantAccessPolicy.organization_id)
        .join(TenantPolicyRule, TenantPolicyRule.policy_id == TenantAccessPolicy.id)
        .join(Organization, Organization.id == TenantAccessPolicy.organization_id)
        .join(
            OrganizationCluster,
            and_(
                OrganizationCluster.organization_id == TenantAccessPolicy.organization_id,
                OrganizationCluster.cluster_id == TenantPolicyRule.cluster_id,
            ),
        )
        .where(
            TenantAccessPolicy.organization_id.in_(candidates),
            Organization.is_active == True,  # noqa: E712 - SQLAlchemy expression
            TenantPolicyRule.cluster_id == cluster_id,
            TenantPolicyRule.project_name == project,
        )
        .order_by(TenantAccessPolicy.organization_id)
        .limit(1)
    )
    row = grant.first()
    return row[0] if row is not None else None


async def standing_authority_refusal(
    db: Any,
    *,
    actor_email: Optional[str],
    organization_id: Optional[int],
    cluster_id: str,
    project: str,
) -> Optional[Refusal]:
    """Rebuild a schedule's durable authorization from current database state.

    The HTTP request stores no authorization snapshot that a worker may trust later. This query
    therefore proves three independent facts on every use: the organization is still active and
    bound to the cluster, the named actor still creates or belongs to that organization, and a
    named project on a shared cluster is still recorded by that organization's policy.

    ``default`` remains visible on a shared cluster, matching the Partner request bound. A cluster
    with one owner remains unrestricted by project, also matching that bound. Admin does not get a
    background exemption: this product intentionally treats a recurring schedule as an
    organization-owned standing commitment, not as a continuation of an Admin session. The
    scheduler process itself is never an actor and cannot authorize a row merely by running it.

    note: project-ownership rules deliberately do not require a tenant assignment. Dashboard-made
    projects are recorded in an unassigned ``dashboard:<cluster>`` policy, because the rule owns
    the Partner boundary without granting a Tenant access. Assignment expiry remains a Tenant ACL
    concern and cannot be used as schedule authority without breaking that contract.
    """
    if organization_id is None:
        return Refusal(
            code="schedule_has_no_organization",
            message=(
                "The organization that owned this schedule no longer exists, so nothing can "
                "vouch for its access to the cluster."
            ),
            terminal=True,
        )
    if not actor_email:
        return Refusal(
            code="schedule_owner_is_not_identifiable",
            message="The operator who authorized this schedule is no longer identifiable.",
            terminal=True,
        )

    binding = await db.execute(
        select(OrganizationCluster.id)
        .join(Organization, Organization.id == OrganizationCluster.organization_id)
        .where(
            Organization.id == organization_id,
            Organization.is_active == True,  # noqa: E712 - SQLAlchemy expression
            OrganizationCluster.cluster_id == cluster_id,
        )
    )
    if binding.first() is None:
        return Refusal(
            code="cluster_left_the_organization",
            message=(
                f"Cluster '{cluster_id}' is no longer bound to the active organization that "
                "created this schedule."
            ),
            terminal=True,
        )

    affiliated = await _affiliated_organizations(
        db,
        actor_email=actor_email,
        cluster_id=cluster_id,
    )
    if organization_id not in affiliated:
        return Refusal(
            code="schedule_owner_left_the_organization",
            message=(
                "The operator who authorized this schedule no longer belongs to the owning "
                "organization."
            ),
            terminal=True,
        )

    owner_keys = await _cluster_owner_keys(db, cluster_id)
    if len(owner_keys) <= 1 or project == "default":
        return None

    project_grant = await db.execute(
        select(TenantPolicyRule.id)
        .join(TenantAccessPolicy, TenantAccessPolicy.id == TenantPolicyRule.policy_id)
        .join(Organization, Organization.id == TenantAccessPolicy.organization_id)
        .join(
            OrganizationCluster,
            and_(
                OrganizationCluster.organization_id == TenantAccessPolicy.organization_id,
                OrganizationCluster.cluster_id == TenantPolicyRule.cluster_id,
            ),
        )
        .where(
            TenantAccessPolicy.organization_id == organization_id,
            Organization.is_active == True,  # noqa: E712 - SQLAlchemy expression
            TenantPolicyRule.cluster_id == cluster_id,
            TenantPolicyRule.project_name == project,
        )
    )
    if project_grant.first() is None:
        return Refusal(
            code="project_left_the_organization",
            message=(
                f"Project '{project}' on shared cluster '{cluster_id}' is no longer recorded "
                "by the organization that owns this schedule."
            ),
            terminal=True,
        )
    return None


async def db_refusal_for(db: Any, schedule: Any) -> Optional[Refusal]:
    """The refusals answerable from Postgres alone. Cheap, so it runs before any cluster call.

    Roles arrive only in a JWT, so the worker does not attempt to reconstruct one. It instead
    requires the persisted organization, the exact cluster/project ownership evidence, and the
    creator's current database-backed affiliation. Historical ``registered_by`` metadata is not
    current organization affiliation and cannot lend authority to this schedule after a rebind.
    """
    if schedule.state != "active":
        return Refusal(
            code="schedule_not_active",
            message="This schedule is paused.",
            terminal=True,
        )

    return await standing_authority_refusal(
        db,
        actor_email=schedule.owner_email,
        organization_id=schedule.organization_id,
        cluster_id=schedule.cluster_id,
        project=schedule.project,
    )


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
