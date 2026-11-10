"""The grant a Partner's project create writes can only name that project.

`test_a_partner_cannot_widen_their_own_project_bound` covers the two rule writers a
Partner reaches, which refuse a project the caller cannot see. The grant behind a project
create cannot have that guard (the caller cannot see the project yet), so its invariant is
checked here: every writer of a policy rule is in a known set, the grant's rule takes its
project and cluster from its own parameters, and the create calls the grant only after LXD
has created that project. Also here: every project route applies the bound.
"""

import ast
import pathlib

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "app"

KNOWN_RULE_WRITERS = {
    ("routers/tenant_access.py", "create_policy"),
    ("routers/tenant_access.py", "update_policy"),
    ("routers/integrations.py", "provision_tenant_access"),
    ("routers/integrations.py", "sync_policies_from_external"),
    ("services/project_grants.py", "grant_project_to_creator"),
}


def _functions(path: pathlib.Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def test_every_rule_writer_is_a_known_one():
    writers = set()
    for path in BACKEND.rglob("*.py"):
        for fn in _functions(path):
            # Constructor calls only; the model's own __repr__ string also names the class.
            if any(
                isinstance(n, ast.Call) and ast.unparse(n.func) == "TenantPolicyRule"
                for n in ast.walk(fn)
            ):
                writers.add((path.relative_to(BACKEND).as_posix(), fn.name))
    assert writers == KNOWN_RULE_WRITERS, (
        f"policy rules are written by {sorted(writers - KNOWN_RULE_WRITERS)} beyond the known "
        "set; a new writer needs its own guard and a line here"
    )


def _keyword_names(call: ast.Call, *names):
    out = {}
    for kw in call.keywords:
        if kw.arg in names:
            out[kw.arg] = kw.value
    return out


def test_the_grant_writes_the_rule_from_its_own_parameters():
    grant = next(
        fn for fn in _functions(BACKEND / "services" / "project_grants.py") if fn.name == "grant_project_to_creator"
    )
    params = {a.arg for a in grant.args.kwonlyargs} | {a.arg for a in grant.args.args}
    calls = [
        n for n in ast.walk(grant)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "TenantPolicyRule"
    ]
    assert len(calls) == 1, "one rule per grant"
    kws = _keyword_names(calls[0], "project_name", "cluster_id", "instance_name")
    for key in ("project_name", "cluster_id"):
        value = kws[key]
        assert isinstance(value, ast.Name) and value.id == key and key in params, (
            f"the rule's {key} must be the grant's own parameter, not a literal or an attribute"
        )
    assert isinstance(kws["instance_name"], ast.Constant) and kws["instance_name"].value == "*"


def test_the_create_grants_only_after_lxd_created_the_same_project():
    create = next(fn for fn in _functions(BACKEND / "routers" / "projects.py") if fn.name == "create_project")
    src = create
    lxd_create = next(
        n for n in ast.walk(src)
        if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("lxd_service.create_project")
    )
    grant = next(
        n for n in ast.walk(src)
        if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("record_created_project")
    )
    assert grant.lineno > lxd_create.lineno, "the grant must follow the LXD create"
    created = _keyword_names(lxd_create, "name", "host")
    granted = _keyword_names(grant, "project_name", "cluster_id")
    assert ast.unparse(granted["project_name"]) == ast.unparse(created["name"]) == "request.name"
    assert ast.unparse(granted["cluster_id"]) == ast.unparse(created["host"]) == "request.host"


def test_every_project_route_applies_the_bound():
    handlers = {
        "list_all_projects": ("project_is_visible",),
        "list_projects": ("project_is_visible",),
        "get_project_usage": ("project_is_visible",),
        "update_project": ("_refuse_a_project_outside_the_bound", "project_is_visible"),
        "delete_project": ("_refuse_a_project_outside_the_bound", "project_is_visible"),
    }
    functions = {fn.name: ast.unparse(fn) for fn in _functions(BACKEND / "routers" / "projects.py")}
    missing = [
        name for name, markers in handlers.items()
        if name not in functions or not any(m in functions[name] for m in markers)
    ]
    assert missing == [], f"these project routes do not apply the Partner bound: {missing}"
    guard = functions["_refuse_a_project_outside_the_bound"]
    assert "project_is_visible" in guard and "404" in guard
