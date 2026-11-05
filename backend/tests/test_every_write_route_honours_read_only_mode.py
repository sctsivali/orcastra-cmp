"""Every write route refuses in read-only mode, unless it is listed here with the reason it may not.

`READ_ONLY_MODE` is documented as "disable all create, update, delete operations"
(app/core/config.py), but the check is a per-route dependency, so it held only where someone
remembered it. Organization and member changes, user-group role assignment (which writes to the
identity provider) and the external image and ISO uploads (which import into LXD) ran in read-only
mode (security review of v1.0.0-RC4..c5199ea1, finding 5). Owner decision 2026-11-05: every
dashboard-user write is closed; OrcaHub sync, provisioning and owner-key enrolment keep running,
as do migration planning and its discard and the Vault unseal.
"""

import ast
from pathlib import Path

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"

# (file, function) -> why this write route stays open in read-only mode.
OPEN_IN_READ_ONLY: dict[tuple[str, str], str] = {
    # Reads that travel as POST.
    ("archive.py", "verify_repository"): "reads the snapshot repository",
    ("cluster_registration.py", "check_cluster_authentication"): "probes a cluster, registers nothing",
    ("guest_credentials.py", "overview"): "reads the guest's accounts",
    ("guest_credentials.py", "ssh_server"): "reads sshd state",
    ("guest_credentials.py", "list_keys"): "reads authorized keys",
    ("instance_file_transfer.py", "create_download"): "a download ticket, a read",
    ("instance_files.py", "list_directory"): "read",
    ("instance_files.py", "stat_path"): "read",
    ("instance_files.py", "summarize"): "read",
    ("instance_files.py", "read_text"): "read",
    ("instance_files.py", "preview_image"): "read",
    ("instance_specifications.py", "preflight_instance_specification"): "a plan, changes nothing",
    ("instances.py", "preflight_create_instance"): "a plan, changes nothing",
    ("integrations.py", "validate_api_key_endpoint"): "read",
    ("lxd_operations.py", "wait_operation"): "waits on an operation already running",
    ("tags.py", "classify_tags"): "read",
    ("tags.py", "validate_tags"): "read",
    ("user_preferences.py", "proxy_image"): "fetches a logo for preview",
    ("ws_tickets.py", "mint_ws_ticket"): "a socket ticket; the console is not a resource write",
    ("console_actions.py", "record_console_action"): "a keystroke inside an open console, as above",
    ("release_status.py", "check_release_status_now"): "reads the release feed",
    ("security_advisories.py", "check_kernel_cves_now"): "reads the CVE feed",
    # Operational, changes no record anyone owns.
    ("nodes.py", "invalidate_node_cache"): "cache only",
    ("nodes.py", "invalidate_all_nodes_cache"): "cache only",
    ("system.py", "clear_cache"): "cache only",
    # Owner decisions.
    ("host_health.py", "unseal_vault"): "a sealed Vault must be unsealable in any mode",
    ("migrations.py", "create_plan"): "planning is allowed read-only (owner 2026-11-04); execution is not",
    ("migrations.py", "patch_plan"): "planning, as above",
    ("migrations.py", "discard_plan"): "discarding a draft, as above",
    ("integrations.py", "sync_data"): "OrcaHub sync (owner 2026-11-05)",
    ("integrations.py", "refresh_organizations"): "OrcaHub sync, pulled",
    ("integrations.py", "enroll_owner_key"): "OrcaHub owner-key enrolment",
    ("integrations.py", "remove_own_owner_key"): "OrcaHub owner-key removal",
    ("integrations.py", "sync_policies_from_external"): "OrcaHub sync",
    ("integrations.py", "provision_tenant_access"): "OrcaHub provisioning",
    ("integrations.py", "revoke_tenant_access"): "OrcaHub revocation",
    # Emergency access: an Admin still needs it during a maintenance window, and revoking narrows.
    ("break_glass.py", "create_grant"): "emergency support access",
    ("break_glass.py", "revoke_grant"): "revoking narrows access",
}

_WRITES = {"post", "put", "patch", "delete"}


def _mentions_check(node: ast.AST) -> bool:
    return any(
        (isinstance(n, ast.Name) and n.id == "check_read_only_mode")
        or (isinstance(n, ast.Attribute) and n.attr == "check_read_only_mode")
        for n in ast.walk(node)
    )


def _write_routes():
    for path in sorted(ROUTERS.glob("*.py")):
        tree = ast.parse(path.read_text())
        router_checks = any(
            isinstance(n, ast.Call) and getattr(n.func, "id", "") == "APIRouter" and _mentions_check(n)
            for n in ast.walk(tree)
        )
        for fn in tree.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in fn.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr in _WRITES
                ):
                    checked = router_checks or _mentions_check(fn) or _mentions_check(decorator)
                    yield path.name, fn.name, checked


def test_every_write_route_checks_read_only_mode_or_says_why_not() -> None:
    routes = list(_write_routes())
    assert len(routes) > 100, "the route scan found too few write routes; it has gone stale"
    unchecked = [f"{f}::{name}" for f, name, checked in routes if not checked and (f, name) not in OPEN_IN_READ_ONLY]
    assert not unchecked, (
        "these write routes run in read-only mode: add Depends(check_read_only_mode), or list them in "
        "OPEN_IN_READ_ONLY with the reason:\n  " + "\n  ".join(unchecked)
    )


def test_the_open_list_names_only_routes_that_exist_and_skip_the_check() -> None:
    routes = {(f, name): checked for f, name, checked in _write_routes()}
    stale = [f"{f}::{name}" for (f, name) in OPEN_IN_READ_ONLY if routes.get((f, name)) is not False]
    assert not stale, "remove these from OPEN_IN_READ_ONLY (gone, or now checked):\n  " + "\n  ".join(stale)
