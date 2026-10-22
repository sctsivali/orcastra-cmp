"""Structural rules for handlers that answer through core/swr.serve().

A background read outlives the request that launched it, so whatever its builder closes over
must still be valid after the response is sent, and the read-before-lock fast path only holds if
no single_flight decorator wraps the handler again.
"""

import ast
from pathlib import Path

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"


def _swr_handlers():
    for path in sorted(ROUTERS.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls_serve = any(
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "serve"
                and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id == "swr"
                for sub in ast.walk(node)
            )
            if calls_serve:
                yield path, node


def _decorator_names(node):
    for dec in node.decorator_list:
        func = dec.func if isinstance(dec, ast.Call) else dec
        yield getattr(func, "id", None) or getattr(func, "attr", None)


def test_swr_handlers_exist():
    assert list(_swr_handlers()), "no handler serves through swr; update this test"


def test_no_swr_handler_is_also_single_flighted():
    offenders = [
        f"{path.name}:{node.lineno} {node.name}"
        for path, node in _swr_handlers()
        if "single_flight" in set(_decorator_names(node))
    ]
    assert offenders == [], (
        "single_flight would hold its lock across the cache read swr.serve() does first, so "
        f"a warm reader queues behind a rebuild again: {offenders}"
    )


def test_no_swr_handler_takes_a_request_scoped_database_session():
    offenders = []
    for path, node in _swr_handlers():
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if (
                isinstance(default, ast.Call)
                and getattr(default.func, "id", None) == "Depends"
                and default.args
                and getattr(default.args[0], "id", None) == "get_db"
            ):
                offenders.append(f"{path.name}:{node.lineno} {node.name}")
    assert offenders == [], (
        "a background refresh can run after the request's session is closed; read what the "
        f"builder needs from its own session: {offenders}"
    )
