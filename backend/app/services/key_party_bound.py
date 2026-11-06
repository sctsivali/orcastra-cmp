"""A key a Partner created is held to its creator's projects on a cluster another party holds.

The browser bounds a Partner on such a cluster to the projects its own organizations' policies
name (org_scope.get_visible_projects). That bound lives in request contextvars no key route sets,
so a key minted before services/api_key_grant_bound.py refused `*` there kept reading `*` as every
project: another party's projects, their project deletes, pools and networks (security review of
the RC5 follow-up round, 2026-11-06). A key acts for its creator, so its grants are narrowed to the
creator's bound on every request, in a copy, the way cluster_generations.bind narrows them to live
registrations. Every key door then applies the project-limited rules it already has.

note: the creator is the add-on's installer for an add-on credential (key_default_view_only). The
projects are the browser's (org_scope.partner_projects_on); the clusters it applies to follow the
grant rule a key is minted under (api_key_grant_bound: another party holds the cluster), so a
seller's key beside its own buyers keeps its grant. Inside the bound a Partner's key may still
create and delete projects, as the browser lets a Partner do (owner decision 2026-11-06), under
the browser's project policy: `project_change_refusal`, `shared_project_config` and
`refuse_an_unisolated_project` say when it may not.
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

# A project create or delete on one worker tells every worker through the shared cache.
_CHANGED_NS = "key_party_bound_changed"

_lock = threading.Lock()
# (creator, cluster) -> (read at, wall clock then, None when no other party holds it, else the bound)
_bounds: Dict[Tuple[str, str], Tuple[float, float, Optional[FrozenSet[str]]]] = {}


def _changed_since(cluster_id: str, wall: float) -> bool:
    """Whether a worker recorded a project create or delete on `cluster_id` after `wall`."""
    try:
        from app.core.cache import get_cache_manager

        stamp = get_cache_manager().get(_CHANGED_NS, cluster_id)
    except Exception:  # noqa: BLE001 - a cache hiccup must never break authentication
        return False
    try:
        return stamp is not None and float(stamp) >= wall
    except (TypeError, ValueError):
        return True


def _publish_change(cluster_id: str) -> None:
    try:
        from app.core.cache import get_cache_manager

        get_cache_manager().set(_CHANGED_NS, cluster_id, time.time(), ttl=BOUND_SECONDS + 5)
    except Exception as exc:  # noqa: BLE001 - the local drop still happened; others expire in 30 s
        logger.warning("Could not publish a party bound change on %s: %s", cluster_id, type(exc).__name__)


async def _read_bound(db: Any, creator: str, cluster_id: str) -> Optional[FrozenSet[str]]:
    from app.core.org_scope import partner_projects_on
    from app.services.cluster_parties import clusters_held_by_another_party

    if cluster_id not in await clusters_held_by_another_party(db, creator, {cluster_id}):
        return None
    return frozenset((await partner_projects_on(db, creator, {cluster_id})).get(cluster_id, {"default"}))


async def _bound(api_key: Mapping[str, Any], cluster_id: str, db: Any) -> Tuple[str, Optional[FrozenSet[str]]]:
    if db is None:
        from app.core.database import async_session_maker

        async with async_session_maker() as session:
            return await _bound(api_key, cluster_id, session)
    creator = await _creator(api_key, db)
    cache_key = (creator.strip().lower(), cluster_id)
    now = time.monotonic()
    with _lock:
        cached = _bounds.get(cache_key)
    # note: only a held cluster's bound is checked against the shared stamp, so a key whose clusters
    # nobody else holds pays no cache read for it.
    if cached is not None and now - cached[0] < BOUND_SECONDS and not (
        cached[2] is not None and _changed_since(cluster_id, cached[1])
    ):
        return creator, cached[2]
    wall = time.time()
    bound = await _read_bound(db, creator, cluster_id)
    with _lock:
        _bounds[cache_key] = (now, wall, bound)
    return creator, bound


async def party_bound(
    api_key: Mapping[str, Any], cluster_id: str, *, db: Any = None, roles: Optional[CurrentRoleReader] = None,
    role_seconds: Optional[float] = None,
) -> Tuple[Optional[FrozenSet[str]], bool]:
    """(the projects this key is held to on `cluster_id`, or None for no bound; whether the
    creator's role was unknown). An Admin's key and a cluster no other party holds have no bound.

    note: an identity provider that does not answer leaves the bound in place for that grant only
    (fail closed), so an outage narrows an Admin's key on a shared cluster rather than refusing the
    key everywhere. An unreadable party is a 503: the database is down for every request then.
    """
    try:
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

    important: a copy, never the cached document: the bound moves with the creator's policies and
    bindings, and the stored grant must come back when it does. The stored grants stay in
    `_held_cluster_permissions`, which /validate and /whoami report.
    """
    grants = [g for g in (api_key.get("cluster_permissions") or []) if isinstance(g, dict)]
    if not grants:
        return api_key
    narrowed: List[Dict[str, Any]] = []
    bounded: List[str] = []
    for grant in grants:
        cluster_id = str(grant.get("cluster_id") or "")
        bound, _unknown = await party_bound(api_key, cluster_id, db=db, roles=roles)
        if bound is None:
            narrowed.append(grant)
            continue
        bounded.append(cluster_id)
        narrowed.append(_held_to(grant, bound))
    if not bounded:
        return api_key
    return {
        **api_key,
        "cluster_permissions": narrowed,
        HELD: api_key.get(HELD) or grants,
        PARTY_BOUNDED: bounded,
    }


def _held_to(grant: Mapping[str, Any], bound: FrozenSet[str]) -> Dict[str, Any]:
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

    grant = next(
        (g for g in api_key.get("cluster_permissions") or [] if isinstance(g, dict) and g.get("cluster_id") == cluster_id),
        None,
    )
    held = set((grant or {}).get("projects") or [])
    creator = await _creator(api_key, db)
    if not creating:
        # important: read again, not from the cache: a project deleted moments ago may be a name
        # another party has just created, and a stale bound would hand it to this key.
        fresh = await _read_bound(db, creator, cluster_id) or frozenset()
        if project_name in held and project_name in fresh and project_name != "default":
            return None
        return HTTPException(status_code=404, detail=f"Project '{project_name}' not found in cluster '{cluster_id}'")
    try:
        # Judged as the creator the bound is judged by (the installer for an add-on).
        await refuse_a_project_another_party_holds(db, {**api_key, "created_by": creator}, cluster_id, project_name)
    except HTTPException as refusal:
        return refusal
    if await org_to_hold_project_grant(creator, cluster_id, db) is None:
        return HTTPException(
            status_code=403,
            detail=(
                "None of the organizations this key's creator created or holds as Partner is bound to "
                f"cluster '{cluster_id}', so a project created there could not be recorded to them."
            ),
        )
    return None


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
    """Drop every cached bound on `cluster_id`, here and on every worker. Called wherever a project
    on the cluster is created or deleted, whoever did it: a deleted name may be another party's
    next, and any creator's cached bound could still list it."""
    with _lock:
        for cache_key in [k for k in _bounds if k[1] == cluster_id]:
            _bounds.pop(cache_key, None)
    _publish_change(cluster_id)


def forget_creator_bound_of(api_key: Mapping[str, Any], cluster_id: str) -> None:
    """After a key deleted a project on `cluster_id`."""
    forget_cluster_bounds(cluster_id)


def forget_creator_bound(creator: str, cluster_id: str) -> None:
    """Drop a creator's cached bound on one cluster after a project create, here and on every worker."""
    with _lock:
        _bounds.pop(((creator or "").strip().lower(), cluster_id), None)
    _publish_change(cluster_id)


def shared_project_config(cluster_id: str, config: Mapping[str, Any]) -> Dict[str, str]:
    """The config a Partner's project on a shared cluster is created with, as the browser forces it
    (instance_security_policy.prepare_project_create_config): restricted, isolated features, and no
    other key but user.*. A refused key is the browser's 4xx."""
    from app.services.instance_security_policy import InstanceSecurityPolicyViolation, prepare_project_create_config

    try:
        return prepare_project_create_config(_shared_policy(cluster_id), config or {})
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


# How long one installer's reach may take to judge before it is treated as not reached, how long
# an event waits for the identity provider before the installer's bound applies, and the one
# deadline every installer of an event shares (the instance create emit has 2 s in all).
REACH_SECONDS = 3
ROLE_SECONDS = 1.0
EVENT_REACH_SECONDS = 1.5
PROJECT_DELETED = "orcastra.project.deleted.v1"

# One bound per installer and cluster for a caller judging many subscriptions or rows.
ReachMemo = Dict[Tuple[str, str], Any]
_UNREADABLE = object()


def _grants_naming(grants: List[Mapping[str, Any]], cluster_id: str, project: Optional[str]) -> bool:
    wanted = (project or "").strip() or "default"
    for grant in grants:
        if grant.get("cluster_id") != cluster_id:
            continue
        projects = grant.get("projects", ["default"])
        if "*" in projects or wanted in projects:
            return True
    return False


def _grants_on(install: Any, cluster_id: str) -> List[Mapping[str, Any]]:
    return [
        g for g in (getattr(install, "granted_scope", None) or [])
        if isinstance(g, dict) and g.get("cluster_id") == cluster_id
    ]


def install_grant_names(install: Any, cluster_id: str, project: Optional[str]) -> bool:
    """Whether the installation's stored grant names `project` on `cluster_id` (no bound, no I/O)."""
    return _grants_naming(_grants_on(install, cluster_id), cluster_id, project)


async def _installer_bound(install: Any, cluster_id: str, memo: Optional[ReachMemo]) -> Optional[FrozenSet[str]]:
    """The installer's bound on `cluster_id`, None for none; raises when it cannot be read."""
    key = (str(getattr(install, "installed_by", "") or ""), cluster_id)
    if memo is not None and key in memo:
        known = memo[key]
        if known is _UNREADABLE:
            raise RuntimeError("the installer's bound could not be read")
        return known
    as_key = {"id": str(getattr(install, "api_key_id", "") or ""), "created_by": key[0]}
    try:
        bound, _unknown = await asyncio.wait_for(
            party_bound(as_key, cluster_id, db=None, role_seconds=ROLE_SECONDS), REACH_SECONDS
        )
    except Exception:
        if memo is not None:
            memo[key] = _UNREADABLE
        raise
    if memo is not None:
        memo[key] = bound
    return bound


async def read_bounds(pairs: Iterable[Tuple[Any, str]], memo: ReachMemo, seconds: float) -> None:
    """Read the bound of every distinct (installer, cluster) at once, under one deadline.

    A pair still unread at the deadline counts as unreadable, so one slow installer does not cost
    the others their events, and a page of the delivery log waits once.
    """
    tasks: Dict[Tuple[str, str], "asyncio.Task[Any]"] = {}
    for install, cluster_id in pairs:
        key = (str(getattr(install, "installed_by", "") or ""), cluster_id)
        if key in memo or key in tasks:
            continue
        tasks[key] = asyncio.ensure_future(_installer_bound(install, cluster_id, memo))
    if not tasks:
        return
    done, late = await asyncio.wait(tasks.values(), timeout=seconds)
    for task in late:
        task.cancel()
    await asyncio.gather(*late, return_exceptions=True)
    for task in done:
        task.exception()  # read, so a failure is not reported as never retrieved
    for key, task in tasks.items():
        if task in late:
            memo[key] = _UNREADABLE


async def install_reaches_project(
    install: Any, cluster_id: str, project: Optional[str], *,
    deleted_and_held: Optional[bool] = None, memo: Optional[ReachMemo] = None,
) -> bool:
    """Whether an add-on installation may hear about `project` on `cluster_id`.

    note: webhooks were routed by cluster alone, so an add-on heard every project on a cluster it
    reached, another party's included (security review, 2026-11-06). The event's project must now
    be in the installation's grant for that cluster, and where another party holds the cluster for
    the installer, that grant is held to the installer's bound exactly as its key is. The bound is
    read in its own session, once per installer for a `memo`, and waits ROLE_SECONDS at most for
    the identity provider. Raises when it cannot be read.

    `deleted_and_held`: for a project delete, whether the delete named the installation's
    organization as one whose policies held the project. The project is in no bound by then, so
    a held one is judged by the stored grant; on a cluster another party holds, one not held is not
    heard, so a party that reuses the name is not told its own project went away.
    """
    grants = _grants_on(install, cluster_id)
    if not _grants_naming(grants, cluster_id, project):
        return False
    if deleted_and_held:
        return True
    bound = await _installer_bound(install, cluster_id, memo)
    if bound is None:
        return True
    if deleted_and_held is False:
        return False
    return _grants_naming([_held_to(g, bound) for g in grants], cluster_id, project)


def payload_reach(payload: Mapping[str, Any]) -> Optional[Tuple[str, str, bool]]:
    """(cluster, project, is a project delete) an instance, volume or project payload is about;
    None for an event about the installation itself; ("", "", False) for one that names none."""
    data = (payload or {}).get("data") or {}
    resource_type = data.get("resource_type")
    if resource_type not in ("instance", "volume", "project"):
        return None
    cluster_id = data.get("cluster_id")
    project = data.get("resource_name") if resource_type == "project" else data.get("project")
    if not cluster_id or not project:
        return ("", "", False)
    return (str(cluster_id), str(project), (payload or {}).get("type") == PROJECT_DELETED)


async def payload_reaches(install: Any, payload: Mapping[str, Any], memo: Optional[ReachMemo] = None) -> bool:
    """Whether a queued webhook payload is about something this installation reaches now.

    An instance or volume event names its project; one that names none (queued before events
    carried it) is not reached. A project event is about the project it names; a queued project
    delete is judged as one the delete did not name as held (the queue does not keep that), so on a
    cluster another party holds it is not reached. An event about the installation itself always
    is. A bound that cannot be read is not reached.
    """
    about = payload_reach(payload)
    if about is None:
        return True
    cluster_id, project, deleted = about
    if not cluster_id:
        return False
    try:
        return await install_reaches_project(
            install, cluster_id, project, deleted_and_held=False if deleted else None, memo=memo
        )
    except Exception as exc:  # noqa: BLE001 - an unknown reach is not a delivery
        logger.warning("Could not tell whether an installation reaches %s on %s: %s", project, cluster_id, type(exc).__name__)
        return False


def forget_party_bounds() -> None:
    """Tests only: drop every cached bound."""
    with _lock:
        _bounds.clear()
