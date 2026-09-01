"""A pylxd path must be built with indexing, never passed as a positional string.

`client.api` is already rooted at `<endpoint>/1.0`, and every verb forwards its positional
arguments straight to the underlying requests Session:

    def get(self, *args, **kwargs):
        response = self.session.get(self._api_endpoint, *args, **kwargs)

`requests.Session.get(url, params=None, **kwargs)` takes **params** second, `post` and `put`
take **data** second, and `delete` takes nothing second at all. So a call written as
`client.api.get('/1.0/network-acls?recursion=1')` does not request that path. It requests the
API root with the path as the query string, and the response parses into nothing.

That is not hypothetical. Seven calls across two routers were written this way, and the whole
Network ACL feature was inert because of it: the list endpoint answered `total: 0` for every
cluster while logging one swallowed "'str' object has no attribute 'get'" per node, create and
update addressed the root, and delete raised `TypeError: Session.delete() takes 2 positional
arguments but 3 were given`. Nothing surfaced, because the inner handler that keeps one bad
item from condemning a whole node also hid the fact that every item was bad.

The correct idiom is already used elsewhere in this codebase:

    client.api["storage-pools"][pool_name].get(params={"target": member_name})
"""

import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
VERBS = {"get", "post", "put", "delete", "patch", "head"}


def _api_calls_with_a_positional_path(source: str):
    """Yield (lineno, verb) for every `<something>.api.<verb>('...')` call."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in VERBS:
            continue
        # the receiver must be `<x>.api`, which is the node rooted at /1.0
        receiver = func.value
        if not (isinstance(receiver, ast.Attribute) and receiver.attr == "api"):
            continue
        first = node.args[0]
        is_str = isinstance(first, ast.Constant) and isinstance(first.value, str)
        is_fstr = isinstance(first, ast.JoinedStr)
        if is_str or is_fstr:
            yield node.lineno, func.attr


def test_no_router_or_service_passes_a_path_as_a_positional_argument():
    offenders = [
        f"{path.relative_to(APP.parent)}:{lineno} passes a path positionally to api.{verb}(), "
        f"which requests sends as its second positional parameter instead of as the path"
        for path in sorted(APP.rglob("*.py"))
        for lineno, verb in _api_calls_with_a_positional_path(path.read_text())
    ]
    assert offenders == [], "\n".join(offenders)


def _api_nodes_indexing_the_version_again(source: str):
    """Yield the lineno of every `<x>.api["1.0"]`, which addresses /1.0/1.0."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        value = node.value
        if not (isinstance(value, ast.Attribute) and value.attr == "api"):
            continue
        key = node.slice
        if isinstance(key, ast.Constant) and key.value == "1.0":
            yield node.lineno


def test_nothing_indexes_the_version_segment_twice():
    """`client.api` IS /1.0, so indexing "1.0" again targets /1.0/1.0 and hits nothing.

    pylxd's own client calls `self.api.get()` for that resource, which is the shape to copy.
    Found in lxd_server.py, where a server-config PUT was addressing /1.0/1.0 and so never
    wrote the config it was given.
    """
    offenders = [
        f"{path.relative_to(APP.parent)}:{lineno} indexes '1.0' on a node already rooted there"
        for path in sorted(APP.rglob("*.py"))
        for lineno in _api_nodes_indexing_the_version_again(path.read_text())
    ]
    assert offenders == [], "\n".join(offenders)


def test_the_detector_catches_each_broken_shape():
    """The guard is worthless if it only matches the exact lines already fixed."""
    broken = """
def h(client, name, body):
    client.api.get('/1.0/network-acls?recursion=1')
    client.api.get(f'/1.0/network-acls/{name}')
    client.api.post('/1.0/network-acls', json=body)
    client.api.put(f'/1.0/network-acls/{name}', json=body)
    client.api.delete(f'/1.0/network-acls/{name}')
"""
    assert len(list(_api_calls_with_a_positional_path(broken))) == 5

    correct = """
def h(client, name, body):
    client.api["network-acls"].get(params={"recursion": 1})
    client.api["network-acls"][name].get()
    client.api["network-acls"].post(json=body)
    client.api["network-acls"][name].put(json=body)
    client.api["network-acls"][name].delete()
    client.api.get(timeout=(3, 5))
    other.get('/some/path')
"""
    assert list(_api_calls_with_a_positional_path(correct)) == []


def test_a_raw_session_call_is_not_an_offender():
    """`client.api.session` is a requests Session, where a positional URL IS the signature.

    ipam.py deliberately drops to it to inject a timeout that `_APINode` would not, and without
    that timeout the call blocks forever against a silent node and burns a pool thread. The
    rule is "a path positional to an _APINode verb", not "a positional argument anywhere under
    .api", so widening this guard must not swallow that call.
    """
    raw = """
def h(client, full_url, t):
    client.api.session.get(full_url, timeout=t)
    client.api.session.post(full_url, json={})
"""
    assert list(_api_calls_with_a_positional_path(raw)) == []
