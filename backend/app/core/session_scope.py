"""Hold the organization-wide session views to what the caller may see on a shared cluster.

The Sessions page's org scope listed live and past console and terminal sessions by the
caller's cluster set alone. Each row names an instance, its project and the user's email, so on
a cluster bound to two organizations a Partner, or a tenant of that org holding one rule there,
read the other organization's users, instances and projects. A row now needs its project inside
the project bound and, for a tenant, `view` on its instance, the same test every instance read
applies.
"""

from typing import Any, Iterable, TypeVar

from sqlalchemy import and_, false, func, or_

from app.core.instance_access import DEFAULT_PROJECT, can_view_instance
from app.core.org_scope import visible_projects_for

Row = TypeVar("Row")


def audit_scope_clause(model: Any, allowed: set[str]) -> Any:
    """SQL for audit rows on `allowed` clusters whose project sits inside the bound there.

    note: the bound is applied in SQL, before the page limit, so a busy neighbour on a shared
    cluster cannot push the caller's own rows off the page. An empty set matches nothing.
    """
    project = func.coalesce(func.nullif(model.project, ""), DEFAULT_PROJECT)
    clauses = []
    for cluster_id in sorted(allowed):
        projects = visible_projects_for(cluster_id)
        if projects is None:
            clauses.append(model.host == cluster_id)
        else:
            clauses.append(and_(model.host == cluster_id, project.in_(sorted(projects))))
    return or_(*clauses) if clauses else false()


def visible_session_rows(rows: Iterable[Row]) -> list[Row]:
    """The rows whose instance this request may view: the project bound and the tenant ACL."""
    return [
        row for row in rows
        if can_view_instance(row.host, row.project, row.instance_name)  # type: ignore[attr-defined]
    ]
