"""A resource name in a URL path must not be able to change the request's query.

Found by a security review of this branch, and confirmed end to end against the installed
uvicorn, pylxd and requests rather than reasoned about:

  1. uvicorn percent-decodes the path AFTER splitting off the query string, so a request for
     `/networks/br0%3Fproject=orgA?host=nuc1` reaches the handler with
     `network_name == "br0?project=orgA"` and the route's own `project` parameter still None.
  2. pylxd's `_APINode.__getitem__` builds the path by f-string concatenation, unquoted.
  3. `requests` then appends its own params with `&`.

So the URL leaving this process was `/1.0/networks/br0?project=orgA&project=default`, which
LXD reads as project `orgA`. Every authorization check had already passed for `default`. It
applied to GET, PUT and DELETE alike, which made it a cross-tenant write, not only a read.

The fix is to encode every user-supplied name that becomes a path segment. These tests pin
both halves: the encoder, and the fact that no call site skips it.
"""

import ast
import pathlib

import pytest

from app.core.lxd_path import is_valid_lxd_name, safe_segment


class TestTheEncoder:
    @pytest.mark.parametrize(
        "hostile",
        [
            "br0?project=orgA",
            "br0#fragment",
            "br0&project=orgA",
            "../../instances/victim",
            "br0/state",
            "br0 with spaces",
            "br0%2Fstate",
        ],
    )
    def test_nothing_survives_that_could_leave_the_segment(self, hostile):
        encoded = safe_segment(hostile)
        for char in ("?", "#", "&", "/", " "):
            assert char not in encoded, f"{char!r} survived in {encoded!r}"

    def test_an_ordinary_name_is_unchanged(self):
        """Encoding must not break the normal case, or every read starts 404ing."""
        for name in ("br-uplink", "ovn-uat-hermes", "lxdbr0", "web-tier", "eth0"):
            assert safe_segment(name) == name

    def test_the_injection_from_the_review_is_neutralised(self):
        assert safe_segment("br0?project=orgA") == "br0%3Fproject%3DorgA"

    @pytest.mark.parametrize(
        "name,valid",
        [
            ("br-uplink", True),
            ("ovn-uat-hermes", True),
            ("br0?project=orgA", False),
            ("../escape", False),
            ("", False),
            ("9leading-digit", False),
            ("a" * 64, False),
        ],
    )
    def test_the_name_shape_check_agrees_with_lxd(self, name, valid):
        assert is_valid_lxd_name(name) is valid


# Every attribute chain that puts a value into an LXD path. `client.api[...]` and
# `client.api.instances[...]` both end in a subscript whose value lands in the URL.
BACKEND = pathlib.Path(__file__).resolve().parents[1] / "app"


def _is_api_chain(node: ast.AST) -> bool:
    """True when this subscript hangs off a `.api` attribute chain on a client."""
    cur = node
    while isinstance(cur, (ast.Subscript, ast.Attribute)):
        if isinstance(cur, ast.Attribute) and cur.attr == "api":
            return True
        cur = cur.value
    return False


def _offending_subscripts():
    offenders = []
    for path in sorted(BACKEND.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript) or not _is_api_chain(node):
                continue
            index = node.slice
            # A literal collection name is written by us, not by a caller.
            if isinstance(index, ast.Constant) and isinstance(index.value, str):
                continue
            # The one dynamic value that is ours: project_scope walks a fixed set of
            # collection names it chooses itself.
            if isinstance(index, ast.Name) and index.id == "collection":
                continue
            # note: lxd_segment is safe_segment plus a 400 for an empty or dot segment
            if (
                isinstance(index, ast.Call)
                and isinstance(index.func, ast.Name)
                and index.func.id in ("safe_segment", "lxd_segment")
            ):
                continue
            offenders.append(
                f"{path.relative_to(BACKEND.parent)}:{node.lineno} "
                f"puts {ast.unparse(index)} into an LXD path unencoded"
            )
    return offenders


def test_no_call_site_puts_a_raw_value_into_an_lxd_path():
    """A ratchet, because this is a whole-codebase property and the review found it in ten
    files at once. A new endpoint that indexes a name without safe_segment reopens it."""
    offenders = _offending_subscripts()
    assert offenders == [], "\n".join(offenders)


# ── the second way a name reaches the path: pylxd's manager API ────────────────────────────────
#
# The ratchet above walks `client.api[...]` subscripts, which is where this codebase builds a
# path by hand. It is blind to the other route into the same URL. `client.instances.get(name)`
# looks like an ORM call and is not one:
#
#     @classmethod
#     def get(cls, client, name):
#         response = client.api[cls._endpoint][name].get()
#
# pylxd does the subscripting itself, with the name exactly as handed over. So a manager call
# carries the identical injection, and the first ratchet reported a clean tree while 31 call
# sites across 11 files passed an unencoded name. Several were reachable from an unvalidated
# route parameter: `storage_pools.py` declares `pool_name: str` as a bare Path with no pattern,
# and the integration proxy takes its instance name from a JSON body.

MANAGER_ATTRS = frozenset(
    {
        "instances",
        "networks",
        "profiles",
        "projects",
        "images",
        "storage_pools",
        "certificates",
        "network_acls",
        "operations",
        "virtual_machines",
        "containers",
    }
)


def _offending_manager_calls():
    offenders = []
    for path in sorted(BACKEND.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in ("get", "delete"):
                continue
            owner = func.value
            if not (isinstance(owner, ast.Attribute) and owner.attr in MANAGER_ATTRS):
                continue
            if not node.args:
                continue
            first = node.args[0]
            # a literal is written by us, not by a caller
            if isinstance(first, ast.Constant):
                continue
            if (
                isinstance(first, ast.Call)
                and isinstance(first.func, ast.Name)
                and first.func.id in ("safe_segment", "lxd_segment")
            ):
                continue
            offenders.append(
                f"{path.relative_to(BACKEND.parent)}:{node.lineno} "
                f"passes {ast.unparse(first)} to {owner.attr}.{func.attr}() unencoded"
            )
    return offenders


def test_no_manager_call_passes_a_raw_name_to_pylxd():
    """pylxd subscripts the name itself, so a manager call is a path build in disguise."""
    offenders = _offending_manager_calls()
    assert offenders == [], "\n".join(offenders)
