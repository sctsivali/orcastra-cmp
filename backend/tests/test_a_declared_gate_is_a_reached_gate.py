"""A control that exists, is tested, and is never called is not a control.

This exists because four of them shipped that way. `desktop_policy.get_consent`,
`break_glass_service.get_active_key_grant`, the two desktop rate buckets and the dedicated desktop
thread pool were all written, unit-tested against stubs, and named in comments and documentation as
gates standing between an agent and a customer's screen. None had a call site. The suite was green
throughout, because every test called the function directly and none asked whether a request ever
reached it.

The pool one had teeth: `services/desktop/runtime.py` argues that desktop must never borrow
`exec_executor` because a screenshot loop would starve every other integration's exec, and then
built a pool that nothing used, so that starvation was reachable.

So this walks the packages where those controls live and requires each public callable to be
referenced from somewhere other than its own module and the tests. It is deliberately structural
and deliberately noisy: a new helper has to be wired or listed, and the list is where a deliberate
exception has to justify itself in writing.
"""

import ast
from pathlib import Path
from typing import Dict, Set

APP = Path(__file__).resolve().parents[1] / "app"

# The packages whose contents are gates, transports or budgets rather than incidental helpers.
WATCHED = (
    APP / "services" / "desktop",
    APP / "services" / "desktop_policy.py",
    APP / "mcp",
)

# Deliberate exceptions, each with the reason it is not a defect.
#
# important: an entry here is a claim that the symbol is reached by something this walker cannot
# see, or that it is intentionally an interface rather than a call. "It will be used later" is not
# one of the reasons, because that is exactly what the four dead controls looked like.
EXEMPT: Dict[str, str] = {
    # Protocols are annotations, not calls. `DesktopProvider` is the seam a second provider
    # arrives through and is used in `_provider`'s return type.
    "DesktopProvider": "a typing Protocol, referenced as an annotation",
    # Dataclasses and enums are constructed by name in their own module and consumed as values.
    "Action": "a dataclass, constructed in actions.py and passed onward",
    "ActionResult": "a dataclass returned by every provider",
    "ProbeReport": "a dataclass returned by the probe",
    "Display": "a dataclass carried on every result",
    "Target": "a dataclass naming the guest",
    "DesktopConsent": "a dataclass returned by get_consent",
    "ImageGeometry": "a tuple subclass returned by read_geometry",
    "ToolRequirement": "a dataclass, one instance per row of TOOL_CONTRACT",
    # Registered with the MCP server by decorator inside `register`, so the walker sees the
    # decorator rather than a call.
    "register": "each tool module's registrar, called from server.build_mcp_server",
    "build_mcp_server": "called by build_mcp_route",
    "build_mcp_route": "called from main.create_app",
}


def _public_callables(path: Path) -> Dict[str, Set[str]]:
    """Public function names defined in each module under `path`."""
    files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
    defined: Dict[str, Set[str]] = {}
    for file in files:
        if file.name == "__init__.py":
            continue
        tree = ast.parse(file.read_text())
        names = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and not node.name.startswith("_")
        }
        if names:
            defined[str(file)] = names
    return defined


def _referenced_outside(name: str, owner: str) -> bool:
    """Whether `name` is CALLED or read as an attribute anywhere else under app/.

    note: a re-export in a package `__init__` does not count. Being exported and never called is
    precisely the shape of the four controls this file exists for.
    """
    for file in sorted(APP.rglob("*.py")):
        if str(file) == owner or file.name == "__init__.py":
            continue
        try:
            tree = ast.parse(file.read_text())
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if getattr(func, "id", None) == name or getattr(func, "attr", None) == name:
                    return True
            # A decorator or a bare reference passed as a value counts too.
            if isinstance(node, ast.Attribute) and node.attr == name:
                return True
    return False


def test_no_gate_is_declared_without_being_reached() -> None:
    unreached = []
    for path in WATCHED:
        for owner, names in _public_callables(path).items():
            for name in sorted(names):
                if name in EXEMPT:
                    continue
                if not _referenced_outside(name, owner):
                    unreached.append(f"{Path(owner).relative_to(APP)}::{name}")
    assert not unreached, (
        "these are defined and never reached from anywhere else in the application, which is how "
        "consent, the key grant, the rate buckets and the desktop pool all shipped doing "
        f"nothing:\n  " + "\n  ".join(unreached)
    )


def test_the_exempt_list_names_only_symbols_that_exist() -> None:
    """An exemption for a symbol that has been renamed or deleted silences a check nobody asked
    to silence."""
    defined: Set[str] = set()
    for path in WATCHED:
        for names in _public_callables(path).values():
            defined |= names
    stale = sorted(set(EXEMPT) - defined)
    assert not stale, f"EXEMPT names symbols that no longer exist: {stale}"


def test_every_exemption_carries_a_reason() -> None:
    assert all(reason.strip() for reason in EXEMPT.values())
