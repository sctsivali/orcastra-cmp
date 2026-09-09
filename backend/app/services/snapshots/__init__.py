"""Orcastra's own snapshot scheduling.

important: nothing in this package may call an org-scope helper that reads a ContextVar. Every one
of them is documented to be inert outside a request: `verify_cluster_access` is "a no-op for admin /
dev mode / background tasks" (core/org_scope.py), `apply_cluster_filter` "returns all clusters if no
scope is active", and `check_tenant_permission` "returns True on a None ACL. In a worker that is
unconditional, so it fails OPEN" (core/blocking.py). A task started from the lifespan has no caller
whose context could be copied, so calling one here does not weaken a check, it deletes it silently.

Authorization in this package is derived from the database on every pass and passed as explicit
arguments. test_a_background_run_does_not_inherit_a_request_scope.py holds that structurally.
"""
