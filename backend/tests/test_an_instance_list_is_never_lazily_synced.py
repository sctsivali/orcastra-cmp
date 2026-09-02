"""`client.instances.all()` must always name its recursion. A structural guard, on purpose.

pylxd's Instance.all defaults to recursion=0, which asks the daemon for a list of URLs. pylxd
then builds one Instance per URL carrying only a name (pylxd/models/instance.py, the `all`
classmethod). Every attribute this app reads afterwards - location, status, config - is
declared in `__attributes__`, so the read falls into `Model.__getattribute__`, which calls
`sync()`, which is `self.api.get()`. One HTTP request per instance in the whole cluster,
sequential, each bounded by the pooled (3, 8) socket timeout.

Nothing at the call site shows any of that. The line reads like a single list fetch, the
attribute read below it reads like a dict lookup, and the cost only appears as a member
resource panel that times out on a cluster with enough instances. Both call sites had it:
routers/cluster_members.py and services/lxd/node_resources.py, the latter feeding the whole
clustered aggregate.

A behavioural test covers each call site separately. This is the net underneath, because the
next one will look exactly as harmless.
"""

import ast
from pathlib import Path


APP = Path(__file__).resolve().parents[1] / "app"


def _bare_instance_list_calls(tree: ast.AST):
    """Every `<...>.instances.all(...)` that does not pass a recursion."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "all":
            continue
        owner = func.value
        if not isinstance(owner, ast.Attribute) or owner.attr != "instances":
            continue
        if any(kw.arg == "recursion" for kw in node.keywords):
            continue
        if node.args:
            # A positional recursion is still a recursion, ex; all(1).
            continue
        yield node.lineno


def test_no_instance_list_is_fetched_without_a_recursion():
    """No allowlist. A new bare call is always a regression, never a style choice."""
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno in _bare_instance_list_calls(tree):
            offenders.append(f"{path.relative_to(APP.parent)}:{lineno}")

    assert not offenders, (
        "instances.all() defaults to recursion=0, which costs one extra HTTP request per "
        "instance the first time any attribute is read. Pass recursion=1. Offenders: "
        + ", ".join(offenders)
    )


def test_the_guard_can_actually_see_a_bare_call():
    """Ablation. A guard that cannot fail is not a guard."""
    bad = ast.parse("client.instances.all()\n")
    assert list(_bare_instance_list_calls(bad)) == [1]

    good = ast.parse("client.instances.all(recursion=1)\n")
    assert list(_bare_instance_list_calls(good)) == []

    unrelated = ast.parse("thing.all()\nclient.images.all()\n")
    assert list(_bare_instance_list_calls(unrelated)) == []
