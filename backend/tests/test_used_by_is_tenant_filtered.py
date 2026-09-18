"""Every tenant-reachable function that hands back `used_by` must filter it first.

An LXD `used_by` list names the instances, profiles and volumes that reference a resource,
across every project on the cluster. On a router reachable by a tenant that list is another
organisation's inventory unless it is filtered, and the list endpoints already drop a resource
whose references are all invisible. A detail endpoint that skips the same two steps therefore
hands a tenant both the references they may not see and a resource that is absent from their
own list.

That is what `storage_pools.get_storage_pool` did while its five siblings filtered, so the gap
was invisible next to working code rather than obviously missing. It is also invisible at
runtime to anyone whose own references happen to come back, which is why this is structural and
not a functional test: it only shows up when someone asks for a resource they do not own.

important: this checks that the filter reaches the value that is EMITTED, not merely that a
filter is called somewhere in the function. An earlier version checked only for the presence of
the name, and a review demonstrated a mutant that kept the denial gate, reverted the emission to
the raw list, and passed. That mutant leaks the whole reference list for every resource the
tenant can see one reference of, which is the more damaging half of the original bug.
"""

import ast
from pathlib import Path

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"

# Any one of these discharges the obligation. They are different shapes of the same decision:
# the first two filter or gate a reference list, the third answers the same question per
# allocation for a payload that carries one reference rather than a list.
FILTERS = frozenset(
    {
        "filter_visible_lxd_references",
        "tenant_acl_can_view_lxd_used_by",
        "tenant_can_view_lxd_used_by",
        "check_tenant_permission",
    }
)

ELEVATED = frozenset({"require_partner", "require_admin"})

# Functions that emit an unfiltered used_by for a reason, keyed (filename, function).
#
# important: every entry here is a fan-out worker that runs inside a thread pool, where the
# tenant ACL contextvar does not propagate, so it CANNOT filter and the filtering has to happen
# in the caller after the results are gathered. Each reason names the caller and the line, so a
# reviewer can check the claim rather than trust it, and so an exemption cannot outlive the
# filtering it depends on. Do not add an entry without that line reference.
EXEMPT: dict[tuple[str, str], str] = {
    ("ipam.py", "_fetch_ipam_for_node"): "list_ipam filters the gathered rows, ipam.py:277-286",
    ("network_acls.py", "_fetch_acls_for_node"): "list_network_acls filters, network_acls.py:241",
    ("networks.py", "_fetch_networks_for_node"): "list_networks filters, networks.py:284-287",
    ("profiles.py", "_fetch_profiles_for_node"): "list_profiles filters the rows and used_by (the bound lives in filter_visible_lxd_references)",
    ("storage_pools.py", "_fetch_pools_for_node"): "list_storage_pools filters, storage_pools.py:165-171",
    ("storage_volumes.py", "_fetch_volumes_for_node"): "list_storage_volumes filters, storage_volumes.py:176-179",
}


def _is_filter_call(expr: ast.AST | None) -> bool:
    """True when the expression's value comes from one of the filter helpers."""
    if expr is None:
        return False
    if isinstance(expr, ast.Call):
        func = expr.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        return name in FILTERS
    # `filter(...) or []` and `filter(...) if x else []` still carry the filtered value.
    if isinstance(expr, ast.BoolOp):
        return any(_is_filter_call(v) for v in expr.values)
    if isinstance(expr, ast.IfExp):
        return _is_filter_call(expr.body) or _is_filter_call(expr.orelse)
    return False


def _filtered_locals(fn: ast.AST) -> set[str]:
    """Names bound to a filtered value somewhere in this function."""
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and _is_filter_call(node.value):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and _is_filter_call(node.value):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def _emissions(fn: ast.AST):
    """Every expression that becomes a `used_by` value: keyword argument or assignment."""
    for node in ast.walk(fn):
        if isinstance(node, ast.keyword) and node.arg == "used_by":
            yield node.value
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                named = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
                if named == "used_by":
                    yield node.value


def _emission_is_filtered(expr: ast.AST, filtered: set[str]) -> bool:
    if _is_filter_call(expr):
        return True
    if isinstance(expr, ast.Name):
        return expr.id in filtered
    # important: `all`, not `any`. `pool.used_by or []` evaluates to the RAW list whenever it is
    # non-empty, so the safe-looking `[]` branch says nothing about the branch that actually
    # carries data. An earlier `any` here passed every unfiltered list in the codebase.
    if isinstance(expr, ast.BoolOp):
        return all(_emission_is_filtered(v, filtered) for v in expr.values)
    if isinstance(expr, ast.IfExp):
        return _emission_is_filtered(expr.body, filtered) and _emission_is_filtered(expr.orelse, filtered)
    # A literal empty list emits nothing to leak.
    if isinstance(expr, ast.List) and not expr.elts:
        return True
    return False


def _depends_on_elevated(expr: ast.AST | None) -> bool:
    """True for `Depends(require_partner())`, false for the uncalled `Depends(require_partner)`.

    important: matching the bare name would read a broken dependency as a working gate. FastAPI
    resolves `Depends(require_partner)` as the factory itself, which enforces nothing, so the
    call has to be present for the exemption to be real.
    """
    if not isinstance(expr, ast.Call):
        return False
    outer = getattr(expr.func, "id", None) or getattr(expr.func, "attr", None)
    if outer in ELEVATED:
        return True
    if outer != "Depends":
        return False
    for arg in expr.args:
        if isinstance(arg, ast.Call):
            inner = getattr(arg.func, "id", None) or getattr(arg.func, "attr", None)
            if inner in ELEVATED:
                return True
    return False


def _module_is_tenant_reachable(tree: ast.Module) -> bool:
    """A module is tenant-reachable unless its router is mounted above the tenant role.

    note: this asks the question in the safe direction. Keying on the presence of
    `require_tenant()` instead would skip a router mounted with no role dependency at all, and
    two of those exist today.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "dependencies":
            for element in ast.walk(node.value):
                if _depends_on_elevated(element):
                    return False
    return True


def _handler_elevates_above_tenant(node: ast.AST, tree: ast.Module) -> bool:
    """True when the function re-gates itself above the router's own dependency.

    A tenant-mounted router can still carry Partner-only handlers, and all four `create_*`
    endpoints do exactly that. Their used_by is unfiltered and that is correct: no tenant
    reaches them, and a resource has no users the moment it is created. Reading the elevation
    rather than naming the four handlers keeps this true if a fifth is added, and makes the
    check fire if one of them ever loses its gate.
    """
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]
    if any(_depends_on_elevated(d) for d in defaults):
        return True
    # The same gate is also written as a decorator argument on some routers.
    for decorator in node.decorator_list:
        for element in ast.walk(decorator):
            if isinstance(element, ast.keyword) and element.arg == "dependencies":
                for inner in ast.walk(element.value):
                    if _depends_on_elevated(inner):
                        return True
    return False


def _functions_emitting_used_by():
    for path in sorted(ROUTERS.glob("*.py")):
        tree = ast.parse(path.read_text())
        if not _module_is_tenant_reachable(tree):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _handler_elevates_above_tenant(node, tree):
                continue
            emissions = list(_emissions(node))
            if emissions:
                yield path.name, node, emissions


def test_every_tenant_reachable_used_by_is_filtered() -> None:
    offenders = []
    checked = 0
    for filename, node, emissions in _functions_emitting_used_by():
        if EXEMPT.get((filename, node.name)) or EXEMPT.get((filename, "*")):
            continue
        checked += 1
        filtered = _filtered_locals(node)
        for expr in emissions:
            if not _emission_is_filtered(expr, filtered):
                offenders.append(f"{filename}::{node.name}  (line {expr.lineno}: {ast.unparse(expr)})")

    assert checked, "found no function emitting used_by; the detection has gone stale"
    assert not offenders, (
        "these functions hand a tenant a used_by value that no filter reached:\n  "
        + "\n  ".join(offenders)
        + f"\n\nThe EMITTED value must come from one of {', '.join(sorted(FILTERS))}, as "
        "profiles.get_profile and storage_pools.get_storage_pool do. Calling a filter "
        "elsewhere in the function is not enough: the gate and the emission are separate "
        "halves and only the emission is what the caller receives. If the function genuinely "
        "does not need it, add it to EXEMPT with the reason."
    )
