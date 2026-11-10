"""Ratchet: `get_client` is never called inline on the event loop.

`connection_manager.get_client` revalidates the pooled connection with a live request and,
on failure, rebuilds the pylxd client with a TLS handshake and a verifying GET. Against a
cluster that accepts TCP and then goes silent, that is seconds of wall clock per call on the
loop thread, with nothing else served.

Inside an `async def`, a call to `.get_client(...)` must be an argument to `run_blocking`,
never a call in its own right.
"""

import ast
import unittest
from pathlib import Path
from typing import List

APP = Path(__file__).resolve().parents[1] / "app"


def _offending_calls(tree: ast.AST) -> List[int]:
    """Line numbers of direct `get_client(...)` calls inside async functions."""
    offenders: List[int] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.async_depth = 0
            # Calls that appear as arguments to run_blocking are the correct form. Record the
            # node ids so the generic Call visit can skip them.
            self.allowed: set = set()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.async_depth += 1
            self.generic_visit(node)
            self.async_depth -= 1

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            # A plain def inside an async def runs in a thread or is an explicit sync call.
            depth, self.async_depth = self.async_depth, 0
            self.generic_visit(node)
            self.async_depth = depth

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if isinstance(func, ast.Name) and func.id == "run_blocking":
                # run_blocking(fn, *args): `fn` is referenced, not called, so there is
                # nothing to flag. Its arguments are still visited for nested violations.
                for arg in node.args[1:]:
                    self.visit(arg)
                for kw in node.keywords:
                    self.visit(kw.value)
                return
            if (
                self.async_depth > 0
                and isinstance(func, ast.Attribute)
                and func.attr == "get_client"
            ):
                offenders.append(node.lineno)
            self.generic_visit(node)

    Visitor().visit(tree)
    return offenders


# Inline-call ceilings per file. The numbers may only go down: a new inline call fails the
# guard, and converting one without lowering the count fails it too. An explicit zero keeps
# an improved file in the ratchet and rejects reintroduction there. Counted per file so that
# edits which shift line numbers do not move the baseline.
#
# The upload routers and the proxy, network-create, metrics and rebuild paths are converted
# and absent. What remains is the read and CRUD surface, where a wedged cluster costs a
# bounded stall per request (the pooled socket timeout is (3, 8)).
BASELINE = {
    # Only calls inside an `async def` count. The per-node fan-out helpers are plain `def`s
    # that already run in a thread. Each count below is a blocking revalidation on the loop.
    "app/routers/cluster_registration.py": 0,
    "app/routers/console.py": 0,
    "app/routers/my_keys.py": 1,
    "app/services/lxd/lxd_service.py": 0,
    "app/services/tag_service.py": 6,
}


class NoInlineGetClientTest(unittest.TestCase):
    def test_no_new_inline_get_client_calls(self) -> None:
        found: dict = {}
        detail: List[str] = []
        for path in sorted(APP.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
                continue
            lines = _offending_calls(tree)
            if not lines:
                continue
            rel = str(path.relative_to(APP.parent))
            found[rel] = len(lines)
            detail.extend(f"{rel}:{line}" for line in lines)

        regressions = [
            f"{f}: {n} inline calls, baseline allows {BASELINE.get(f, 0)}"
            for f, n in sorted(found.items())
            if n > BASELINE.get(f, 0)
        ]
        self.assertEqual(
            [],
            regressions,
            "new inline get_client call(s) on an async path. Wrap in run_blocking, as "
            "iso_uploads.py and image_uploads.py do.\n  " + "\n  ".join(detail),
        )

        improved = [
            f"{f}: now {found.get(f, 0)}, baseline says {n}"
            for f, n in sorted(BASELINE.items())
            if found.get(f, 0) < n
        ]
        self.assertEqual(
            [],
            improved,
            "inline get_client calls were removed. Lower the BASELINE counts to lock the "
            "improvement in:\n  " + "\n  ".join(improved),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
