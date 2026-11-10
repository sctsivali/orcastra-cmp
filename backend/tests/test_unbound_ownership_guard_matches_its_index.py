"""The unbound-ownership guard must filter on exactly the columns its index covers.

`uq_org_cluster_null` (db_models.py) is a partial unique index on `cluster_id` alone, where
`organization_id IS NULL`. Postgres treats NULLs as distinct, so `uq_org_cluster` does not
cover org-less rows; this partial index does.

If the existence check in `register_cluster` filters on an extra column (ex; `registered_by`),
a cluster claimed by another user misses the check, the INSERT violates the index, and the
broad `except Exception` around the block swallows it: registration succeeds with no
ownership recorded. Fewer columns are wrong too, and the same equality catches them. The
check is structural (the block runs only after Vault and the certificate manager) and compares
column sets, so a differently spelled defect still fails.
"""

import ast
import pathlib
import unittest


ROUTER = (
    pathlib.Path(__file__).resolve().parents[1]
    / "app"
    / "routers"
    / "cluster_registration.py"
)
MODELS = pathlib.Path(__file__).resolve().parents[1] / "app" / "models" / "db_models.py"

# The column uq_org_cluster_null covers, plus the organization_id IS NULL predicate that scopes
# it. Both belong in the guard and nothing else does.
EXPECTED = {"organization_id", "cluster_id"}


def _filtered_columns(where_call: ast.Call) -> set:
    """Columns named inside `.where(...)` only.

    The projection is ignored: the lookup selects `registered_by` to name the holder in the
    log, and only the WHERE has to match the index.
    """
    found = set()
    for arg in where_call.args:
        for node in ast.walk(arg):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "OrganizationCluster"
            ):
                found.add(node.attr)
    return found


class UnboundOwnershipGuardTests(unittest.TestCase):
    def test_the_index_is_still_shaped_the_way_this_test_assumes(self):
        """If the index changes, this test's premise is stale and must fail loudly."""
        src = MODELS.read_text()
        self.assertIn(
            'Index(\n            "uq_org_cluster_null",\n            "cluster_id",',
            src,
            "uq_org_cluster_null is no longer a partial index on cluster_id alone. The guard "
            "in cluster_registration.py must be re-derived from whatever it is now, and this "
            "test's EXPECTED set updated with it.",
        )
        self.assertIn('postgresql_where=text("organization_id IS NULL")', src)

    def test_the_guard_filters_on_exactly_the_index_columns(self):
        tree = ast.parse(ROUTER.read_text())

        guards = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", None) != "where":
                continue
            cols = _filtered_columns(node)
            # Only the unbound-ownership lookup tests organization_id for NULL.
            if "organization_id" in cols and "is_" in ast.dump(node):
                guards.append((node.lineno, cols))

        self.assertTrue(
            guards,
            "no unbound-ownership lookup found. It tests organization_id.is_(None) and "
            "cluster_id; if it was renamed or removed, update this test rather than deleting it.",
        )

        for lineno, cols in guards:
            self.assertEqual(
                cols,
                EXPECTED,
                f"cluster_registration.py:{lineno} filters on {sorted(cols)} while "
                f"uq_org_cluster_null covers {sorted(EXPECTED)}. Filtering on more columns "
                "makes the guard miss a conflicting row and the INSERT violates the index; "
                "filtering on fewer makes it refuse rows it should accept. The guard's "
                "columns and the constraint's columns have to be the same set.",
            )


if __name__ == "__main__":
    unittest.main()
