"""Ratchet: a metadata-store call must name a method the store actually has.

lxd_server.py called `metadata_store.get_node(node_name)` in two handlers. NodeMetadataStore
never defined it. Every call raised AttributeError straight into a bare `except Exception`
logged at debug level, so the Incus/MicroCloud override those blocks existed to apply had
never run once, and nothing in the logs said so above debug.

A behavioural test cannot catch this: the handler returns a correct-looking response either
way, because the fallback it silently took was the live detection. Only the structure shows it.

The rule: any attribute called on a name ending in `metadata_store` must exist on
NodeMetadataStore. Public surface only - a private helper is the class's own business.
"""

import ast
import pathlib
import unittest

from app.core.node_metadata import NodeMetadataStore

APP = pathlib.Path(__file__).resolve().parents[1] / "app"


def _store_calls(tree: ast.AST) -> list:
    """(line, attribute) for every call made on a *metadata_store name."""
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        target = func.value
        if isinstance(target, ast.Name) and target.id.endswith("metadata_store"):
            calls.append((node.lineno, func.attr))
    return calls


class StoreCallsResolveTests(unittest.TestCase):
    def test_every_metadata_store_call_names_a_real_method(self):
        available = {name for name in dir(NodeMetadataStore) if not name.startswith("_")}
        missing = []
        for path in sorted(APP.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for line, attr in _store_calls(tree):
                if attr not in available:
                    missing.append(f"{path.relative_to(APP.parent)}:{line} .{attr}()")

        self.assertEqual(
            missing,
            [],
            "a metadata-store method that does not exist; the call raises AttributeError into "
            "whatever handler wraps it: " + ", ".join(missing),
        )

    def test_the_methods_the_fix_relies_on_are_present(self):
        for name in ("get_all_node_metadata", "set_node_type", "set_node_display_name"):
            self.assertTrue(hasattr(NodeMetadataStore, name), name)

    def test_the_guard_would_catch_the_original_defect(self):
        tree = ast.parse("stored = metadata_store.get_node(node_name)\n")
        self.assertEqual(_store_calls(tree), [(1, "get_node")])
        self.assertNotIn("get_node", {n for n in dir(NodeMetadataStore) if not n.startswith("_")})


if __name__ == "__main__":
    unittest.main()
