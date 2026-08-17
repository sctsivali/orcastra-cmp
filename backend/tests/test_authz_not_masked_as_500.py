"""Regression guard: an authorization refusal must not surface as a 500.

`verify_cluster_access` and `verify_tenant_permission` raise `HTTPException(403)`. Several HTTP
handlers called them inside a broad `try` whose only handler was `except Exception`, which
caught that 403 and re-raised it as `HTTPException(500, detail=str(e))`. The client got a 500
whose body read `"403: Access denied: cluster 'x' is not in your organization"`, and the
refusal was logged at ERROR.

Measured on 1.0.0-RC9 before the fix, as a tenant against a cluster outside their scope:

    GET /api/v1/networks?host=nuc2        403   (correct)
    GET /api/v1/storage/pools?host=nuc2   403   (correct)
    GET /api/v1/network-acls?host=nuc2    500   <- same denial, wrong status
    GET /api/v1/operations?host=nuc2      500
    GET /api/v1/warnings?host=nuc2        500

That is worse than cosmetic. A 500 tells a client the server broke and tells monitoring to
page someone, for a permission check that worked exactly as designed. The same defect was
fixed in instances.py and vms.py earlier in this release by moving the check ahead of the try,
so this guard exists to stop it reappearing in the handlers that were missed.

WebSocket handlers are excluded: they cannot answer with a status code and correctly close with
1008 instead, which is what `events.py` does.
"""

import ast
import unittest
from pathlib import Path
from typing import List

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"
GUARDS = {"verify_cluster_access", "verify_tenant_permission"}


def _is_websocket_handler(fn: ast.AST) -> bool:
    for dec in getattr(fn, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr == "websocket":
            return True
    return False


def _handler_functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _handled_types(handler: ast.ExceptHandler) -> List[str]:
    t = handler.type
    if t is None:
        return ["bare"]
    if isinstance(t, ast.Name):
        return [t.id]
    if isinstance(t, ast.Attribute):
        return [t.attr]
    if isinstance(t, ast.Tuple):
        out = []
        for el in t.elts:
            if isinstance(el, ast.Name):
                out.append(el.id)
            elif isinstance(el, ast.Attribute):
                out.append(el.attr)
        return out
    return []


def _offenders(tree: ast.AST) -> List[int]:
    found: List[int] = []
    for fn in _handler_functions(tree):
        if _is_websocket_handler(fn):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Try):
                continue
            calls_guard = any(
                isinstance(c, ast.Call)
                and isinstance(c.func, ast.Name)
                and c.func.id in GUARDS
                for c in ast.walk(node)
            )
            if not calls_guard:
                continue
            names = [n for h in node.handlers for n in _handled_types(h)]
            catches_everything = "Exception" in names or "bare" in names
            reraises_http = "HTTPException" in names
            if catches_everything and not reraises_http:
                found.append(node.lineno)
    return found


class AuthzNotMaskedTest(unittest.TestCase):
    def test_no_handler_can_turn_a_403_into_a_500(self) -> None:
        failures: List[str] = []
        for path in sorted(ROUTERS.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:  # pragma: no cover
                continue
            for line in _offenders(tree):
                failures.append(f"{path.name}:{line}")

        self.assertEqual(
            [],
            failures,
            "a cluster/tenant authorization check sits inside a try whose only handler is "
            "`except Exception`, so its 403 is re-raised as a 500. Add `except HTTPException: "
            "raise` above it, or move the check ahead of the try:\n  " + "\n  ".join(failures),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
