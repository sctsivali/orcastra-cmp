"""SQL scope for due schedules, matching the queue's per-row snapshot permission check."""

from typing import Any, List

from sqlalchemy import and_, false, func, or_

from app.core.org_scope import (
    get_current_allowed_clusters,
    get_tenant_acl,
    visible_projects_for,
)


def schedule_conditions(model: Any) -> List[Any]:
    """Filter schedules before counting or limiting, including snapshot-only Tenant grants."""
    conditions: List[Any] = []
    project = func.coalesce(model.project, "default")
    allowed = get_current_allowed_clusters()
    if allowed is not None:
        clusters = sorted(allowed)
        conditions.append(model.cluster_id.in_(clusters or ["__none__"]))
        for cluster_id in clusters:
            projects = visible_projects_for(cluster_id)
            if projects is not None:
                conditions.append(
                    or_(model.cluster_id != cluster_id, project.in_(sorted(projects)))
                )

    acl = get_tenant_acl()
    if acl is not None:
        branches = []
        for cluster_id, rules in acl.items():
            for (project_rule, instance_rule), permissions in rules.items():
                if "snapshot" not in permissions:
                    continue
                terms = [model.cluster_id == cluster_id]
                if project_rule != "*":
                    terms.append(project == project_rule)
                if instance_rule != "*":
                    terms.append(model.instance_name == instance_rule)
                branches.append(and_(*terms))
        conditions.append(or_(*branches) if branches else false())
    return conditions
