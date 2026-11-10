"""Structural guard: a new offload must carry the caller's context.

`core/blocking.run_blocking` and `asyncio.to_thread` copy the context; raw
`loop.run_in_executor` does not, and then `get_client`'s org-scope filter cannot deny,
`get_scope_cache_key` returns the ADMIN namespace and `check_tenant_permission` fails open, all
silently. BASELINE only goes down: a new offload uses one of those two, or wraps its callable as
services/settle_invalidation.py does: `partial(contextvars.copy_context().run, fn, arg)`.

important: copy INSIDE the loop that builds the tasks. `Context.run` refuses re-entry, so one
copy shared by concurrent threads raises "cannot enter context"
(test_offloaded_work_keeps_the_callers_scope.py).
"""

import ast
import unittest
from pathlib import Path
from typing import Dict, List, Tuple

APP = Path(__file__).resolve().parents[1] / "app"

# Raw `run_in_executor` calls whose callable is not context-wrapped, per file. A number is
# lowered when a site is converted and never raised.
BASELINE: Dict[str, int] = {
    "routers/cluster_registration.py": 2,
    "routers/image_uploads.py": 3,
    "routers/iso_uploads.py": 3,
    "routers/lxd_operations.py": 1,
    "routers/lxd_server.py": 1,
    "routers/lxd_warnings.py": 1,
    "routers/network_acls.py": 1,
    "routers/networks.py": 1,
    "routers/permissions.py": 2,
    "routers/profiles.py": 1,
    "routers/storage_pools.py": 1,
    "services/lxd/lxd_service.py": 1,
}


def _is_context_wrapped(node: ast.expr) -> bool:
    """True for `partial(ctx.run, fn, ...)`, `partial(copy_context().run, fn, ...)`, `ctx.run`."""

    def _is_ctx_run(expr: ast.expr) -> bool:
        if not isinstance(expr, ast.Attribute) or expr.attr != "run":
            return False
        owner = expr.value
        if isinstance(owner, ast.Name) and "ctx" in owner.id.lower():
            return True
        if isinstance(owner, ast.Call):
            func = owner.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            return name == "copy_context"
        return False

    if _is_ctx_run(node):
        return True
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "partial" and node.args:
            return _is_ctx_run(node.args[0])
    return False


def _uncopied_offloads(tree: ast.AST) -> List[int]:
    lines: List[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "run_in_executor":
            continue
        # run_in_executor(executor, fn, *args)
        if len(node.args) >= 2 and _is_context_wrapped(node.args[1]):
            continue
        lines.append(node.lineno)
    return lines


def _scan() -> Tuple[Dict[str, int], Dict[str, List[int]]]:
    counts: Dict[str, int] = {}
    where: Dict[str, List[int]] = {}
    for path in sorted(APP.rglob("*.py")):
        rel = str(path.relative_to(APP))
        if rel == "core/blocking.py":
            continue  # the helper that does the copying
        found = _uncopied_offloads(ast.parse(path.read_text()))
        if found:
            counts[rel] = len(found)
            where[rel] = found
    return counts, where


class OffloadsCarryContext(unittest.TestCase):
    def test_no_file_grows_a_new_uncopied_offload(self):
        counts, where = _scan()
        for rel, count in sorted(counts.items()):
            allowed = BASELINE.get(rel, 0)
            self.assertLessEqual(
                count,
                allowed,
                f"{rel} has {count} offload(s) that drop the caller's context at line(s) "
                f"{where[rel]}, baseline {allowed}. Use run_blocking, asyncio.to_thread, or "
                f"partial(contextvars.copy_context().run, fn, ...).",
            )

    def test_the_baseline_does_not_outlive_the_fixes(self):
        counts, _ = _scan()
        stale = sorted(
            rel for rel, allowed in BASELINE.items() if counts.get(rel, 0) < allowed
        )
        self.assertEqual(
            [],
            stale,
            f"BASELINE is above the real count for {stale}. Lower it so the ratchet keeps its grip.",
        )

    def test_the_region_fan_out_is_already_converted(self):
        """This site reaches get_client from a thread, so it must stay converted."""
        counts, _ = _scan()
        self.assertEqual(0, counts.get("routers/regions.py", 0))


if __name__ == "__main__":
    unittest.main()
