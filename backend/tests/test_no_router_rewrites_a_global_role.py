"""Ratchet: nothing outside the Authentik service reaches its two role-rewriting calls.

`sync_user_role` removes the user from every role group but the target one, and
`remove_user_from_all_roles` strips all three, role_admin included. The integration member sync
called the first and its orphan revoke the second, so any key could demote an Admin. An
integration now goes through services/integration_roles, which only grants.

Two rules, because either alone passes while the class returns:
  1. no module under app/ names either call, except the allowlisted dashboard handlers;
  2. each allowlisted handler still carries the guard that makes it safe to name the call.

No baseline count: every other caller was converted, so the rule is absolute.
"""

import ast
import pathlib
from typing import Dict, Iterator, List, Set, Tuple

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
SERVICE = APP / "services" / "authentik_sync.py"
REWRITES = frozenset({"sync_user_role", "remove_user_from_all_roles"})

# (file relative to app/, function, call) -> why that handler may name the call.
ALLOWED: Dict[Tuple[str, str, str], str] = {
    # Settings > User Groups. A Partner reaches it, but it refuses a target who already holds
    # role_admin or role_partner, and an unknown answer from Authentik, before the push.
    ("routers/user_groups.py", "assign_user_role", "sync_user_role"):
        "partner may only move a user holding neither role_admin nor role_partner",
    # Only an Admin's delete reaches Authentik. A Partner's delete removes memberships in the
    # organizations it owns and never touches a role group.
    ("routers/user_groups.py", "remove_user_from_all", "remove_user_from_all_roles"):
        "the Authentik call runs only when the caller's highest role is Admin",
}


class _Finder(ast.NodeVisitor):
    """Records (file, innermost function, name) for every mention of a rewriting call."""

    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.stack: List[str] = ["<module>"]
        self.found: Set[Tuple[str, str, str]] = set()

    def _enter(self, node: ast.AST) -> None:
        self.stack.append(getattr(node, "name"))
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _enter
    visit_AsyncFunctionDef = _enter

    def _hit(self, name: str) -> None:
        if name in REWRITES:
            self.found.add((self.rel, self.stack[-1], name))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._hit(node.attr)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self._hit(node.id)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self._hit(node.value)

    def visit_alias(self, node: ast.alias) -> None:
        self._hit(node.name)


def _functions(tree: ast.AST) -> Iterator[ast.AST]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _sites() -> Set[Tuple[str, str, str]]:
    found: Set[Tuple[str, str, str]] = set()
    for path in sorted(APP.rglob("*.py")):
        if path == SERVICE:
            continue
        finder = _Finder(str(path.relative_to(APP)))
        finder.visit(ast.parse(path.read_text(), filename=str(path)))
        found |= finder.found
    return found


def _function(rel: str, name: str) -> ast.AST:
    tree = ast.parse((APP / rel).read_text())
    return next(fn for fn in _functions(tree) if fn.name == name)


def test_nothing_outside_the_service_names_a_role_rewrite() -> None:
    unexpected = sorted(_sites() - set(ALLOWED))
    assert unexpected == [], (
        "a role-rewriting Authentik call is named outside the service. An integration must use "
        f"services/integration_roles, which only grants: {unexpected}"
    )


def test_every_allowlisted_site_still_exists() -> None:
    stale = sorted(set(ALLOWED) - _sites())
    assert stale == [], f"remove the stale allowlist entries: {stale}"


def test_the_integration_sync_does_not_name_either_call() -> None:
    sites = {site for site in _sites() if site[0] == "routers/integrations.py"}
    assert sites == set()


def _calls(node: ast.AST, attr: str) -> List[ast.Call]:
    return [
        sub for sub in ast.walk(node)
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == attr
    ]


def test_assign_user_role_checks_the_target_before_it_pushes() -> None:
    fn = _function("routers/user_groups.py", "assign_user_role")
    reads = _calls(fn, "get_user_role_groups")
    pushes = _calls(fn, "sync_user_role")
    assert reads and pushes
    assert min(r.lineno for r in reads) < min(p.lineno for p in pushes)
    source = ast.unparse(fn)
    assert "if highest != Role.ADMIN:" in source and "body.role != 'tenant'" in source
    assert '{"role_admin", "role_partner"}' in source or "{'role_admin', 'role_partner'}" in source
    assert "HTTP_403_FORBIDDEN" in source and "HTTP_503_SERVICE_UNAVAILABLE" in source


def test_the_global_revoke_runs_only_for_an_admin() -> None:
    fn = _function("routers/user_groups.py", "remove_user_from_all")
    guarded = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.If)
        and ast.unparse(node.test).replace(" ", "") == "highest==Role.ADMIN"
        and _calls(ast.Module(body=node.body, type_ignores=[]), "remove_user_from_all_roles")
    ]
    assert len(guarded) == 1
    assert len(_calls(fn, "remove_user_from_all_roles")) == 1
