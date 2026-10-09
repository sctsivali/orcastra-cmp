"""Every write that ends an integration key is one somebody decided about its organizations.

A key that owns an OrcaHub organization is the only key that may sync it, so ending that key
freezes the organization. The routes an operator uses refuse it (services/integration_key_ownership.py
and test_a_key_that_owns_an_organization_is_not_revoked_or_deleted). This finds every other place
by shape, so a new way to end a key cannot ship without a reviewed line here saying why it is safe
or what it does about the organizations.

The shapes: a stored key marked `["is_revoked"] = True` or `["is_active"] = False`, the same pair
written as a dict literal (the check-and-set merge on this line), and a call that removes a key's
documents.
"""

import ast
from pathlib import Path
from typing import Dict, Set

APP = Path(__file__).resolve().parents[1] / "app"
ENDING_CALLS = {"delete_api_key_from_vault"}
ENDING_FLAGS = {("is_revoked", True), ("is_active", False)}
REFUSAL = "_refuse_ending_an_owner"

# Each function that ends a key, and why its organizations are accounted for.
CHECKED: Dict[str, str] = {
    "routers/integrations.py:revoke_api_key":
        "refuses a key that owns an active OrcaHub organization, inside the key's ownership lock",
    "routers/integrations.py:delete_api_key":
        "refuses a key that owns an active OrcaHub organization, inside the key's ownership lock",
}


def _ends(node: ast.AST) -> bool:
    if isinstance(node, ast.Assign):
        return any(
            isinstance(target, ast.Subscript)
            and isinstance(target.slice, ast.Constant)
            and isinstance(node.value, ast.Constant)
            and (target.slice.value, node.value.value) in ENDING_FLAGS
            for target in node.targets
        )
    if isinstance(node, ast.Dict):
        return any(
            isinstance(k, ast.Constant) and isinstance(v, ast.Constant) and (k.value, v.value) in ENDING_FLAGS
            for k, v in zip(node.keys, node.values)
        )
    if isinstance(node, ast.Call):
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        return name in ENDING_CALLS
    # note: a blocking helper is usually handed to asyncio.to_thread or run_blocking, not called.
    return isinstance(node, ast.Name) and node.id in ENDING_CALLS


def _enders() -> Dict[str, ast.AST]:
    found: Dict[str, ast.AST] = {}
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(_ends(n) for n in ast.walk(fn)):
                found[f"{path.relative_to(APP)}:{fn.name}"] = fn
    return found


def _calls(fn: ast.AST) -> Set[str]:
    return {
        getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
    }


def test_every_function_that_ends_a_key_is_reviewed() -> None:
    assert set(_enders()) == set(CHECKED)


def test_the_checked_ones_refuse_before_they_write() -> None:
    enders = _enders()
    for where in CHECKED:
        assert REFUSAL in _calls(enders[where]), f"{where} no longer checks what the key owns"


def test_the_shapes_are_recognised() -> None:
    """The ratchet itself: each shape it claims to find, it finds."""
    for source in (
        "def f(m):\n    m['is_revoked'] = True\n",
        "def f(m):\n    m['is_active'] = False\n",
        "def f(v, k):\n    merge(v, k, {'is_revoked': True, 'is_active': False})\n",
        "def f(v, k):\n    delete_api_key_from_vault(v, k)\n",
        "async def f(v, k):\n    await asyncio.to_thread(delete_api_key_from_vault, v, k)\n",
    ):
        (fn,) = [n for n in ast.walk(ast.parse(source)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        assert any(_ends(n) for n in ast.walk(fn)), source
    (fn,) = [n for n in ast.walk(ast.parse("def f(m):\n    m['is_active'] = True\n")) if isinstance(n, ast.FunctionDef)]
    assert not any(_ends(n) for n in ast.walk(fn))
