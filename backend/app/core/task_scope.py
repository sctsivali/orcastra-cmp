"""The one place the task journal's reads are scoped.

The journal is written as facts by two background writers that must never name a scope helper
(their ContextVars are unset, and unset means allowed). Scope is applied on read, here, as the
exact translation of the instance-list rule: the caller's cluster set, the Partner project bound
on a shared cluster, and the tenant ACL. Each rule exists twice on purpose: once as SQL, so a
page is filtered before it is loaded, and once as a Python predicate, so every row is judged
again after the load and the WebSocket feed can judge a frame before it is queued. The corpus
test holds the two forms equal.

important: this module lives outside `app/services/task_journal` because that package is
ratcheted against naming any of these helpers. Only this module and the routers may.
"""

from typing import Any, Iterable, List, Literal, Mapping, Optional

from sqlalchemy import and_, case, func, or_

from app.core.org_scope import (
    apply_cluster_filter,
    check_tenant_permission,
    get_current_allowed_clusters,
    get_tenant_acl,
    project_is_visible,
    visible_projects_for,
)
from app.core.tenant_visibility import tenant_acl_active, tenant_may_see_project

Tier = Literal["admin", "partner", "tenant"]

# Placeholder that matches no cluster, so an empty allowed set compiles to a predicate that
# selects nothing rather than to `IN ()`, which is a syntax error on Postgres.
_NO_CLUSTER = "__none__"


def caller_tier() -> Tier:
    """Which field policy the caller gets. Admin carries no cluster set; a tenant carries an ACL."""
    if get_current_allowed_clusters() is None:
        return "admin"
    if get_tenant_acl() is not None:
        return "tenant"
    return "partner"


def _owner_project(model: Any) -> Any:
    # A record with no project is a default-project record, the reading every list gives it.
    return func.coalesce(model.project, "default")


def _named_project(model: Any) -> Any:
    # A project row with no name reads as the default project, as tenant_may_see_project does.
    return func.coalesce(model.resource_name, "default")


def _bound_project(model: Any) -> Any:
    # The project a row is judged in for the Partner bound. A project-typed row (a project
    # create or delete) runs in `default` but names the project it is about; the bound must
    # read the name, or a Partner never sees its own project create and a Partner bound to
    # `default` on a shared cluster sees every other organisation's.
    return case(
        (model.resource_type == "project", _named_project(model)),
        else_=_owner_project(model),
    )


def _view_rules(acl: Mapping[str, Mapping[tuple, Iterable[str]]]) -> List[tuple]:
    """(cluster, project_rule, instance_rule) for every ACL rule that grants `view`."""
    rules: List[tuple] = []
    for cluster_id in sorted(acl):
        for (project_rule, instance_rule), permissions in sorted(acl[cluster_id].items()):
            if "view" in permissions:
                rules.append((cluster_id, project_rule, instance_rule))
    return rules


def _acl_condition(model: Any, acl: Mapping[str, Any]) -> Any:
    """`check_tenant_permission(cluster, project, name, "view")` for instance rows and
    `tenant_may_see_project(cluster, name)` for project rows, as one OR of rule predicates.

    important: every other resource type is dropped for a tenant. A network, an image or a
    warning names nothing of theirs, which is the reading `frame_is_visible` gives the event
    stream. An instance row with no name is dropped for the same reason.
    """
    branches: List[Any] = []
    for cluster_id, project_rule, instance_rule in _view_rules(acl):
        instance_terms: List[Any] = [
            model.cluster_id == cluster_id,
            model.resource_type == "instance",
            model.resource_name.isnot(None),
        ]
        if project_rule != "*":
            instance_terms.append(_owner_project(model) == project_rule)
        if instance_rule != "*":
            instance_terms.append(model.resource_name == instance_rule)
        branches.append(and_(*instance_terms))

        # A project row is visible when any view rule names its project (or every project):
        # check_tenant_permission merges rules at project level regardless of instance.
        project_terms: List[Any] = [
            model.cluster_id == cluster_id,
            model.resource_type == "project",
        ]
        if project_rule != "*":
            project_terms.append(_named_project(model) == project_rule)
        branches.append(and_(*project_terms))

    # `default` stays visible unconditionally while a tenant ACL is in force, with or without a
    # `view` rule, exactly as tenant_may_see_project reads it (the review found the SQL side
    # answering false() for a tenant whose grants carry no view while the Python twin admitted
    # the row); the cluster set predicate bounds which clusters.
    branches.append(and_(model.resource_type == "project", _named_project(model) == "default"))
    return or_(*branches)


def _scope_conditions(model: Any) -> List[Any]:
    conditions: List[Any] = []
    allowed = get_current_allowed_clusters()
    if allowed is not None:
        clusters = sorted(allowed)
        conditions.append(model.cluster_id.in_(clusters or [_NO_CLUSTER]))
        for cluster_id in clusters:
            projects = visible_projects_for(cluster_id)
            if projects is not None:
                conditions.append(
                    or_(
                        model.cluster_id != cluster_id,
                        _bound_project(model).in_(sorted(projects)),
                    )
                )
    acl = get_tenant_acl()
    if acl is not None:
        conditions.append(_acl_condition(model, acl))
    return conditions


def task_conditions(model: Any) -> List[Any]:
    """WHERE terms that hold a `task_journal` page to the caller's scope. Empty for Admin."""
    return _scope_conditions(model)


# important: a My Keys row names a private credential by id (the only thing between a partner and
# another owner's PFX download) on the key's cluster, so cluster scope would show it to every
# partner sharing that cluster. These rows reach Admin and the person who acted, like a host-less row.
# A key, the group cleanup, and a key that was asked for and not issued (its own type, never a key id).
KEY_RESOURCE_TYPES = ("ssh_key", "ssh_key_cleanup", "ssh_key_request")


def _is_own(model: Any, caller_email: str) -> Any:
    return func.lower(model.actor_email) == caller_email.lower()


def log_conditions(model: Any, *, caller_email: Optional[str]) -> List[Any]:
    """WHERE terms for `cluster_log`. A host-less row (identity, settings, an access denial) and
    a My Keys row reach Admin and their own actor; nobody else."""
    if get_current_allowed_clusters() is None:
        return []
    not_a_key_row = or_(model.resource_type.is_(None), model.resource_type.notin_(KEY_RESOURCE_TYPES))
    scoped = and_(not_a_key_row, *_scope_conditions(model))
    if not caller_email:
        return [scoped]
    own_hostless = and_(model.cluster_id.is_(None), model.actor_email == caller_email, not_a_key_row)
    own_key_row = and_(model.resource_type.in_(KEY_RESOURCE_TYPES), _is_own(model, caller_email))
    return [or_(scoped, own_hostless, own_key_row)]


def _row_in_scope(row: Mapping[str, Any]) -> bool:
    """The Python twin of `_scope_conditions` for one row."""
    cluster_id = row.get("cluster_id")
    allowed = get_current_allowed_clusters()
    if allowed is not None and cluster_id not in allowed:
        return False
    project = row.get("project") or "default"
    resource_type = row.get("resource_type")
    resource_name = row.get("resource_name")
    bound_project = (resource_name or "default") if resource_type == "project" else project
    if not project_is_visible(cluster_id, bound_project):
        return False
    if not tenant_acl_active():
        return True
    if resource_type == "instance":
        return bool(resource_name) and check_tenant_permission(
            cluster_id, project, resource_name, "view"
        )
    if resource_type == "project":
        return tenant_may_see_project(cluster_id, resource_name)
    return False


def task_is_visible(row: Mapping[str, Any]) -> bool:
    """Whether one `task_journal` row may reach the caller whose scope is in context."""
    return _row_in_scope(row)


def log_is_visible(row: Mapping[str, Any], *, caller_email: Optional[str]) -> bool:
    """Whether one `cluster_log` row may reach the caller whose scope is in context."""
    if get_current_allowed_clusters() is None:
        return True
    if row.get("resource_type") in KEY_RESOURCE_TYPES:
        actor = row.get("actor_email")
        return bool(caller_email and actor) and str(actor).lower() == caller_email.lower()
    if row.get("cluster_id") is None:
        return bool(caller_email) and row.get("actor_email") == caller_email
    return _row_in_scope(row)


def disconnected_for_caller(states: Iterable[Mapping[str, Any]]) -> List[str]:
    """Followed clusters in the caller's scope whose event stream is not connected.

    note: a cluster the follower recorded as `unregistered` is no longer followed, so it is not
    reported as disconnected; it would otherwise name a deregistered cluster to Admin until
    the row is pruned.
    """
    return apply_cluster_filter(
        [
            str(state["cluster_id"])
            for state in states
            if state.get("state") not in ("connected", "unregistered")
        ]
    )
