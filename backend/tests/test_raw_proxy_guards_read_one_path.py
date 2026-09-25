"""
Ratchet: no raw-proxy guard reads the path except through canonical_proxy_path.

The raw-proxy bypasses (`/1.0/./certificates`, `/1.0/certificates#x`, `./exec`, a double-encoded
`project`, a fragment that skipped the migration fence) all came from a guard or the URL build
parsing the raw string its own way. This holds the one-parser rule structurally:

- a guard's `path` parameter is only ever handed to the canonical parser or to another guard that
  hands it on, never split, stripped, decoded, compared or aliased;
- in the router, `request.path` reaches only those guards and the record (log text, audit target);
- the URL raw_proxy sends is built from `canonical_proxy_path(request.path).request_target(...)`;
- none of these functions calls a URL parser of its own.
"""

import ast
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Set, Tuple

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
CAPABILITIES = APP / "core" / "api_key_capabilities.py"
RAW_PROXY_PATH = APP / "core" / "raw_proxy_path.py"
LIMITED_WRITES = APP / "core" / "limited_key_writes.py"
ROUTER = APP / "routers" / "integrations.py"

# Functions that take a raw-proxy path string, per file.
GUARDS: Dict[Path, Tuple[str, ...]] = {
    CAPABILITIES: (
        "proxy_path_segments",
        "policy_keys_in_raw_body",
        "proxy_destructive_action_for",
        "guest_access_capability_for",
        "assert_guest_access_allowed",
        "assert_project_scope_allows_path",
        "assert_proxy_path_allowed",
    ),
    RAW_PROXY_PATH: ("assert_server_state_unchanged",),
    LIMITED_WRITES: ("assert_body_within_grant", "limited_write_plan"),
    ROUTER: ("_proxy_audit_action",),
}

# The raw-proxy functions in the router that read `request.path`.
ROUTER_HANDLERS = (
    "raw_proxy",
    "_audit_raw_proxy",
    "_raw_proxy_fenced_write_target",
    "_raw_proxy_fenced_project",
    "_raw_proxy_has_cluster_wide_write",
)

# Calls that may receive the path: the parser, and guards that read it through the parser.
PATH_READERS: Set[str] = {"canonical_proxy_path", *(name for names in GUARDS.values() for name in names)}
# Keyword arguments that only record the path as text.
RECORD_KEYWORDS = {"target", "target_id"}
URL_PARSERS = {"unquote", "unquote_plus", "urlsplit", "urlparse", "parse_qs", "parse_qsl", "urljoin"}


def _functions(path: Path) -> Dict[str, ast.AST]:
    tree = ast.parse(path.read_text())
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _parents(function: ast.AST) -> Dict[ast.AST, ast.AST]:
    parents: Dict[ast.AST, ast.AST] = {}
    for node in ast.walk(function):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _callee(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ast.unparse(func)


def _misuses(function: ast.AST, is_path: Callable[[ast.AST], bool]) -> List[str]:
    """Every use of the path that is not a hand-off to a reader or a record."""
    parents = _parents(function)
    found: List[str] = []
    for node in ast.walk(function):
        if not is_path(node):
            continue
        parent = parents.get(node)
        if isinstance(parent, ast.Call) and node in parent.args and _callee(parent) in PATH_READERS:
            continue
        if isinstance(parent, ast.keyword) and parent.arg in RECORD_KEYWORDS:
            continue
        if isinstance(parent, ast.FormattedValue):
            continue
        if isinstance(parent, ast.Subscript) and isinstance(parent.slice, ast.Slice):
            grand = parents.get(parent)
            if isinstance(grand, ast.keyword) and grand.arg in RECORD_KEYWORDS:
                continue
        found.append(f"line {node.lineno}: {ast.unparse(parent) if parent is not None else ast.unparse(node)}")
    return found


def _parser_calls(function: ast.AST) -> List[str]:
    return [
        f"line {node.lineno}: {ast.unparse(node)}"
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and _callee(node) in URL_PARSERS
    ]


def _guard_cases() -> Iterable[Tuple[Path, str]]:
    for path, names in GUARDS.items():
        for name in names:
            yield path, name


@pytest.mark.parametrize("source,name", list(_guard_cases()))
def test_a_guard_hands_its_path_only_to_the_canonical_parser(source: Path, name: str) -> None:
    function = _functions(source).get(name)
    assert function is not None, f"{name} is gone from {source.name}; update this ratchet"

    def is_path(node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id == "path" and isinstance(node.ctx, ast.Load)

    assert any(is_path(node) for node in ast.walk(function)), f"{name} no longer reads `path`"
    assert _misuses(function, is_path) == [], f"{name} reads the path itself"
    assert _parser_calls(function) == [], f"{name} parses a URL itself"


@pytest.mark.parametrize("name", ROUTER_HANDLERS)
def test_the_router_hands_request_path_only_to_the_guards(name: str) -> None:
    function = _functions(ROUTER).get(name)
    assert function is not None, f"{name} is gone from the router; update this ratchet"

    def is_path(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "path"
            and isinstance(node.value, ast.Name)
            and node.value.id == "request"
            and isinstance(node.ctx, ast.Load)
        )

    assert any(is_path(node) for node in ast.walk(function)), f"{name} no longer reads request.path"
    assert _misuses(function, is_path) == [], f"{name} reads request.path outside the canonical parser"
    assert _parser_calls(function) == [], f"{name} parses a URL itself"


def test_every_guard_reaches_the_canonical_parser() -> None:
    """A guard that hands its path only to itself would pass the rule above and read nothing."""
    calls: Dict[str, Set[str]] = {}
    for source, names in GUARDS.items():
        functions = _functions(source)
        for name in names:
            calls[name] = {_callee(node) for node in ast.walk(functions[name]) if isinstance(node, ast.Call)}
    for name in calls:
        seen, frontier = set(), {name}
        while frontier:
            current = frontier.pop()
            seen.add(current)
            frontier |= (calls.get(current, set()) & set(calls)) - seen
        reached = set().union(*(calls[item] for item in seen))
        assert "canonical_proxy_path" in reached, f"{name} never reaches canonical_proxy_path"


def test_the_sent_url_is_built_from_the_canonical_path() -> None:
    raw_proxy = _functions(ROUTER)["raw_proxy"]
    assignments = [
        node for node in ast.walk(raw_proxy)
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "full_url" for target in node.targets
        )
    ]
    assert len(assignments) == 1, "raw_proxy builds the sent URL in more than one place"
    built = ast.unparse(assignments[0].value)
    assert "canonical_proxy_path(request.path).request_target(project)" in built, built
    senders = [
        node for node in ast.walk(raw_proxy)
        if isinstance(node, ast.Call) and any(
            isinstance(arg, ast.Name) and arg.id == "raw_request" for arg in node.args
        )
    ]
    assert senders, "raw_proxy no longer sends through raw_request"
    for call in senders:
        assert "full_url" in {arg.id for arg in call.args if isinstance(arg, ast.Name)}, ast.unparse(call)


def test_the_capability_module_imports_no_url_parser() -> None:
    tree = ast.parse(CAPABILITIES.read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and (getattr(node, "module", None) or "").startswith("urllib")
        for alias in node.names
    }
    assert not imported, f"api_key_capabilities.py imports {sorted(imported)}; read canonical_proxy_path instead"
    assert "canonical_proxy_path" in _functions(RAW_PROXY_PATH)
