"""What each MCP tool costs before it may run.

One declarative table, read by three things that would otherwise drift: the auth middleware that
enforces it, the `tools/list` filter that hides what a key cannot use, and the ratchet tests that
assert the surface matches the proxy vocabulary.

important: the per-cluster level is DERIVED from ``PROXY_OPERATION_LEVELS`` rather than restated
here. Restating it would create a second price list, and this repo has already paid for that once:
``proxy_operations`` says in its own docstring that the router's permission table and the
dispatcher fell out of step, so a caller holding exactly the right grant passed authorization and
then got "Unknown operation". Deriving means a tool cannot claim a price the operation does not
charge.
"""

from dataclasses import dataclass
from typing import Dict, Optional

from app.services.lxd.proxy_operations import (
    CLUSTER_WIDE_OPERATIONS,
    GUEST_ACCESS_OPERATIONS,
    PROXY_OPERATION_LEVELS,
)

__all__ = ["TOOL_CONTRACT", "ToolRequirement", "requirement_for"]


@dataclass(frozen=True)
class ToolRequirement:
    """The gate a tool passes through, and what a host should be told about it."""

    # The endpoint-family capability, checked with the INHERITING gate. A legacy key satisfies it,
    # which is deliberate: these are powers that existed when those keys were issued.
    capability: str

    # The proxy operation this tool dispatches to, or None for the inventory tier, which reads
    # across the key's whole grant rather than acting on one cluster.
    operation: Optional[str] = None

    # Marked for the host's confirmation UI. Read-only tools are safe to auto-approve; the rest
    # are not, and a host can only tell them apart if the server says so.
    read_only: bool = False

    @property
    def level(self) -> Optional[str]:
        """The per-cluster permission level, straight from the operation's own price."""
        if self.operation is None:
            return None
        return PROXY_OPERATION_LEVELS[self.operation]

    @property
    def guest_capability(self) -> Optional[str]:
        """The capability that must be granted EXPLICITLY, for a tool that reaches inside a guest.

        Never inherited by a legacy key and never seeded by an application type. Derived from the
        operation so a new guest-reaching operation cannot be exposed as a tool without it.
        """
        if self.operation is None:
            return None
        return GUEST_ACCESS_OPERATIONS.get(self.operation)

    @property
    def destructive(self) -> bool:
        return self.level == "admin"

    @property
    def cluster_wide(self) -> bool:
        """Targets a resource no project contains, so a project allow-list cannot bound it."""
        return self.operation in CLUSTER_WIDE_OPERATIONS


def _proxy(operation: str, *, read_only: bool = False) -> ToolRequirement:
    return ToolRequirement(capability="lxd.proxy", operation=operation, read_only=read_only)


def _inventory() -> ToolRequirement:
    return ToolRequirement(capability="inventory.read", read_only=True)


# The complete surface. A tool registered on the server without a row here is refused by the
# middleware and flagged by `test_every_mcp_tool_declares_a_capability`, which is the MCP analogue
# of the ratchet that keeps a REST endpoint from skipping `require_capability`.
TOOL_CONTRACT: Dict[str, ToolRequirement] = {
    # ── inventory: reads across the whole grant, no single target cluster ──────────────────────
    "orcastra_whoami": ToolRequirement(capability="inventory.read", read_only=True),
    "orcastra_list_clusters": _inventory(),
    "orcastra_list_projects": _inventory(),
    "orcastra_list_instances": _inventory(),
    "orcastra_get_instance": _inventory(),
    "orcastra_get_operation": _inventory(),

    # ── proxy reads: one named cluster, `read` on it ───────────────────────────────────────────
    "lxd_list_instances": _proxy("list_instances", read_only=True),
    "lxd_get_instance": _proxy("get_instance", read_only=True),
    "lxd_list_projects": _proxy("list_projects", read_only=True),
    "lxd_list_images": _proxy("list_images", read_only=True),
    "lxd_list_snapshots": _proxy("list_snapshots", read_only=True),
    # note: networks, storage pools and profiles share one code path and one price, so they are
    # one tool with a `kind`. Collapsing erases no permission distinction. start/stop/restart also
    # share a branch and are deliberately NOT collapsed: three verbs, three blast radii, and an
    # operator will want to allow one without the others.
    "lxd_list_resources": _proxy("list_networks", read_only=True),

    # ── proxy writes: `write` on the named cluster ─────────────────────────────────────────────
    "lxd_create_instance": _proxy("create_instance"),
    "lxd_update_instance": _proxy("update_instance"),
    "lxd_start_instance": _proxy("start_instance"),
    "lxd_stop_instance": _proxy("stop_instance"),
    "lxd_restart_instance": _proxy("restart_instance"),
    "lxd_create_snapshot": _proxy("create_snapshot"),
    "lxd_restore_snapshot": _proxy("restore_snapshot"),

    # ── reaching inside a guest: an explicit capability on top of everything above ─────────────
    "lxd_exec_instance": _proxy("exec_instance"),
    "lxd_read_file": _proxy("get_file", read_only=True),
    "lxd_write_file": _proxy("put_file"),

    # ── the guest's graphical session ──────────────────────────────────────────────────────────
    # Split along the capability boundaries rather than by convenience: a host allow-rule and an
    # operator's grant both have to be able to tell "look at this screen" from "type into it".
    "desktop_probe": _proxy("desktop_probe", read_only=True),
    "desktop_screenshot": _proxy("desktop_view", read_only=True),
    "desktop_zoom": _proxy("desktop_view", read_only=True),
    "desktop_cursor_position": _proxy("desktop_view", read_only=True),
    "desktop_control": _proxy("desktop_input"),
    "desktop_read_clipboard": _proxy("desktop_clipboard", read_only=True),

    # ── admin: destruction, and infrastructure no project contains ─────────────────────────────
    "lxd_delete_instance": _proxy("delete_instance"),
    "lxd_delete_snapshot": _proxy("delete_snapshot"),
    "lxd_create_network": _proxy("create_network"),
    "lxd_delete_network": _proxy("delete_network"),
    "lxd_create_storage_pool": _proxy("create_storage_pool"),
    "lxd_delete_storage_pool": _proxy("delete_storage_pool"),
    "lxd_create_project": _proxy("create_project"),
    "lxd_delete_project": _proxy("delete_project"),
}


def requirement_for(tool_name: str) -> Optional[ToolRequirement]:
    return TOOL_CONTRACT.get(tool_name)
