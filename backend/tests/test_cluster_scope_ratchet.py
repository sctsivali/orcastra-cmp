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
    ("terminal.py", "*"): "enforce_sensitive_access is the gate",
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
    checked = 0
    for filename, node, src in _handlers_taking_a_cluster():
        reason = _exempt(filename, node.name)
        if reason:
            continue
        checked += 1
        body = ast.unparse(node)
        if "verify_cluster_access" not in body:
            offenders.append(f"{filename}:{node.lineno} {node.name}")

    assert checked > 0, "the scan found nothing, so it is not actually checking anything"
    assert not offenders, (
        "these handlers take a cluster from the caller and never verify it against the "
        "caller's organisation. Either call verify_cluster_access(<param>) or add an entry to "
        "EXEMPT with the reason:\n  " + "\n  ".join(offenders)
    )


def test_the_exempt_list_stays_honest() -> None:
    """An exemption for a file that no longer exists hides a real handler behind a stale name."""
    for (filename, _func) in EXEMPT:
        assert (ROUTERS / filename).exists(), f"EXEMPT names {filename}, which does not exist"
