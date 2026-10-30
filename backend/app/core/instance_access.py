"""May the current caller open a session on ONE instance: the gate a shell or a console needs.

The organization's project bound first, then the tenant's view grant on that exact instance, and,
behind the tenant-permission rollout flag, the operation's own grant. Viewing is the floor under
every session: disabling the flag must never make an unviewable instance reachable.

note: routers/console.py keeps its own _verify_instance_access with these semantics, because
its tests drive it through that module's names; test_the_session_gates_agree.py holds the two to
the same answers. Raises HTTPException 403 like the org-scope primitives it calls.
"""

from typing import Optional

from app.core.config import get_settings
from app.core.org_scope import verify_project_access, verify_tenant_permission


def verify_instance_access(host: str, project: Optional[str], instance_name: str, permission: str = "view") -> str:
    """The project the caller may reach `instance_name` in; raises 403 when it may not."""
    scoped_project = verify_project_access(host, project)
    verify_tenant_permission(host, scoped_project, instance_name, "view")
    if permission != "view" and get_settings().tenant_perm_enforcement_enabled:
        verify_tenant_permission(host, scoped_project, instance_name, permission)
    return scoped_project


def verify_file_access(host: str, project: Optional[str], instance_name: str) -> str:
    """The project the caller may reach `instance_name`'s files in; raises 403 when it may not.

    important: `file_transfer` is checked whatever `tenant_perm_enforcement_enabled` says. That flag
    relaxes a rollout of console grants, and reading it here would turn every viewer into root in
    the guest's filesystem the moment an operator switched it off.
    """
    scoped_project = verify_project_access(host, project)
    verify_tenant_permission(host, scoped_project, instance_name, "view")
    verify_tenant_permission(host, scoped_project, instance_name, "file_transfer")
    return scoped_project
