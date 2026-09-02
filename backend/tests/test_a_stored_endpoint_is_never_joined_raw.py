"""Ratchet: nothing in lxd_certs may build a URL onto a raw stored endpoint.

The 404 that made a trusted MicroCloud read as "Not Authenticated" was one f-string:
`f"{endpoint}/1.0/instances"` on a value stored as "https://host:8443/". Three sibling call
sites in trust.py carried the same shape, and console_service, terminal_service and events_hub
had already been hardened with a local `.rstrip("/")` - so the codebase looked normalised at a
glance while the probe path was not. That asymmetry is exactly what a structural guard is for.

The rule: inside app/services/lxd_certs/, an f-string that joins a name containing "endpoint"
to a path must join a normalised value, not the raw name. `normalize_endpoint(endpoint)` inside
the f-string satisfies it, and so does a preceding `endpoint = normalize_endpoint(...)` binding.

No baseline count. Every in-scope site was converted in one pass, and an absolute rule cannot
rot the way a count can.

Scope is `lxd_certs/` deliberately, not the whole backend. Running this rule repo-wide reports
nineteen more joins, and all nineteen are safe for reasons this simple AST check cannot see:
`image_uploads.py`, `iso_uploads.py` and `lxd/instance.py` bind `endpoint` from
`client.api._api_endpoint`, which pylxd has already normalised, and `console_service.py`,
`terminal_service.py` and `lxd/events_hub.py` build their `ws_endpoint` with a local
`.rstrip("/")`. Teaching the checker those two shapes would make it cleverer and more brittle;
carrying them as a baseline of nineteen would invite exactly the drift a ratchet exists to
prevent. `lxd_certs/` is where an endpoint comes straight out of Vault unmediated, which is the
only place the raw join is dangerous, so that is where the absolute rule applies.
"""

import ast
import pathlib
import unittest

LXD_CERTS = pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "lxd_certs"
NORMALIZER = "normalize_endpoint"


def _normalized_names(tree: ast.AST) -> set:
    """Names bound from a normalize_endpoint(...) call anywhere in the module."""
    bound = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if isinstance(value, ast.Call) and getattr(value.func, "id", None) == NORMALIZER:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound.add(target.id)
    return bound


def _raw_joins(tree: ast.AST, normalized: set) -> list:
    """Line numbers where an endpoint-ish name is joined to a path without normalising."""
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        # A path join is an f-string whose literal part starts a URL path.
        joins_a_path = any(
            isinstance(part, ast.Constant)
            and isinstance(part.value, str)
            and part.value.startswith("/")
            for part in node.values
        )
        if not joins_a_path:
            continue
        for part in node.values:
            if not isinstance(part, ast.FormattedValue):
                continue
            inner = part.value
            if not isinstance(inner, ast.Name):
                continue
            if "endpoint" not in inner.id.lower():
                continue
            if inner.id not in normalized:
                offenders.append(node.lineno)
    return offenders


class StoredEndpointIsNormalizedTests(unittest.TestCase):
    def test_no_raw_endpoint_is_joined_to_a_path(self):
        detail = []
        for path in sorted(LXD_CERTS.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for line in _raw_joins(tree, _normalized_names(tree)):
                detail.append(f"{path.name}:{line}")

        self.assertEqual(
            detail,
            [],
            "an endpoint is joined to a URL path without normalize_endpoint, which rebuilds "
            "the double-slash 404: " + ", ".join(detail),
        )

    def test_the_guard_would_catch_the_original_defect(self):
        """Guard the guard: the pre-fix source must fail this rule."""
        original = 'test_url = f"{endpoint}/1.0/instances"\n'
        tree = ast.parse(original)
        self.assertEqual(len(_raw_joins(tree, _normalized_names(tree))), 1)

    def test_the_fixed_shape_passes(self):
        fixed = 'endpoint = normalize_endpoint(meta["endpoint"])\nurl = f"{endpoint}/1.0/instances"\n'
        tree = ast.parse(fixed)
        self.assertEqual(_raw_joins(tree, _normalized_names(tree)), [])

    def test_an_inline_normalizer_passes(self):
        inline = 'url = f"{normalize_endpoint(endpoint)}/1.0/instances"\n'
        tree = ast.parse(inline)
        self.assertEqual(_raw_joins(tree, _normalized_names(tree)), [])


if __name__ == "__main__":
    unittest.main()
