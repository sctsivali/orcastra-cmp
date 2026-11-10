"""Orcastra's own snapshot scheduling.

important: nothing in this package may call an org-scope helper that reads a ContextVar. Those
helpers allow everything outside a request (`verify_cluster_access` and `apply_cluster_filter` in
core/org_scope.py, `check_tenant_permission` on a None ACL), and a task started from the lifespan
has no caller context to copy, so calling one here silently removes the check.

Authorization in this package is derived from the database on every pass and passed as explicit
arguments; test_a_background_run_does_not_inherit_a_request_scope.py enforces that.
"""
