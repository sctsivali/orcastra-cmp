"""A control that exists and is tested but is never called is not a control.

A gate (a consent check, a key grant, a rate bucket, a dedicated thread pool) can be written,
unit-tested against stubs and named in documentation with no call site, and the suite stays green
because every test calls the function directly. A desktop pool nothing uses, for example, lets a
screenshot loop borrow `exec_executor` and starve every other integration's exec, which
`services/desktop/runtime.py` exists to prevent.

So this walks the packages where those controls live and requires each public callable to be
referenced from somewhere other than its own module and the tests. It is structural and noisy: a
new helper has to be wired or listed, and the list is where an exception justifies itself in
writing.
"""

import ast
from pathlib import Path
from typing import Dict, Set

APP = Path(__file__).resolve().parents[1] / "app"

# The packages whose contents are gates, transports or budgets rather than incidental helpers.
#
# important: a package missing from this list is not checked at all. An event dispatcher with no
# caller in an unwatched package means every catalogued add-on event silently never fires.
WATCHED = (
    APP / "services" / "desktop",
    APP / "services" / "desktop_policy.py",
    APP / "mcp",
    APP / "services" / "webhooks",
    # The task journal's scope translation and the package behind the task panel. A visibility
    # predicate nothing calls is a page that answers unscoped, which is the same shape again.
    APP / "core" / "task_scope.py",
    APP / "services" / "task_journal",
)

# Exceptions, each with the reason it is not a defect.
#
# important: an entry here claims the symbol is reached by something this walker cannot see, or
# that it is an interface rather than a call. "It will be used later" is not a reason.
EXEMPT: Dict[str, str] = {
    # A Protocol is referenced as an annotation. `DesktopProvider` is the seam a second provider
    # arrives through and appears in `_provider`'s return type.
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
    # The receiver's half of the signature recipe, published for add-on authors; the sender never
    # calls it. `test_a_delivery_arrives_signed_over_its_raw_bytes` signs with `sign` and verifies
    # with this, so the two cannot drift.
    "verify": "the receiver-side reference implementation, round-tripped against sign in tests",
    # The per-worker follower singleton is read by name in the election test to assert that a
    # step-down cleared it; the application reaches the follower through start/stop.
    "get_follower": "a test seam over the per-worker singleton, read by the election test",
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


def _referenced(name: str, *, owner: str, include_owner: bool) -> bool:
    """Whether `name` is called or read as an attribute under app/.

    A re-export in a package `__init__` does not count: exported and never called is the shape
    this file refuses.
    """
    for file in sorted(APP.rglob("*.py")):
        if file.name == "__init__.py":
            continue
        if str(file) == owner and not include_owner:
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


def _module_is_used(owner: str, names: Set[str]) -> bool:
    """Whether anything this module defines is reached from outside it."""
    return any(_referenced(name, owner=owner, include_owner=False) for name in names)


def test_no_gate_is_declared_without_being_reached() -> None:
    unreached = []
    for path in WATCHED:
        for owner, names in _public_callables(path).items():
            # A module whose entry point is reached may reach its own helpers internally, and
            # those are not dead. A module nothing reaches gets no such credit, so a whole
            # package that is never imported is still caught, and so is a function with no
            # reference anywhere at all.
            used = _module_is_used(owner, names)
            for name in sorted(names):
                if name in EXEMPT:
                    continue
                if not _referenced(name, owner=owner, include_owner=used):
                    unreached.append(f"{Path(owner).relative_to(APP)}::{name}")
    assert not unreached, (
        "these are defined and never reached from anywhere else in the application, which is how "
        "consent, the key grant, the rate buckets and the desktop pool all shipped doing "
        f"nothing:\n  " + "\n  ".join(unreached)
    )


def test_the_exempt_list_names_only_symbols_that_exist() -> None:
    """An exemption for a renamed or deleted symbol would silently exempt whatever reuses the
    name."""
    defined: Set[str] = set()
    for path in WATCHED:
        for names in _public_callables(path).values():
            defined |= names
    stale = sorted(set(EXEMPT) - defined)
    assert not stale, f"EXEMPT names symbols that no longer exist: {stale}"


def test_every_exemption_carries_a_reason() -> None:
    assert all(reason.strip() for reason in EXEMPT.values())
