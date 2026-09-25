"""Ratchet: on every surface an API key reaches, a value put into an LXD URL path is encoded.

pylxd builds a URL by string concatenation (`_APINode.__getitem__`) and the upload routers build
theirs with f-strings, so a caller's name lands in the path as it is. `x?project=other&` then reads
or deletes in a project the key was never granted, and `..` is folded out of the path by requests.
Found on /proxy delete_snapshot and on the image-upload alias reads and repoint delete.

Every path segment built from a variable must be `safe_segment(...)` or `lxd_segment(...)`
(core/lxd_segment.py refuses a dot segment too), or a name assigned only from one of those in the
same function. A value that came from a request must use `lxd_segment`, since `safe_segment` leaves
`.` and `..` for requests to fold out of the path. `safe_segment` stays only where the code read the
value from LXD or built it itself, and each such site is listed in SAFE_SEGMENT_SITES with the reason.
A key route also reaches LXD through helpers it imports (services/lxd/utils.find_instance_in_projects
put the one-instance read's caller name into the path with safe_segment, so `..` read LXD's server
root). So the scan follows every function a scanned module imports from app/, every class it
constructs, and what those use in turn, and holds them to the same rules. Three shapes are read:

- an f-string naming an LXD resource, where a value follows a `/`
- a subscript on a pylxd node (`client.api.instances[...]`, `...snapshots[...]`)
- a pylxd manager lookup (`client.instances.get(...)`, `inst.snapshots.get(...)`).
"""

import ast
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

APP = Path(__file__).resolve().parents[1] / "app"
KEY_REACHABLE = sorted(
    {
        APP / "routers" / "image_uploads.py",
        APP / "routers" / "iso_uploads.py",
        APP / "routers" / "integrations.py",
        APP / "services" / "lxd" / "proxy_operations.py",
        APP / "services" / "lxd" / "proxy.py",
        APP / "services" / "lxd" / "snapshots.py",
        APP / "services" / "lxd" / "limited_write_reach.py",
        APP / "services" / "extension_install.py",
        APP / "services" / "orcahub_sync.py",
        APP / "services" / "orcahub_webhook.py",
        APP / "services" / "lxd" / "guest_exec.py",
        APP / "services" / "lxd" / "guest_files.py",
        APP / "services" / "lxd" / "guest_output.py",
        *(APP / "services" / "desktop").glob("*.py"),
        *(APP / "mcp").glob("*.py"),
    }
)
ENCODERS = {"safe_segment", "lxd_segment"}
RESOURCE_MARKERS = (
    "/1.0", "/instances", "/images", "/aliases", "/storage-pools", "/volumes", "/profiles",
    "/networks", "/projects", "/operations", "/snapshots", "/logs", "/files",
)
PYLXD_NODE = re.compile(
    r"(^|\.)(api|instances|virtual_machines|containers|snapshots|images|aliases|profiles|networks|"
    r"storage_pools|volumes|operations|projects|logs)$"
)
MANAGER_LOOKUPS = {"get", "get_by_alias", "exists"}

# note: every safe_segment call in the scanned modules, as (file, function, call), and why its value
# cannot come from a request. A new call fails until it is either lxd_segment or listed here.
SAFE_SEGMENT_SITES: Dict[Tuple[str, str, str], str] = {
    ("app/routers/image_uploads.py", "_resolve_alias_sync", "safe_segment(target)"):
        "the fingerprint LXD's alias record points at",
    ("app/routers/image_uploads.py", "_lxd_import_sync", "safe_segment(precomputed_fp)"):
        "the sha256 this process computed of the staged file (_run_import passes its digest)",
    ("app/routers/image_uploads.py", "_lxd_import_sync", "safe_segment(fingerprint)"):
        "the fingerprint LXD's import answered with, or the computed digest",
    ("app/services/lxd/snapshots.py", "wait_for_operation", "safe_segment(operation_id)"):
        "the operation id LXD answered a snapshot write with",
    ("app/services/lxd/guest_output.py", "_fetch_log", "safe_segment(name)"):
        "the exec-output log name LXD's exec operation reports",
    ("app/services/lxd/guest_output.py", "_discard_log", "safe_segment(name)"):
        "the exec-output log name LXD's exec operation reports",
    ("app/services/lxd/guest_output.py", "run_recorded", "safe_segment(operation_id)"):
        "the operation id LXD answered the exec with",
    **{
        ("app/services/lxd/utils.py", method, "safe_segment(self.name)"):
            "the name in the instance record LXD answered find_instance_in_projects with"
        for method in ("start", "stop", "restart", "delete", "sync", "state")
    },
    ("app/services/lxd/utils.py", "wait_for_lxd_operation", "safe_segment(operation_id)"):
        "the operation id LXD answered the write with",
}

# note: what a route is handed rather than hands a name to. A key surface uses the service only through
# SERVICE_CALLS, which take a cluster id or read the cluster registry, and the scan checks that. The two
# bare managers are handed whole to status.probe_clusters, a followed helper, for the availability probe.
DEPENDENCY_PROVIDERS: Dict[str, str] = {
    "get_lxd_service": "the FastAPI dependency that hands a route the LXDService",
    "LXDService": "the service a route is handed, reached only through SERVICE_CALLS",
}
SERVICE_CALLS = {
    "cert_manager", "cert_manager.get_cluster_info", "cert_manager.read_registry", "check_node_status",
    "connection_manager", "connection_manager.get_client", "connection_manager.get_project_client",
    "ensure_connection", "get_node_names", "instance_manager.fetch_node_instances",
}
# note: the encoders themselves, which the rules above describe rather than obey.
ENCODER_MODULES = {APP / "core" / "lxd_segment.py", APP / "core" / "lxd_path.py"}
# note: the followed helpers that build an LXD path, pinned so a new one is a reviewed addition to the scan.
LXD_PATH_HELPERS = {
    ("app/core/cloud_init_config.py", "cloud_init_compared_at"),
    ("app/core/cloud_init_config.py", "proxy_cloud_init_compared_at"),
    ("app/services/lxd/proxy_path_params.py", "assert_path_params"),
    ("app/services/lxd/utils.py", "InstanceWrapper"),
    ("app/services/lxd/utils.py", "find_instance_in_projects"),
    ("app/services/lxd/utils.py", "wait_for_lxd_operation"),
    ("app/services/shared_cluster_audit.py", "_read_projects"),
}


def _is_encoder_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
    return name in ENCODERS


def _encoded_names(function: ast.AST) -> Set[str]:
    """Names every assignment in `function` binds to an encoder call, and nothing else."""
    bound: Dict[str, bool] = {}
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound[target.id] = bound.get(target.id, True) and _is_encoder_call(node.value)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)) and isinstance(node.target, ast.Name):
            bound[node.target.id] = False
    return {name for name, encoded in bound.items() if encoded}


def _is_encoded(node: ast.AST, encoded_names: Set[str]) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.Name) and node.id in encoded_names:
        return True
    return _is_encoder_call(node)


_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _with_scope(node: ast.AST, encoded: Set[str]) -> Iterator[Tuple[ast.AST, Set[str]]]:
    """Every node with the encoded names of its innermost function (a closure sees its parent's)."""
    if isinstance(node, _FUNCTIONS):
        own = _encoded_names(node)
        assigned = {t.id for n in ast.walk(node) if isinstance(n, ast.Assign) for t in n.targets
                    if isinstance(t, ast.Name)}
        encoded = own | (encoded - assigned)
    yield node, encoded
    for child in ast.iter_child_nodes(node):
        yield from _with_scope(child, encoded)


def _unencoded_segments(source: str, label: str) -> List[str]:
    return _unencoded_segments_in(ast.parse(source), label)


def _unencoded_segments_in(tree: ast.AST, label: str) -> List[str]:
    found: Set[Tuple[int, str]] = set()
    for node, encoded in _with_scope(tree, set()):
        if isinstance(node, ast.JoinedStr):
            text = "".join(part.value for part in node.values if isinstance(part, ast.Constant))
            if not any(marker in text for marker in RESOURCE_MARKERS):
                continue
            for before, part in zip(node.values, node.values[1:]):
                if (
                    isinstance(part, ast.FormattedValue)
                    and isinstance(before, ast.Constant)
                    and str(before.value).endswith("/")
                    and not _is_encoded(part.value, encoded)
                ):
                    found.add((node.lineno, ast.unparse(part.value)))
        elif isinstance(node, ast.Subscript) and PYLXD_NODE.search(ast.unparse(node.value)):
            if not _is_encoded(node.slice, encoded):
                found.add((node.lineno, ast.unparse(node)))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in MANAGER_LOOKUPS
            and PYLXD_NODE.search(ast.unparse(node.func.value))
            and node.args
            and not _is_encoded(node.args[0], encoded)
        ):
            found.add((node.lineno, ast.unparse(node)))
    return [f"{label}:{line}: {text}" for line, text in sorted(found)]


def _safe_segment_calls(source: str, label: str) -> List[Tuple[str, str, str, int]]:
    return _safe_segment_calls_in(ast.parse(source), label)


def _safe_segment_calls_in(tree: ast.AST, label: str) -> List[Tuple[str, str, str, int]]:
    """Every safe_segment call, as (file, enclosing function, call, line)."""
    found: List[Tuple[str, str, str, int]] = []

    def visit(node: ast.AST, function: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if _is_encoder_call(node) and ast.unparse(node).startswith(("safe_segment(", "lxd_path.safe_segment(")):
            found.append((label, function, ast.unparse(node), node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, "<module>")
    return found


def _label(path: Path) -> str:
    return str(path.relative_to(APP.parent))


def _module_file(parts: List[str], base: Path) -> Optional[Path]:
    target = base.joinpath(*parts)
    for candidate in (target.with_suffix(".py"), target / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


@lru_cache(maxsize=None)
def _index(path: Path) -> Tuple[ast.Module, Dict[str, ast.AST], Dict[str, Tuple[Path, Optional[str]]]]:
    """A module's tree, its top-level functions and classes, and each name it imports from app/.

    An imported name maps to (module file, name there), or to (module file, None) for a module.
    """
    tree = ast.parse(path.read_text())
    defs = {node.name: node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    imports: Dict[str, Tuple[Path, Optional[str]]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base, parts = path.parents[node.level - 1], (node.module or "").split(".") if node.module else []
            elif (node.module or "").split(".")[0] == "app":
                base, parts = APP.parent, node.module.split(".")
            else:
                continue
            for alias in node.names:
                submodule = _module_file([*parts, alias.name], base)
                module = _module_file(parts, base)
                if submodule is not None:
                    imports[alias.asname or alias.name] = (submodule, None)
                elif module is not None:
                    imports[alias.asname or alias.name] = (module, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                module = _module_file(alias.name.split("."), APP.parent) if alias.name.startswith("app.") else None
                if module is not None and alias.asname:
                    imports[alias.asname] = (module, None)
    return tree, defs, imports


def _references(path: Path, node: ast.AST) -> List[Tuple[Path, str, bool]]:
    """Every app/ function, class or module attribute `node` names, and whether it calls it."""
    _, defs, imports = _index(path)
    called = {id(call.func) for call in ast.walk(node) if isinstance(call, ast.Call)}
    found: List[Tuple[Path, str, bool]] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            if child.id in defs and defs[child.id] is not node:
                found.append((path, child.id, id(child) in called))
            elif child.id in imports and imports[child.id][1] is not None:
                module, name = imports[child.id]
                found.append((module, name, id(child) in called))
        elif (
            isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name)
            and child.value.id in imports and imports[child.value.id][1] is None
        ):
            found.append((imports[child.value.id][0], child.attr, id(child) in called))
    return found


@lru_cache(maxsize=None)
def _followed_helpers() -> Dict[Tuple[str, str], Tuple[Path, ast.AST]]:
    """Every function a key-reachable module uses from app/ outside the scanned set, every class it
    constructs, and what those use in turn, by (file, name)."""
    reached: Dict[Tuple[str, str], Tuple[Path, ast.AST]] = {}
    frontier = [ref for path in KEY_REACHABLE for ref in _references(path, _index(path)[0])]
    while frontier:
        path, name, constructed = frontier.pop()
        if path in KEY_REACHABLE or path in ENCODER_MODULES or name in DEPENDENCY_PROVIDERS:
            continue
        if (_label(path), name) in reached:
            continue
        _, defs, imports = _index(path)
        node = defs.get(name)
        if node is None:
            if imports.get(name, (None, None))[1] is not None:
                frontier.append((*imports[name], constructed))
            continue
        if isinstance(node, ast.ClassDef) and not constructed:
            continue
        reached[(_label(path), name)] = (path, node)
        frontier.extend(_references(path, node))
    return reached


def _all_safe_segment_calls() -> List[Tuple[str, str, str, int]]:
    return [
        call for path in KEY_REACHABLE
        for call in _safe_segment_calls(path.read_text(), _label(path))
    ] + [
        call for (label, _name), (_path, node) in _followed_helpers().items()
        for call in _safe_segment_calls_in(node, label)
    ]


def test_a_request_value_is_encoded_with_lxd_segment() -> None:
    unlisted = [
        f"{label}:{line}: {call} in {function}" for label, function, call, line in _all_safe_segment_calls()
        if (label, function, call) not in SAFE_SEGMENT_SITES
    ]
    assert unlisted == [], (
        "safe_segment keeps a `.` or `..` a request sent, so use lxd_segment, or list the site in "
        "SAFE_SEGMENT_SITES with why the value cannot come from a request:\n" + "\n".join(unlisted)
    )


def test_every_listed_safe_segment_site_still_exists() -> None:
    present = {(label, function, call) for label, function, call, _ in _all_safe_segment_calls()}
    assert sorted(set(SAFE_SEGMENT_SITES) - present) == []


def test_the_safe_segment_scan_sees_a_request_name() -> None:
    sample = (
        "def f(client, params):\n"
        "    client.api.instances[safe_segment(params['name'])].get()\n"
        "    client.api.instances[lxd_segment(params['name'], what='x')].get()\n"
    )
    assert [(function, call) for _, function, call, _ in _safe_segment_calls(sample, "probe")] == [
        ("f", "safe_segment(params['name'])")
    ]


def test_every_lxd_path_segment_on_a_key_surface_is_encoded() -> None:
    offenders = [
        line for path in KEY_REACHABLE
        for line in _unencoded_segments(path.read_text(), _label(path))
    ] + [
        line for (label, _name), (_path, node) in _followed_helpers().items()
        for line in _unencoded_segments_in(node, label)
    ]
    assert offenders == [], "caller values reach an LXD URL path unencoded:\n" + "\n".join(offenders)


def _builds_an_lxd_path(node: ast.AST) -> bool:
    return any(
        _is_encoder_call(child)
        or (isinstance(child, ast.Subscript) and PYLXD_NODE.search(ast.unparse(child.value)))
        or (
            isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
            and child.func.attr in MANAGER_LOOKUPS and PYLXD_NODE.search(ast.unparse(child.func.value))
        )
        for child in ast.walk(node)
    )


def test_the_scan_follows_the_helpers_a_key_route_calls() -> None:
    """The one-instance read's helper is followed, and every followed helper that builds a path is known."""
    followed = _followed_helpers()
    assert ("app/services/lxd/utils.py", "find_instance_in_projects") in followed
    assert {key for key, (_path, node) in followed.items() if _builds_an_lxd_path(node)} == LXD_PATH_HELPERS


def _service_attributes() -> Set[str]:
    tree = _index(APP / "services" / "lxd" / "lxd_service.py")[0]
    service = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "LXDService")
    names = {node.name for node in service.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    names |= {node.attr for node in ast.walk(service) if isinstance(node, ast.Attribute)
              and isinstance(node.value, ast.Name) and node.value.id == "self" and isinstance(node.ctx, ast.Store)}
    return {name for name in names if not name.startswith("__")}


def test_a_key_route_calls_the_service_it_is_handed_only_with_a_cluster_id() -> None:
    """Why the service is not followed: a key surface reaches it only through SERVICE_CALLS."""
    attributes = _service_attributes()
    trees = [(_label(path), _index(path)[0]) for path in KEY_REACHABLE]
    trees += [(label, node) for (label, _name), (_path, node) in _followed_helpers().items()]
    used: Set[str] = set()
    for _label_, tree in trees:
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in attributes and isinstance(node.value, ast.Name) \
                    and node.value.id != "self":
                parent = parents.get(node)
                chained = isinstance(parent, ast.Attribute) and parent.value is node
                used.add(f"{node.attr}.{parent.attr}" if chained else node.attr)
    assert used and used <= SERVICE_CALLS, sorted(used - SERVICE_CALLS)


def test_the_scan_sees_the_shapes_it_names() -> None:
    """The three shapes are recognised, so a green run is not a scan that reads nothing."""
    sample = (
        "def f(client, name, endpoint):\n"
        "    client.api.instances[name].get()\n"
        "    client.instances.get(name)\n"
        "    session.get(f'{endpoint}/images/aliases/{name}')\n"
        "    ok = safe_segment(name)\n"
        "    client.api.instances[ok].snapshots['s'].get()\n"
    )
    offenders = _unencoded_segments(sample, "probe")
    assert [line.split(": ", 1)[1] for line in offenders] == [
        "client.api.instances[name]", "client.instances.get(name)", "name",
    ]
