"""Every write that ends an integration key is one somebody decided about its organizations.

A key that owns an OrcaHub organization is the only key that may sync it, so ending that key
freezes the organization. The routes an operator uses refuse it (services/integration_key_ownership.py
and test_a_key_that_owns_an_organization_is_not_revoked_or_deleted). This finds every other place
by shape, so a new way to end a key cannot ship without a reviewed line here saying why it is safe
or what it does about the organizations.

The shapes: a stored key marked `["is_revoked"] = True` or `["is_active"] = False`, and a call to
one of the functions that remove or revoke a key's documents.
"""

import ast
from pathlib import Path
from typing import Dict, Set

APP = Path(__file__).resolve().parents[1] / "app"
ENDING_CALLS = {"delete_api_key_from_vault", "destroy_install_credential", "revoke_install_credential"}
ENDING_FLAGS = {("is_revoked", True), ("is_active", False)}
REFUSAL = "_refuse_ending_an_owner"

# Each function that ends a key, and why its organizations are accounted for.
CHECKED: Dict[str, str] = {
    "routers/integrations.py:revoke_api_key":
        "refuses a key that owns an active OrcaHub organization, inside the key's ownership lock",
    "routers/integrations.py:delete_api_key":
        "refuses a key that owns an active OrcaHub organization before the add-on claim check",
}
NOT_CHECKED_YET: Dict[str, str] = {
    "services/extension_credentials.py:_retire_key":
        "retires an add-on credential for its rotation and for the reconcile sweep; organizations an "
        "add-on credential owns are not moved or checked on this path",
    "services/extension_credentials.py:destroy_install_credential":
        "uninstall removes the add-on's credentials; organizations they own are not checked first",
    "services/extension_teardown.py:teardown_install":
        "the uninstall teardown, through destroy_install_credential",
    "services/extension_reconcile.py:_narrow_drifted_scope":
        "revokes an installation's credential once its organization reaches none of its clusters",
    "services/extension_reconcile.py:_revoke_orphaned_credentials":
        "revokes an add-on credential no installation claims",
}


def _enders() -> Dict[str, ast.AST]:
    found: Dict[str, ast.AST] = {}
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                ends = False
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Subscript)
                            and isinstance(target.slice, ast.Constant)
                            and isinstance(node.value, ast.Constant)
                            and (target.slice.value, node.value.value) in ENDING_FLAGS
                        ):
                            ends = True
                elif isinstance(node, ast.Call):
                    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    ends = name in ENDING_CALLS
                if ends:
                    found[f"{path.relative_to(APP)}:{fn.name}"] = fn
                    break
    return found


def _calls(fn: ast.AST) -> Set[str]:
    return {
        getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
    }


def test_every_function_that_ends_a_key_is_reviewed() -> None:
    assert set(_enders()) == set(CHECKED) | set(NOT_CHECKED_YET)
    assert not set(CHECKED) & set(NOT_CHECKED_YET)


def test_the_checked_ones_refuse_before_they_write() -> None:
    enders = _enders()
    for where in CHECKED:
        assert REFUSAL in _calls(enders[where]), f"{where} no longer checks what the key owns"


def test_withdrawing_org_sync_is_checked_too() -> None:
    """Not a key's end by shape, but the same freeze: the owner can no longer sync."""
    tree = ast.parse((APP / "routers" / "integrations.py").read_text())
    (fn,) = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "update_api_key_capabilities"]
    assert REFUSAL in _calls(fn)
    assert "has_capability" in _calls(fn)
