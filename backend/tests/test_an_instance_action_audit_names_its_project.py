"""Every instance-shaped audit record names the LXD project it acted in.

The journal correlates a dashboard action with its LXD operation by
`(cluster, project, resource_type, resource_name, action)`. An audit call without
`target_project` lands in `default`, never matches the operation the daemon reported in the
instance's real project, and the panel lists the click twice. The adversarial review found
every `vm.*` call in `routers/vms.py` (the path the console drives) missing it; this keeps
both routers honest by reading the call sites.
"""

import ast
from pathlib import Path

ROUTERS = ("app/routers/vms.py", "app/routers/instances.py")
PREFIXES = ("vm.", "instance.")


def _audit_calls(path: Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "log_activity":
            continue
        kwargs = {k.arg: k.value for k in node.keywords if k.arg}
        action = kwargs.get("action")
        if isinstance(action, ast.Constant) and isinstance(action.value, str):
            yield node.lineno, action.value, kwargs


def test_every_vm_and_instance_audit_carries_target_project():
    missing = []
    for rel in ROUTERS:
        path = Path(__file__).resolve().parents[1] / rel
        for lineno, action, kwargs in _audit_calls(path):
            if not action.startswith(PREFIXES):
                continue
            if "target_project" not in kwargs:
                missing.append(f"{rel}:{lineno} {action}")
    assert missing == [], "audit calls without target_project:\n" + "\n".join(missing)
