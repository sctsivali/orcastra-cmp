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
from app.core.org_scope import refuse_default_operation, verify_project_access, verify_tenant_permission


def verify_instance_access(host: str, project: Optional[str], instance_name: str, permission: str = "view") -> str:
    """The project the caller may reach `instance_name` in; raises 403 when it may not."""
    scoped_project = verify_project_access(host, project)
    verify_tenant_permission(host, scoped_project, instance_name, "view")
    if permission != "view":
        # important: whatever the rollout flag says. A ticket is a read POST, so this is the door
        # that keeps a Partner out of another party's guests in a shared `default`.
        refuse_default_operation(host, scoped_project)
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
    refuse_default_operation(host, scoped_project)
    verify_tenant_permission(host, scoped_project, instance_name, "view")
    verify_tenant_permission(host, scoped_project, instance_name, "file_transfer")
    return scoped_project


def verify_guest_access(host: str, project: Optional[str], instance_name: str) -> str:
    """The project the caller may manage `instance_name`'s logins in; raises 403 when it may not.

    important: `console` is checked whatever `tenant_perm_enforcement_enabled` says, for the reason
    `verify_file_access` gives. The console is already a root shell, but the credentials panel
    makes a lasting change (root's password, a sudo account) one click away, and a viewer must not
    gain it because an operator relaxed a rollout flag.
    """
    scoped_project = verify_project_access(host, project)
    refuse_default_operation(host, scoped_project)
    verify_tenant_permission(host, scoped_project, instance_name, "view")
    verify_tenant_permission(host, scoped_project, instance_name, "console")
    return scoped_project
