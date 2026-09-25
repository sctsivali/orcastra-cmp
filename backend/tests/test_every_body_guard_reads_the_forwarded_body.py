"""
Ratchet: every body guard on the raw proxy and /proxy reads the body LXD is sent, after it is
held to one spelling.

LXD reads body field names without regard to case (Go's encoding/json, with Unicode folding), so
a guard that read the caller's body before core/api_key_body refused a misspelled field judged a
different request from the one LXD served: `{"action": "stop", "Action": "restart"}` took the
migration fence's stop exemption and restarted the instance, and `Config` on a /proxy create
reached LXD as config without the instance.exec price. This holds the order structurally:

- raw_proxy replaces `request` with the canonical body once, and reads `request.body`, or hands
  `request` to a guard, only after that line;
- proxy_operation holds its params to the rule before any other read of `request.params`.
"""

import ast
from pathlib import Path
from typing import Dict, List

APP = Path(__file__).resolve().parents[1] / "app"
ROUTER = APP / "routers" / "integrations.py"
BODY_READERS_IN_THE_ROUTER = (
    "_raw_proxy_fenced_write_target",
    "_raw_proxy_fenced_project",
)


def _functions() -> Dict[str, ast.AST]:
    tree = ast.parse(ROUTER.read_text())
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _is_request_attribute(node: ast.AST, attribute: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attribute
        and isinstance(node.value, ast.Name)
        and node.value.id == "request"
        and isinstance(node.ctx, ast.Load)
    )


def _canonical_assignment_line(function: ast.AST) -> int:
    found: List[int] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "request" for target in node.targets
        ):
            found.append(node.lineno)
            assert "canonical_raw_proxy_body(method, request.body)" in ast.unparse(node.value), ast.unparse(node)
    assert len(found) == 1, f"raw_proxy replaces request {len(found)} times"
    return found[0]


def test_raw_proxy_reads_the_body_only_after_it_is_canonical() -> None:
    raw_proxy = _functions()["raw_proxy"]
    line = _canonical_assignment_line(raw_proxy)
    early = [
        node.lineno for node in ast.walk(raw_proxy)
        if _is_request_attribute(node, "body") and node.lineno < line
    ]
    assert early == [], f"raw_proxy reads request.body before it is canonical, lines {early}"


def test_raw_proxy_hands_the_request_to_a_body_guard_only_after_it_is_canonical() -> None:
    raw_proxy = _functions()["raw_proxy"]
    line = _canonical_assignment_line(raw_proxy)
    handed = [
        (node.lineno, ast.unparse(node.func))
        for node in ast.walk(raw_proxy)
        if isinstance(node, ast.Call)
        and any(isinstance(arg, ast.Name) and arg.id == "request" for arg in node.args)
    ]
    assert handed, "raw_proxy no longer hands the request to any guard"
    assert [call for call in handed if call[0] < line] == [], handed
    guards = {name for _, name in handed}
    assert set(BODY_READERS_IN_THE_ROUTER) <= guards, guards


def test_the_fence_helpers_read_the_body_of_the_request_they_are_given() -> None:
    functions = _functions()
    for name in BODY_READERS_IN_THE_ROUTER:
        reads = [node for node in ast.walk(functions[name]) if _is_request_attribute(node, "body")]
        assert reads, f"{name} no longer reads the body, so update this ratchet"


def test_proxy_operation_holds_its_params_to_the_spelling_before_reading_them() -> None:
    proxy_operation = _functions()["proxy_operation"]
    checks = [
        node for node in ast.walk(proxy_operation)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "assert_canonical_proxy_params"
    ]
    assert len(checks) == 1, "proxy_operation must hold its params to the spelling exactly once"
    line = checks[0].lineno
    early = [
        node.lineno for node in ast.walk(proxy_operation)
        if _is_request_attribute(node, "params") and node.lineno < line
    ]
    assert early == [], f"proxy_operation reads request.params before the spelling rule, lines {early}"
