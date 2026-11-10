"""A mutation must drop the cached list it just made stale.

Every list endpoint caches its response under `<kind>:list:...` in the `api_responses`
namespace. A handler that mutates the same kind and does not clear it leaves readers on a
stale list until the TTL expires. The failure is silent: the endpoint returns 200 and the
operator does not see their own change.

The rule enforced here: if a router's list endpoint caches, every mutating handler in that
router must call an invalidation helper. network_acls.py caches nothing, so it has nothing
to invalidate.
"""

# The check is per module, not per resource kind, so a router that caches one thing and
# mutates another is a false positive. Each exemption states why; a new one needs the same
# justification.
EXEMPT = {
    # Pulls the organization roster from OrcaHub. The only thing this module caches in
    # api_responses is the API-key list (`integrations:api_keys:list*`), which a roster
    # refresh does not touch.
    "refresh_organizations",
    # POST only because it takes a key id and secret in the body. It validates and returns,
    # it does not mutate anything.
    "validate_api_key_endpoint",
    # Read-only validation uses POST because the full prospective create payload is its input.
    "preflight_create_instance",
    # Durable create invalidates only after daemon state changes or terminal readback. Invalidating
    # when the request is accepted would let an older in-flight list repopulate stale state before
    # create settles; the worker owns the generation-safe invalidation instead of this handler.
    "create_instance",
    # POST because the request names clusters and options in a body; it reads the inventory
    # an integration may see (capability inventory.read) and writes nothing.
    "sync_data",
}

import ast
import unittest
from pathlib import Path
from typing import List, Set

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"
MUTATING = {"post", "put", "patch", "delete"}
INVALIDATORS = {
    "invalidate_instance_cache",
    "invalidate_resource_cache",
    "invalidate_cluster_cache",
    # Check Authentication and a verified adopt: clusters, projects, regions, nodes, instances.
    "invalidate_cluster_reachability",
    "invalidate_node_cache",
    "invalidate_all_node_caches",
    "invalidate_availability_cache",
    "invalidate_after_external_write",
    "invalidate_capacity_cache",
    "invalidate_tag_cache",
    # A tenant's grant changed: clusters, projects, regions, warnings, monitoring, instances, nodes.
    "invalidate_after_access_change",
    "delete_pattern",
}
# `delete` counts only in the cache's own shape, delete("<namespace>", key). As a bare name it
# would also match pylxd's instance.delete() and SQLAlchemy deletes.
CACHE_NAMESPACES = {"api_responses", "vault_list", "lxd_resources", "tags"}


def _caches_a_list(tree: ast.AST) -> bool:
    """True if any handler in this module writes into the api_responses namespace.

    A handler served through core/swr.serve() writes there too, through its builder.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "serve"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "swr"
        ):
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "set"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "api_responses"
        ):
            return True
    return False


def _mutating_handlers(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        methods = {
            d.func.attr
            for d in node.decorator_list
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
        }
        if methods & MUTATING:
            yield node


def _is_cache_delete(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "delete"
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value in CACHE_NAMESPACES
    )


def _invalidates(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr in INVALIDATORS:
            return True
        # The lifecycle routes clear through core.cache's outcome helper, called by name.
        if isinstance(node.func, ast.Name) and node.func.id == "clear_instance_lists_after_failure":
            return True
        if _is_cache_delete(node):
            return True
    return False


def _module_helpers_that_invalidate(tree: ast.AST) -> Set[str]:
    """Module-level helpers that invalidate, so a handler delegating to one still counts."""
    out: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _invalidates(node):
            out.add(node.name)
    return out


class CacheInvalidationCoverageTest(unittest.TestCase):
    def test_every_mutation_clears_the_list_it_invalidates(self) -> None:
        gaps: List[str] = []
        for path in sorted(ROUTERS.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:  # pragma: no cover
                continue
            if not _caches_a_list(tree):
                continue
            helpers = _module_helpers_that_invalidate(tree)
            for fn in _mutating_handlers(tree):
                if fn.name in EXEMPT or _invalidates(fn):
                    continue
                delegates = any(
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name)
                    and n.func.id in helpers
                    for n in ast.walk(fn)
                )
                if not delegates:
                    gaps.append(f"{path.name}:{fn.lineno} {fn.name}")

        self.assertEqual(
            [],
            gaps,
            "these handlers mutate a resource whose list endpoint is cached, without "
            "clearing that cache, so the change stays invisible until the TTL:\n  "
            + "\n  ".join(gaps),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


def test_instance_lifecycle_clears_the_storage_volume_list() -> None:
    """LXD backs every instance with a root volume (type `container` or `virtual-machine`), so a
    create or a delete changes the storage volume list even though no volume endpoint was
    called. Left cached, the list would hide a new instance's root disk until its TTL.
    """
    from app.core.cache import CacheManager

    cleared: list[str] = []

    mgr = CacheManager.__new__(CacheManager)
    mgr.delete_pattern = (  # type: ignore[method-assign]
        lambda ns, pattern, **_kwargs: cleared.append(pattern)
    )

    CacheManager.invalidate_instance_cache(mgr)

    assert "storage_volumes:*" in cleared, (
        "an instance create or delete changes the volume list; leaving it cached hides the "
        f"instance's own root disk. cleared={cleared}"
    )
    for expected in ("instances:*", "vms:*", "monitoring:*"):
        assert expected in cleared, f"{expected} must still be cleared; cleared={cleared}"
