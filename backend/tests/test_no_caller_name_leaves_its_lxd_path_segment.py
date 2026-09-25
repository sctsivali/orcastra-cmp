"""Ratchet: on every surface an API key reaches, a value put into an LXD URL path is encoded.

pylxd builds a URL by string concatenation (`_APINode.__getitem__`) and the upload routers build
theirs with f-strings, so a caller's name lands in the path as it is. `x?project=other&` then reads
or deletes in a project the key was never granted, and `..` is folded out of the path by requests.
Found on /proxy delete_snapshot and on the image-upload alias reads and repoint delete.

Every path segment built from a variable must be `safe_segment(...)` or `lxd_segment(...)`
(core/lxd_segment.py refuses a dot segment too), or a name assigned only from one of those in the
same function. Three shapes are read:

- an f-string naming an LXD resource, where a value follows a `/`;
- a subscript on a pylxd node (`client.api.instances[...]`, `...snapshots[...]`);
- a pylxd manager lookup (`client.instances.get(...)`, `inst.snapshots.get(...)`).
"""

import ast
import re
from pathlib import Path
from typing import Dict, Iterator, List, Set, Tuple

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
    found: Set[Tuple[int, str]] = set()
    for node, encoded in _with_scope(ast.parse(source), set()):
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


def test_every_lxd_path_segment_on_a_key_surface_is_encoded() -> None:
    offenders = [
        line for path in KEY_REACHABLE
        for line in _unencoded_segments(path.read_text(), str(path.relative_to(APP.parent)))
    ]
    assert offenders == [], "caller values reach an LXD URL path unencoded:\n" + "\n".join(offenders)


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
