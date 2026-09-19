"""The task journal: a Postgres record of every task on every cluster and of dashboard activity.

Scope-free by construction. Nothing in this package names a request-scoped helper
(`check_tenant_permission`, `get_current_allowed_clusters`, ...): the writers run as background
tasks with no request context, and the readers take their scope from `app.core.task_scope`, which
is the one module allowed to translate a caller's rights into SQL and per-row checks.
"""
