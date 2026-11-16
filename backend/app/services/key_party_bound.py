"""A key a Partner created is held to its creator's projects on a cluster another party holds.

The browser bounds a Partner on such a cluster to the projects its own organizations' policies
name (org_scope.get_visible_projects), but that bound lives in request contextvars no key route
sets, so a key holding `*` there would reach another party's projects, pools and networks. A key
acts for its creator, so every request narrows its grants, in a copy, to the creator's bound
(org_scope.partner_projects_on); the key routes then apply their project-limited rules as usual.
An add-on credential's creator is its installer (key_default_view_only). Only clusters another
party holds are bounded, so a seller's key beside its own buyers keeps its grant: the buyers the
key itself sold onto the cluster join the creator's party here (cluster_parties.key_sale_orgs), and
a creator who reaches the cluster only by a purchase is bounded whatever the key owns
(cluster_parties.key_held_by_another_party). Inside the bound
a key may create and delete projects under the browser's project policy
(`project_change_refusal`, `shared_project_config`, `refuse_an_unisolated_project`).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple

from fastapi import HTTPException

from app.services.authentik_role_reading import CurrentRoleReader
from app.services.creator_role import creator_is_admin
from app.services.key_default_view_only import _creator

logger = logging.getLogger(__name__)

# The same window as the key metadata cache: a binding or policy change reaches a key within it.
BOUND_SECONDS = 30
PARTY_BOUNDED = "_party_bounded_clusters"
NOT_RECORDED = (
    ". Access to it could not be recorded; ask an administrator to add it to your organization's policy"
)
HELD = "_held_cluster_permissions"
# Clusters bounded only because the creator's admin role could not be read (fail closed).
ROLE_UNKNOWN = "_party_role_unknown_clusters"
BIND_SCREEN = "Settings > Integrations > Synced Organizations > Manage clusters"

# A project create or delete on one worker tells every worker through the shared cache.
_CHANGED_NS = "key_party_bound_changed"

_lock = threading.Lock()
# (creator, cluster, party key id) -> (read at, wall clock then, None when no other party holds it,
# else the bound). The key id is part of it: a key's own sale organizations join its party.
_bounds: Dict[Tuple[str, str, str], Tuple[float, float, Optional[FrozenSet[str]]]] = {}


def _changed_since(cluster_id: str, wall: float, *, unreadable_is_change: bool = False) -> bool:
    """Whether a worker recorded a change on `cluster_id` after `wall`: a project created or
    deleted, a binding created or removed, or an owner set changed (forget_cluster_bounds).

    `unreadable_is_change`: for a cached unbounded verdict, a stamp that cannot be read sends the
    caller back to the database (fail closed), since the change it missed may be the one that bounds
    the key. A held verdict is already the narrow one and is served on.
    """
    try:
        from app.core.cache import get_cache_manager

        stamp, authoritative = get_cache_manager().get_entry(_CHANGED_NS, cluster_id)
    except Exception:  # noqa: BLE001 - a cache hiccup must never break authentication
        return unreadable_is_change
    try:
        changed = stamp is not None and float(stamp) >= wall
    except (TypeError, ValueError):
        return True
    # important: while a configured Redis fails, the answer comes from this worker's memory, which
    # another worker's change never reached. It proves a change this worker made, never its absence.
    return changed or (not authoritative and unreadable_is_change)


def _publish_change(cluster_id: str) -> None:
    try:
        from app.core.cache import get_cache_manager

        get_cache_manager().set(_CHANGED_NS, cluster_id, time.time(), ttl=BOUND_SECONDS + 5)
    except Exception as exc:  # noqa: BLE001 - the local drop still happened; others expire in 30 s
        logger.warning("Could not publish a party bound change on %s: %s", cluster_id, type(exc).__name__)


async def _read_bound(db: Any, creator: str, cluster_id: str, key_id: str) -> Optional[FrozenSet[str]]:
    """None when no other party holds the cluster for this key, else the creator's bound there.

    `key_id` is cluster_parties.party_key_id: "" for an add-on credential, so only its installer's
    party counts."""
    from app.core.org_scope import partner_projects_on
    from app.services.cluster_parties import key_held_by_another_party

    if not await key_held_by_another_party(db, creator, key_id, cluster_id):
        return None
    return frozenset((await partner_projects_on(db, creator, {cluster_id})).get(cluster_id, {"default"}))


async def _bound(api_key: Mapping[str, Any], cluster_id: str, db: Any) -> Tuple[str, Optional[FrozenSet[str]]]:
    from app.services.cluster_parties import party_key_id

    if db is None:
        from app.core.database import async_session_maker

        async with async_session_maker() as session:
            return await _bound(api_key, cluster_id, session)
    creator = await _creator(api_key, db)
    key_id = party_key_id(api_key)
    cache_key = (creator.strip().lower(), cluster_id, key_id)
    now = time.monotonic()
    with _lock:
        cached = _bounds.get(cache_key)
    # important: every cached verdict is checked against the shared stamp, an unbounded one too. A
    # binding or owner-set change on another worker (forget_cluster_bounds) can make the cluster
    # another party's, and an unbounded `*` key must not outlive that by the window. One shared
    # cache read per granted cluster per request; an unreadable one re-reads an unbounded verdict.
    if cached is not None and now - cached[0] < BOUND_SECONDS and not _changed_since(
        cluster_id, cached[1], unreadable_is_change=cached[2] is None
    ):
        return creator, cached[2]
    wall = time.time()
    bound = await _read_bound(db, creator, cluster_id, key_id)
    with _lock:
        _bounds[cache_key] = (now, wall, bound)
    return creator, bound


async def party_bound(
    api_key: Mapping[str, Any], cluster_id: str, *, db: Any = None, roles: Optional[CurrentRoleReader] = None,
    role_seconds: Optional[float] = None, db_gate: Any = None,
) -> Tuple[Optional[FrozenSet[str]], bool]:
    """(the projects this key is held to on `cluster_id`, or None for no bound; whether the
    creator's role was unknown). An Admin's key and a cluster no other party holds have no bound.

    An identity provider that does not answer leaves the bound in place for that grant only (fail
    closed), so an outage narrows an Admin's key on a shared cluster instead of refusing the key
    everywhere. An unreadable party is a 503: the database is down for every request then.
    """
    try:
        if db_gate is None:
            creator, bound = await _bound(api_key, cluster_id, db)
        else:
            # Held for the database read only, never across the identity provider's wait.
            async with db_gate:
                creator, bound = await _bound(api_key, cluster_id, db)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - an unreadable party is not a verdict either way
        logger.warning("Party bound unavailable for a key on %s: %s", cluster_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="Authentication backend unavailable") from None
    if bound is None:
        return None, False
    if role_seconds is None:
        admin = await creator_is_admin(creator, roles=roles)
    else:
        # A caller that cannot wait for the identity provider (an event on a request path) takes
        # the bound, as an unanswered provider does.
        admin = await _role_within(creator, roles, role_seconds)
    if admin:
        return None, False
    if admin is None:
        logger.warning("Creator role unknown for a key on %s; held to the creator's bound", cluster_id)
    return bound, admin is None


# One role read per creator in flight. A read that outlives its caller's wait keeps going and fills
# creator_role's cache, so the next event does not wait again for an answer that was on its way.
_role_reads: Dict[str, "asyncio.Task[Optional[bool]]"] = {}


def _role_read_done(key: str, task: "asyncio.Task[Optional[bool]]") -> None:
    if _role_reads.get(key) is task:
        _role_reads.pop(key, None)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("Creator role read failed: %s", type(task.exception()).__name__)


async def _role_within(creator: str, roles: Optional[CurrentRoleReader], seconds: float) -> Optional[bool]:
    loop = asyncio.get_running_loop()
    key = creator.strip().lower()
    task = _role_reads.get(key)
    if task is None or task.done() or task.get_loop() is not loop:
        task = loop.create_task(creator_is_admin(creator, roles=roles))
        _role_reads[key] = task
        task.add_done_callback(lambda done, k=key: _role_read_done(k, done))
    try:
        return await asyncio.wait_for(asyncio.shield(task), seconds)
    except asyncio.TimeoutError:
        return None
    except Exception:  # noqa: BLE001 - a failed read is an unknown role, as the provider's silence is
        return None


async def narrow_to_party_bound(
    api_key: Dict[str, Any], *, db: Any = None, roles: Optional[CurrentRoleReader] = None,
) -> Dict[str, Any]:
    """The key with every grant on a cluster another party holds narrowed to its creator's bound.

    important: returns a copy, never the cached document. The bound moves with the creator's
    policies and bindings, and the stored grant must come back when it does. The stored grants
    stay in `_held_cluster_permissions`, which /validate and /whoami report.
    """
    grants = [g for g in (api_key.get("cluster_permissions") or []) if isinstance(g, dict)]
    if not grants:
        return api_key
    narrowed: List[Dict[str, Any]] = []
    bounded: List[str] = []
    unknown: List[str] = []
    for grant in grants:
        cluster_id = str(grant.get("cluster_id") or "")
        bound, role_unknown = await party_bound(api_key, cluster_id, db=db, roles=roles)
        if bound is None:
            narrowed.append(grant)
            continue
        bounded.append(cluster_id)
        if role_unknown:
            unknown.append(cluster_id)
        narrowed.append(held_to(grant, bound))
    if not bounded:
        return api_key
    narrowed_key = {
        **api_key,
        "cluster_permissions": narrowed,
        HELD: api_key.get(HELD) or grants,
        PARTY_BOUNDED: bounded,
    }
    if unknown:
        narrowed_key[ROLE_UNKNOWN] = unknown
    return narrowed_key


def held_to(grant: Mapping[str, Any], bound: FrozenSet[str]) -> Dict[str, Any]:
    projects = grant.get("projects", ["default"])
    held = sorted(bound) if "*" in projects else sorted(p for p in projects if p in bound)
    # An empty list denies every project (api_key_capabilities.normalise_project_grant).
    return {**grant, "projects": held}


def is_party_bounded(api_key: Mapping[str, Any], cluster_id: str) -> bool:
    return cluster_id in (api_key.get(PARTY_BOUNDED) or [])


async def project_change_refusal(
    api_key: Mapping[str, Any], cluster_id: str, project_name: str, *, creating: bool, db: Any,
) -> Optional[HTTPException]:
    """Why a party-bounded key may not create, change or delete `project_name`, or None.

    Create: no other party's policy names the name, and the creator runs an organization bound to
    the cluster to record it under (org_scope.org_to_hold_project_grant, as the browser). Change or
    delete: the project is inside the bound; any other answers 404, as the browser does.
    """
    from app.core.org_scope import org_to_hold_project_grant
    from app.services.api_key_grant_bound import refuse_a_project_another_party_holds
    from app.services.cluster_parties import party_key_id

    grant = next(
        (g for g in api_key.get("cluster_permissions") or [] if isinstance(g, dict) and g.get("cluster_id") == cluster_id),
        None,
    )
    held = set((grant or {}).get("projects") or [])
    creator = await _creator(api_key, db)
    if not creating:
        # important: read again, not from the cache: a project deleted moments ago may be a name
        # another party has just created, and a stale bound would hand it to this key.
        fresh = await _read_bound(db, creator, cluster_id, party_key_id(api_key)) or frozenset()
        if project_name in held and project_name in fresh and project_name != "default":
            return None
        return HTTPException(status_code=404, detail=f"Project '{project_name}' not found in cluster '{cluster_id}'")
    try:
        # Judged as the creator the bound is judged by (the installer for an add-on).
        await refuse_a_project_another_party_holds(db, {**api_key, "created_by": creator}, cluster_id, project_name)
    except HTTPException as refusal:
        return refusal
    if await org_to_hold_project_grant(creator, cluster_id, db) is None:
        try:
            named: Optional[Tuple[List[str], int]] = await creator_organizations_for_refusal(
                db, creator, str(api_key.get("id") or "")
            )
        except Exception as exc:  # noqa: BLE001 - the names only explain the refusal; never turn it into a 500
            logger.warning("Could not name the creator's organizations for a refusal on %s: %s", cluster_id, type(exc).__name__)
            named = None
        return HTTPException(status_code=403, detail=_unbound_creator_detail(api_key, cluster_id, named))
    return None


async def creator_organizations_owned_by_key(db: Any, creator: str, key_id: str) -> Tuple[List[Tuple[int, str]], int]:
    """((id, name) of each organization the creator runs that this key owns, how many others the
    creator runs). Only the key's own organizations are named: the key holder syncs those already,
    and the creator's other organizations belong to integrations the holder may not see."""
    from app.models.db_models import Organization
    from app.services.cluster_parties import run_orgs

    from sqlalchemy import select

    runs = await run_orgs(db, creator)
    owned = sorted(org_id for org_id, owners in runs.items() if key_id and key_id in owners)
    if not owned:
        return [], len(runs)
    rows = (await db.execute(select(Organization.id, Organization.name).where(Organization.id.in_(owned)))).all()
    names = {org_id: name for org_id, name in rows}
    return [(org_id, names.get(org_id, "organization")) for org_id in owned], len(runs) - len(owned)


async def creator_organizations_for_refusal(db: Any, creator: str, key_id: str) -> Tuple[List[str], int]:
    """The same, with each organization written as "Name (id)" for a refusal."""
    owned, others = await creator_organizations_owned_by_key(db, creator, key_id)
    return [f"{name} ({org_id})" for org_id, name in owned], others


def _unbound_creator_detail(
    api_key: Mapping[str, Any], cluster_id: str, organizations: Optional[Tuple[List[str], int]],
) -> str:
    """The refusal for a create no organization of the creator can hold: the missing layer is the
    organization-to-cluster binding, not the key's grant (Edit Access) or its owner rows."""
    named, others = organizations if organizations is not None else ([], -1)
    if others < 0:
        whose = "The creator's organizations could not be read just now."
    elif named:
        whose = f"Organizations this key owns: {', '.join(named)}."
        if others:
            whose += f" The creator runs {others} other organization{'s' if others != 1 else ''}."
    elif others:
        whose = f"The key's creator runs {others} organization{'s' if others != 1 else ''}; this key owns none of them."
    else:
        whose = "The key's creator runs no organization."
    detail = (
        "None of the organizations this key's creator runs (created, or holds as Partner) is bound to "
        f"cluster '{cluster_id}', so a project created there could not be recorded to them. The key's "
        "cluster grant and its owner keys are a separate layer and do not record a project. "
        f"{whose} To fix it, bind one of them to '{cluster_id}' in {BIND_SCREEN} (an administrator, or "
        "a Partner of that organization whose party holds the cluster whole)."
    )
    if cluster_id in (api_key.get(ROLE_UNKNOWN) or []):
        detail += (
            " The creator's administrator role could not be confirmed because the identity provider "
            "did not answer, so the key was held to the creator's organizations. Retry in a moment."
        )
    return detail


async def record_key_created_project(
    api_key: Mapping[str, Any], cluster_id: str, project_name: str, *, db: Any = None,
) -> str:
    """Record a project a party-bounded key created to its creator's organization; the bound
    cache forgets the creator so the next call reaches the project. Returns the note for the
    answer ("" on success), as project_grants.record_created_project does."""
    from app.core.org_scope import org_to_hold_project_grant
    from app.services import project_grants

    if db is None:
        from app.core.database import async_session_maker

        async with async_session_maker() as session:
            return await record_key_created_project(api_key, cluster_id, project_name, db=session)
    creator = ""
    try:
        creator = await _creator(api_key, db)
        organization_id = await org_to_hold_project_grant(creator, cluster_id, db)
        if organization_id is None:
            logger.warning("No organization of the key's creator is bound to %s; %s was not recorded", cluster_id, project_name)
            return NOT_RECORDED
        return await project_grants.record_created_project(
            db, organization_id=organization_id, cluster_id=cluster_id, project_name=project_name, created_by=creator,
        )
    except Exception as exc:  # noqa: BLE001 - the project exists; say so rather than fail the create
        logger.error("Could not record project %s on %s for a key: %s", project_name, cluster_id, type(exc).__name__)
        return NOT_RECORDED
    finally:
        forget_creator_bound(creator, cluster_id)


def forget_cluster_bounds(cluster_id: str) -> None:
    """Drop every cached bound and view-only `default` verdict on `cluster_id`, here and on every
    worker. Called wherever a project on the cluster is created or deleted, whoever did it (a
    deleted name may be another party's next, and any creator's cached bound could still list it),
    and wherever a binding to the cluster is created or removed (it moves who holds the cluster)."""
    from app.services.key_default_view_only import forget_cluster_verdicts

    with _lock:
        for cache_key in [k for k in _bounds if k[1] == cluster_id]:
            _bounds.pop(cache_key, None)
    forget_cluster_verdicts(cluster_id)
    _publish_change(cluster_id)


async def forget_organization_bounds(db: Any, organization_ids: Iterable[int]) -> None:
    """forget_cluster_bounds for every cluster these organizations are bound to.

    Called once an owner-set change commits: a key that leaves an organization's owner set, or stops
    being its primary while owner sets are off, loses it as a sale organization
    (cluster_parties.key_sale_orgs), so its cached unbounded verdict must not outlive the change.
    Never raises: the change stands. When the bindings cannot be read, this worker drops every
    verdict and the others expire within BOUND_SECONDS.
    """
    from sqlalchemy import select

    from app.models.db_models import OrganizationCluster

    wanted = sorted({org_id for org_id in organization_ids if org_id is not None})
    if not wanted:
        return
    try:
        clusters = set((await db.execute(
            select(OrganizationCluster.cluster_id).where(OrganizationCluster.organization_id.in_(wanted))
        )).scalars().all())
    except Exception as exc:  # noqa: BLE001 - the owner change is committed; never fail its answer
        logger.warning("Could not read the clusters of organizations %s to drop key bounds: %s", wanted, type(exc).__name__)
        from app.services.key_default_view_only import forget_key_default_verdicts

        forget_party_bounds()
        forget_key_default_verdicts()
        return
    for cluster_id in sorted(clusters):
        forget_cluster_bounds(cluster_id)


def forget_creator_bound_of(api_key: Mapping[str, Any], cluster_id: str) -> None:
    """After a key deleted a project on `cluster_id`."""
    forget_cluster_bounds(cluster_id)


def forget_creator_bound(creator: str, cluster_id: str) -> None:
    """Drop a creator's cached bounds on one cluster, every key of theirs, after a project create,
    here and on every worker."""
    who = (creator or "").strip().lower()
    with _lock:
        for cache_key in [k for k in _bounds if k[0] == who and k[1] == cluster_id]:
            _bounds.pop(cache_key, None)
    _publish_change(cluster_id)


def shared_project_config(cluster_id: str, config: Mapping[str, Any]) -> Dict[str, str]:
    """The config a Partner's project on a shared cluster is created with, as the browser forces it
    (instance_security_policy.prepare_project_create_config): restricted, isolated features, and no
    other key but user.*. A refused key is the browser's 4xx."""
    from app.services.instance_security_policy import InstanceSecurityPolicyViolation, prepare_project_create_config

    try:
        return prepare_project_create_config(_shared_policy(cluster_id), config or {}, limits_allowed=True)
    except InstanceSecurityPolicyViolation as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc


def refuse_an_unisolated_project(cluster_id: str, project_config: Mapping[str, Any]) -> None:
    """409 for an instance write into a project that does not isolate itself, as the browser
    refuses a Partner's (validate_target_project_isolation): through a project that shares
    `default`'s volumes, images, profiles or networks an instance reaches the other party's."""
    from app.services.instance_security_policy import InstanceSecurityPolicyViolation, validate_target_project_isolation

    try:
        validate_target_project_isolation(_shared_policy(cluster_id), project_config or {})
    except InstanceSecurityPolicyViolation as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc


def _shared_policy(cluster_id: str):
    from app.core.rbac import Role
    from app.services.instance_security_policy import ClusterOwnership, ClusterWritePolicy

    return ClusterWritePolicy(cluster_id, Role.PARTNER, ClusterOwnership.SHARED)


def forget_party_bounds() -> None:
    """Drop every cached bound on this worker (tests, and forget_organization_bounds' fallback)."""
    with _lock:
        _bounds.clear()
