"""Whether a schedule may still fire, re-checked on every run.

A schedule outlives the session that made it by months, and the request-path authorization helpers
fail open in a background task: `verify_cluster_access`, `apply_cluster_filter` and
`check_tenant_permission` all answer "allowed" when their ContextVar is unset. None of them is
called from this package (a ratchet test enforces it); authorization is rebuilt from current
database rows.

The shape follows the webhook dispatcher's `_still_authorised`: reload rather than trust the row,
return a reason rather than raise, and record the reason so an operator can see why a schedule
stopped. Two differences: this reads `organization_clusters` directly instead of a JSON copy a sweep
reconciles on a timer, so revocation takes effect at the next tick; and a refusal says whether it
ends the schedule or only this run.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, or_, select

from app.core.config import get_settings

from app.models.db_models import (
    Organization,
    OrganizationCluster,
    OrganizationMember,
    TenantAccessPolicy,
    TenantPolicyRule,
)

logger = logging.getLogger(__name__)

# Consecutive "the instance is not there" answers before the schedule pauses, because one listing
# can be served stale. Misses are retried with the scheduler's failure backoff, not once per slot.
MISSING_INSTANCE_GRACE = 3


DEFAULT_VIEW_ONLY_FOR_THE_OWNER = "default_is_view_only_for_the_owner"
OWNER_ROLE_UNREADABLE = "schedule_owner_role_unreadable"
PURCHASE_TERM_PASSED = "purchase_term_passed"


@dataclass(frozen=True)
class Refusal:
    """Why this schedule must not fire now.

    `terminal` pauses the schedule and shows the message to an operator. A non-terminal refusal is
    retried after the scheduler's failure backoff, ex; the cluster was unreachable, until repeated
    failures pause the schedule too.
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

    ``registered_by`` is not used: it is a historical registration fact that can survive a rebind,
    and reading it as affiliation would let a former registrant keep an unattended write after
    leaving the organization.
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

    Selection happens after project authority. An operator may belong to two organizations on one
    shared cluster while only the second owns the project; picking the first cluster binding would
    give a false 403 and store the wrong organization on the schedule.
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

    No authorization snapshot from the HTTP request is trusted later, so every use proves three
    facts: the organization is still active and bound to the cluster, the actor still created or
    belongs to it, and a named project on a shared cluster is still recorded by its policy.
    ``default`` on a shared cluster and any project on a single-owner cluster pass, matching the
    Partner request bound. Admins get no background exemption, and the scheduler process is never
    an actor.

    Project ownership does not require a tenant assignment: dashboard-made projects are recorded in
    an unassigned ``dashboard:<cluster>`` policy, and assignment expiry is a Tenant ACL concern.
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

    Roles arrive only in a JWT, so the worker does not reconstruct one. It requires the stored
    organization, the cluster/project ownership evidence and the creator's current affiliation;
    ``registered_by`` is not affiliation. The one role the worker reads, for the view-only
    `default` exemption, comes from Authentik in default_view_only_refusal.
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


async def default_view_only_refusal(db: Any, schedule: Any, *, roles: Any = None) -> Optional[Refusal]:
    """`default` where another party holds the cluster is view only for the schedule's owner.

    This also stops schedules created before the rule. It is the browser's rule, judged from the
    owner: the party from the database (cluster_parties), the role from Authentik, because an
    Admin's schedule keeps running. The worker has no request role, so this is the one place it
    reads one; an unanswered identity provider defers the run rather than pausing it.

    Kept apart from standing_authority_refusal, which also decides which schedules and migration
    rows an owner may see: an owner must still see, pause and delete a schedule this stops.
    """
    from app.services.cluster_parties import default_view_only_clusters
    from app.services.creator_role import creator_is_admin

    if ((schedule.project or "").strip() or "default") != "default":
        return None
    held = await default_view_only_clusters(db, schedule.owner_email or "", {schedule.cluster_id})
    if schedule.cluster_id not in held:
        return None
    admin = await creator_is_admin(schedule.owner_email, roles=roles)
    if admin:
        return None
    if admin is None:
        return Refusal(
            code=OWNER_ROLE_UNREADABLE,
            message=(
                "Who owns this schedule could not be checked because the identity provider did not "
                "answer. It is tried again at the next slot."
            ),
            terminal=False,
        )
    return Refusal(
        code=DEFAULT_VIEW_ONLY_FOR_THE_OWNER,
        message=(
            f"The default project on cluster '{schedule.cluster_id}' is shared with another "
            "organization, so it is view only for whoever owns this schedule, and the schedule "
            "stopped. Schedule the instance from one of the owner's own projects, or ask an "
            "administrator."
        ),
        terminal=True,
    )


async def paid_term_refusal(db: Any, schedule: Any) -> Optional[Refusal]:
    """The owner reaches the cluster only by a purchase past its paid term: the schedule pauses.

    Past the term the owner may only look (core/sale_term.py), and a snapshot is a write on the
    seller's cluster. Kept apart from standing_authority_refusal for the reason
    default_view_only_refusal is: the owner must still see, pause and delete it. A renewed term lets
    the owner resume it.
    """
    from app.core.sale_term import ENDED, READ_ONLY, purchase_phases

    if not get_settings().tenant_term_enforcement_enabled:
        return None
    phase = (await purchase_phases(db, schedule.owner_email)).get(schedule.cluster_id)
    if phase not in (READ_ONLY, ENDED):
        return None
    return Refusal(
        code=PURCHASE_TERM_PASSED,
        message=(
            f"The owner of this schedule reaches cluster '{schedule.cluster_id}' only through a "
            "purchase whose paid term has passed, so it may no longer change anything there and "
            "the schedule stopped. Resume it after the purchase is renewed."
        ),
        terminal=True,
    )


def fingerprint_refusal(stored: Optional[str], current: Any) -> Optional[Refusal]:
    """Refuse when the cluster behind the name is demonstrably a different registration.

    important: `cluster_fingerprint` returns None both when nothing is pinned and when Vault could
    not be read. Treating None as a mismatch would pause every schedule during a Vault outage;
    treating a mismatch as None would let a re-registered cluster inherit another organization's
    schedule. So the refusal fires only when both values are present and differ.

    `cluster_id` is free text with no foreign key, so the name alone cannot tell a cluster from its
    replacement. ``current`` is the cluster's `PinState` or a bare fingerprint; a public-CA cluster
    that renews its certificate is still the same cluster while the stored fingerprint is in its
    renewal lineage.
    """
    from app.services.lxd_certs.pinning import PinState

    if not stored or not current:
        return None
    if isinstance(current, PinState):
        if current.accepts(stored):
            return None
    elif stored == current:
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
    """The refusals that need the cluster's own answer.

    Pure: the caller does the reading, so every branch can be tested without a cluster.
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

    # Checked at fire time as well as at creation: an instance can acquire an upstream schedule
    # months after this one was made.
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

    Kept here as well as in the ratchet test so the rule is readable next to the code it governs.
    Each answers "allowed" when the ContextVar it reads is unset, which it always is in a task the
    lifespan started.
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
        # The view-only `default` rule's request half: a worker judges it from the owner instead
        # (default_view_only_refusal).
        "default_is_view_only",
        "refuse_default_operation",
        "verify_project_access",
        "project_is_visible",
    ]
