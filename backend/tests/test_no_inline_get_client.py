"""Regression guard: `get_client` must never be awaited inline on the event loop.

`connection_manager.get_client` is not a cheap lookup. It revalidates the pooled connection
with a live request and, on failure, rebuilds the pylxd client, which itself performs a TLS
handshake and a verifying GET. Against a cluster that accepts TCP and then goes silent, that
is seconds of wall clock per call, on the loop thread, with nothing else served.

That is the exact shape of the RC3 outage. The hardening pass wrapped it in `run_blocking`
everywhere except one place: the image-import recovery sweep, whose ISO twin was converted in
the same commit. The asymmetry survived review because both routers looked hardened at a
glance, which is why this is a structural guard rather than a behavioural test.

The rule enforced here: inside an `async def`, a call to `.get_client(...)` must be an
argument to `run_blocking`, never a call in its own right.
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
            # A plain def inside an async def runs in a thread or is called deliberately.
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


# Sites that still call get_client inline, recorded as of 1.0.0-RC6 (2026-08-17).
#
# This is a ratchet, not an allowlist: the numbers may only go DOWN. A new inline call fails
# the guard, and converting one without updating the count fails it too, so the debt cannot be
# quietly re-added or silently forgotten.
#
# Counted per file rather than per line so that unrelated edits, which shift line numbers,
# do not make the baseline lie about what it is protecting.
#
# The upload routers and the proxy, network-create, metrics and rebuild paths are absent by
# design: those are the paths the RC3 outage ran through and they are converted. What remains
# is the read and CRUD surface, where a wedged cluster now costs a bounded stall per request
# (the pooled socket timeout is (3, 8)) rather than a parked worker forever.
BASELINE = {
    # note: only calls inside an `async def` count. The per-node fan-out helpers are plain
    # `def`s that already run in a thread, so they never appeared here and are not the debt
    # this tracks. What is left below is genuine: a blocking revalidation on the event loop.
    "app/routers/cluster_registration.py": 1,
    "app/routers/console.py": 3,
    "app/routers/my_keys.py": 3,
    "app/services/lxd/lxd_service.py": 1,
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
