"""Every handler that takes a cluster from the caller must check it against the caller.

This class of bug is silent and was widespread: ten routers accepted a `host` or `cluster_id`
from the request and never compared it to the caller's organisation. A live probe with the
tenant account returned another organisation's LXD client certificate and private key.

The ratchet is structural rather than behavioural on purpose. The failure is invisible at
runtime for anyone whose own clusters happen to answer, so it will not show up in a functional
test; it only shows up when someone points the parameter at a cluster they should not reach.
"""

import ast
from pathlib import Path

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"

CLUSTER_PARAMS = {"host", "cluster_id", "node", "node_name"}
CLUSTER_SCOPE_GATES = {
    "verify_cluster_access",
    "verify_project_access",
    "verify_instance_view_access",
    "_verify_instance_access",
    "_enforce_snapshot_access",
    # core/instance_access.py: the shell and console gate, pinned below to keep the project bound
    "verify_instance_access",
    # the same, for a guest's files: core/instance_access.py, with `file_transfer` always enforced
    "verify_file_access",
    # the same, for a guest's logins: core/instance_access.py, with `console` always enforced
    "verify_guest_access",
}

# Handlers that legitimately do not call verify_cluster_access, each with the reason.
EXEMPT = {
    # Authenticated by API key, not by user, so the boundary is the key's cluster_permissions.
    ("image_uploads.py", "*"): "API-key auth: _assert_cluster_permission",
    ("iso_uploads.py", "*"): "API-key auth: _assert_cluster_permission",
    ("integrations.py", "*"): "API-key auth: cluster_permissions allow-list",
    # Registration creates the binding that scope is later derived from.
    ("cluster_registration.py", "register_cluster"): "creates the binding scope derives from",
    # Org-level membership check (user_has_access) is the correct boundary here.
    ("organizations.py", "bind_cluster"): "org membership checked via user_has_access",
    ("organizations.py", "unbind_cluster"): "org membership checked via user_has_access",
    # Break-glass is the mechanism for reaching a cluster you do not own; it has its own gate.
    ("break_glass.py", "*"): "enforce_sensitive_access / require_admin is the gate",
}


def _exempt(filename: str, func: str) -> str | None:
    return EXEMPT.get((filename, func)) or EXEMPT.get((filename, "*"))


def _handlers_taking_a_cluster():
    for path in sorted(ROUTERS.glob("*.py")):
        tree = ast.parse(path.read_text())
        src = path.read_text()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any("router." in ast.unparse(d) for d in node.decorator_list):
                continue
            args = {a.arg for a in list(node.args.args) + list(node.args.kwonlyargs)}
            if not (args & CLUSTER_PARAMS):
                continue
            yield path.name, node, src


def test_every_cluster_taking_handler_checks_the_cluster() -> None:
    offenders = []
    late_gates = []
    checked = 0
    for filename, node, src in _handlers_taking_a_cluster():
        reason = _exempt(filename, node.name)
        if reason:
            continue
        checked += 1
        # important: a CALL, not the name appearing anywhere in the body. Several handlers
        # import verify_cluster_access inside the function, so a substring match was satisfied
        # by the import line alone: deleting the call and leaving the import kept this green.
        # Proven by ablation on terminal.py before this was tightened.
        gate_calls = [
            inner
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call)
            and (
                getattr(inner.func, "id", None) in CLUSTER_SCOPE_GATES
                or getattr(inner.func, "attr", None) in CLUSTER_SCOPE_GATES
            )
        ]
        called = bool(gate_calls)
        if called:
            # A gate after the first LXD call only hides the response. It does not prevent a
            # caller from probing another organization's cluster through timing or errors.
            lxd_calls = [
                inner
                for inner in ast.walk(node)
                if isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and isinstance(inner.func.value, ast.Name)
                and inner.func.value.id == "lxd_service"
            ]
            if lxd_calls and min(call.lineno for call in gate_calls) > min(
                call.lineno for call in lxd_calls
            ):
                late_gates.append(f"{filename}:{node.lineno} {node.name}")
        if not called:
            offenders.append(f"{filename}:{node.lineno} {node.name}")

    assert checked > 0, "the scan found nothing, so it is not actually checking anything"
    assert not offenders, (
        "these handlers take a cluster from the caller and never verify it against the "
        "caller's organisation. Either call verify_cluster_access(<param>) or a shared "
        "verify_project_access(...) / verify_instance_view_access(...) gate, or add an entry to "
        "EXEMPT with the reason:\n  " + "\n  ".join(offenders)
    )
    assert not late_gates, (
        "these handlers touch LXD before checking the caller's cluster scope:\n  "
        + "\n  ".join(late_gates)
    )


def test_the_shared_project_gates_include_cluster_scope() -> None:
    """Recognizing shared helpers must not turn either into an unchecked exemption."""
    scope_path = ROUTERS.parent / "core" / "org_scope.py"
    tree = ast.parse(scope_path.read_text(encoding="utf-8"))
    helpers = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"verify_project_access", "verify_instance_view_access"}
    }
    project_helper = helpers["verify_project_access"]
    view_helper = helpers["verify_instance_view_access"]

    cluster_calls = [
        inner
        for inner in ast.walk(project_helper)
        if isinstance(inner, ast.Call)
        and getattr(inner.func, "id", None) == "verify_cluster_access"
        and inner.args
        and isinstance(inner.args[0], ast.Name)
        and inner.args[0].id == "cluster_id"
    ]
    project_calls = [
        inner
        for inner in ast.walk(view_helper)
        if isinstance(inner, ast.Call)
        and getattr(inner.func, "id", None) == "verify_project_access"
        and inner.args
        and isinstance(inner.args[0], ast.Name)
        and inner.args[0].id == "cluster_id"
    ]
    assert cluster_calls, "verify_project_access must keep the organization cluster gate"
    assert project_calls, "verify_instance_view_access must keep the shared project gate"


def test_router_local_instance_gates_include_project_scope() -> None:
    """Local operation-specific gates may add policy, but cannot replace project scope."""
    expected = {
        ("console.py", "_verify_instance_access"),
        ("instance_rebuild.py", "_verify_instance_access"),
        ("instance_snapshots.py", "_enforce_snapshot_access"),
    }
    checked = set()
    for filename, helper_name in expected:
        tree = ast.parse((ROUTERS / filename).read_text(encoding="utf-8"))
        helper = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == helper_name
        )
        project_calls = [
            inner
            for inner in ast.walk(helper)
            if isinstance(inner, ast.Call)
            and getattr(inner.func, "id", None) == "verify_project_access"
        ]
        assert project_calls, f"{filename}:{helper_name} must keep the shared project gate"
        checked.add((filename, helper_name))
    assert checked == expected


def test_the_exempt_list_stays_honest() -> None:
    """An exemption for a file that no longer exists hides a real handler behind a stale name."""
    for (filename, _func) in EXEMPT:
        assert (ROUTERS / filename).exists(), f"EXEMPT names {filename}, which does not exist"


def test_the_shared_instance_gate_starts_with_the_project_bound() -> None:
    """The instance gates count as cluster gates only because each opens with this call."""
    source = (ROUTERS.parent / "core" / "instance_access.py").read_text(encoding="utf-8")
    for name in ("verify_instance_access", "verify_file_access", "verify_guest_access"):
        helper = next(
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef) and node.name == name
        )
        first = helper.body[1] if isinstance(helper.body[0], ast.Expr) else helper.body[0]
        assert "verify_project_access(host, project)" in ast.unparse(first), name
