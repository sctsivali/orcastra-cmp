"""Every API-key-authenticated handler must declare which capability it needs.

`external_router` carries no router-level dependency, so authorization is per handler. That is
the property `documentation/rbac/external-api-key-scope.md` already names as the standing risk:

    Enforcement is still per handler. A new endpoint on `external_router` that forgets its check
    will have the same class of gap and nothing will catch it at review time.

Capabilities do not fix that on their own. A new endpoint that depends on `validate_api_key`
directly is reachable by every key that authenticates, exactly as before. This ratchet is what
makes forgetting impossible: it is structural, like `test_cluster_scope_ratchet.py`, because the
failure is invisible at runtime to anyone whose own key happens to carry the capability.
"""

import ast
from pathlib import Path

from app.core.api_key_capabilities import CAPABILITIES

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"

# Files whose handlers authenticate with an API key rather than a user session.
API_KEY_ROUTERS = (
    "integrations.py",
    "image_uploads.py",
    "iso_uploads.py",
    "extensions_external.py",
)

# Handlers that legitimately authenticate without requiring a capability, each with the reason.
EXEMPT = {
    (
        "extensions_external.py",
        "describe_self",
    ): "reports what the presenting installation holds and nothing else. It grants no access, reads no cluster, and gating it would make the only reconcile backstop unavailable to an installation whose single capability is storage.upload",

    (
        "integrations.py",
        "validate_api_key_endpoint",
    ): "the credential oracle a caller uses before it has done anything. It grants no access and "
       "reports what the presented key carries plus this deployment's build identity, which is "
       "self-reported, gated on the secret verifying, and null on every refusal",
    (
        "integrations.py",
        "whoami",
    ): "the same answer as /validate, through the usual headers rather than a body. It grants no "
       "access and reports what the presented key holds plus this deployment's build identity, so "
       "requiring a capability would make the one call that diagnoses a key fail for exactly the "
       "keys that need diagnosing",
    (
        "image_uploads.py",
        "options_upload",
    ): "tus CORS/capability discovery; takes no api_key and reads no session",
    (
        "iso_uploads.py",
        "options_upload",
    ): "tus CORS/capability discovery; takes no api_key and reads no session",
}


def _api_key_handlers():
    """Every routed handler in an API-key router, with the source of its own definition."""
    for filename in API_KEY_ROUTERS:
        path = ROUTERS / filename
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = [ast.unparse(d) for d in node.decorator_list]
            if not any("router." in d for d in decorators):
                continue
            # The JWT-guarded router in integrations.py is covered by require_tenant() plus
            # org_scope_dependency at the router level, like every other user-facing router.
            if filename == "integrations.py" and not any(
                d.startswith("external_router.") for d in decorators
            ):
                continue
            yield filename, node


def test_every_api_key_handler_requires_a_capability() -> None:
    offenders = []
    checked = 0
    for filename, node in _api_key_handlers():
        if (filename, node.name) in EXEMPT:
            continue
        source = ast.unparse(node)
        if "require_capability(" not in source:
            offenders.append(f"{filename}:{node.name}")
            continue
        if "validate_api_key" in source:
            offenders.append(
                f"{filename}:{node.name} still depends on validate_api_key directly"
            )
            continue
        checked += 1

    assert checked, "the walker found no handlers, so this ratchet is asserting nothing"
    assert not offenders, (
        "these API-key handlers do not require a capability, so any authenticated key reaches "
        "them. Add Depends(require_capability(...)) or list them in EXEMPT with the reason:\n  "
        + "\n  ".join(offenders)
    )


def test_every_declared_capability_is_a_real_one() -> None:
    """A typo in a handler's capability name would refuse every key, since nothing grants it."""
    offenders = []
    for filename, node in _api_key_handlers():
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            if getattr(call.func, "id", None) != "require_capability":
                continue
            if not call.args or not isinstance(call.args[0], ast.Constant):
                offenders.append(f"{filename}:{node.name} passes a non-literal capability")
                continue
            name = call.args[0].value
            if name not in CAPABILITIES:
                offenders.append(f"{filename}:{node.name} requires unknown '{name}'")
    assert not offenders, "\n  ".join(offenders)


def test_the_exempt_list_stays_honest() -> None:
    """A renamed or deleted handler must not leave a silent hole behind in EXEMPT."""
    known = {(filename, node.name) for filename, node in _api_key_handlers()}
    for entry in EXEMPT:
        assert entry in known, f"EXEMPT names {entry}, which is not a routed API-key handler"


# ── the MCP surface, which this file could not see ─────────────────────────────────────────────

def test_every_mcp_tool_funnels_through_a_gate() -> None:
    """This ratchet walks `app/routers/` and requires a `router.` decorator, so `app/mcp/` was
    invisible to it and so was `test_cluster_scope_ratchet.py`.

    That matters because every MCP tool takes a caller-supplied `cluster_id`, which is exactly
    what the scope ratchet exists to police. Today they are protected transitively: `call_proxy`
    and `run_inventory` funnel into `proxy_operation`, which does its own per-cluster check. But
    that is a property of the current implementation rather than of any test, and a tool that
    reached a service directly would inherit no coverage from either file.

    So: a tool handler either goes through one of the two dispatchers, or it is listed here with
    a reason.
    """
    import ast
    from pathlib import Path

    mcp_dir = Path(__file__).resolve().parents[1] / "app" / "mcp"
    dispatchers = {"call_proxy", "run_inventory"}
    # Tools that legitimately answer without touching a cluster.
    exempt = {
        # Reports what the key itself holds. Reads no cluster, so there is nothing to scope.
        "orcastra_whoami": "reads the credential, not a cluster",
    }

    ungated = []
    for file in sorted(mcp_dir.glob("tools_*.py")):
        tree = ast.parse(file.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorated = any(
                "mcp.tool" in ast.unparse(decorator) for decorator in node.decorator_list
            )
            if not decorated or node.name in exempt:
                continue
            called = {
                getattr(call.func, "id", None) or getattr(call.func, "attr", None)
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
            }
            if not (called & dispatchers):
                ungated.append(f"{file.name}::{node.name}")

    assert not ungated, (
        "these MCP tools do not funnel through call_proxy or run_inventory, so they are covered "
        "by neither this ratchet nor the cluster-scope one:\n  " + "\n  ".join(ungated)
    )


def test_the_mcp_dispatchers_still_enforce() -> None:
    """The funnel above is only worth anything while the thing it funnels into checks."""
    import ast
    import inspect

    from app.mcp import dispatch

    source = ast.unparse(ast.parse(inspect.getsource(dispatch.call_proxy).lstrip()))
    assert "assert_capability" in source, "call_proxy no longer checks the endpoint capability"
    assert "check_read_only_mode" in source, (
        "call_proxy no longer honours read-only mode, which the Depends default would have "
        "supplied had the handler been called through FastAPI"
    )
