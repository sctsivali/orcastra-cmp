"""A resource name in a URL path must not be able to change the request's query.

uvicorn percent-decodes the path after splitting off the query, so
`/networks/br0%3Fproject=orgA?host=node1` reaches the handler as
`network_name == "br0?project=orgA"`. pylxd's `_APINode.__getitem__` concatenates it unquoted and
`requests` appends its own params with `&`, so unencoded the URL leaving this process is
`/1.0/networks/br0?project=orgA&project=default`, which LXD reads as project `orgA` after every
check passed for `default`: a cross-tenant read and write (GET, PUT, DELETE). These tests pin the
encoder and that no call site skips it.
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
        for name in ("br-uplink", "ovn-uat-herald", "lxdbr0", "web-tier", "eth0"):
            assert safe_segment(name) == name

    def test_a_known_injection_is_neutralised(self):
        assert safe_segment("br0?project=orgA") == "br0%3Fproject%3DorgA"

    @pytest.mark.parametrize(
        "name,valid",
        [
            ("br-uplink", True),
            ("ovn-uat-herald", True),
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
            # A literal collection name comes from the code, never from a caller.
            if isinstance(index, ast.Constant) and isinstance(index.value, str):
                continue
            # The one dynamic value the code chooses: project_scope walks a fixed set of
            # collection names.
            if isinstance(index, ast.Name) and index.id == "collection":
                continue
            # lxd_segment is safe_segment plus a 400 for an empty or dot segment.
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
    """A whole-codebase property: a new endpoint that indexes a name without safe_segment
    reopens the injection."""
    offenders = _offending_subscripts()
    assert offenders == [], "\n".join(offenders)


# ── the second way a name reaches the path: pylxd's manager API ────────────────────────────────
#
# The check above walks `client.api[...]` subscripts, where this codebase builds a path by
# hand. `client.instances.get(name)` reaches the same URL and looks like an ORM call, but is not:
#
#     @classmethod
#     def get(cls, client, name):
#         response = client.api[cls._endpoint][name].get()
#
# pylxd subscripts the name exactly as handed over, so a manager call carries the same
# injection. Names reach these calls from route parameters without a pattern and from JSON
# bodies, ex; the instance name the integration proxy receives.

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
            # A literal comes from the code, never from a caller.
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
