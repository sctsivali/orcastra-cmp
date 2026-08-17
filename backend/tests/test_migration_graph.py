"""Structural guards on the Alembic revision graph. No database is touched.

Two whole classes of production incident are covered here.

The first is ordering. `002_org_external_source` ALTERs `organizations`, which
`002_rbac_orgs` creates, and for a while it declared no dependency on it. With one head,
Alembic takes the branch order from the tuple in `009_merge_org_heads`, so the ALTER ran
first and every fresh install died on `relation "organizations" does not exist`.

The second is drift. `user_preferences` lived in the ORM for months with no migration and
only existed in production because startup ran `Base.metadata.create_all`. Now that
`create_all` is retired, a model without a migration means a table that never gets
created, so the parity check below is load-bearing rather than cosmetic.
"""

import ast
import os
import unittest
from pathlib import Path

# app.core.database raises at import time when DATABASE_URL is unset, and this module
# imports it to read Base.metadata. conftest.py sets the same placeholder, but only
# pytest loads conftest, and these tests must also run under `python -m unittest`.
# Nothing here opens a connection, so the value only has to parse.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_DIR = Path(__file__).resolve().parents[1]
VERSIONS_DIR = BACKEND_DIR / "alembic" / "versions"


def _script_directory() -> ScriptDirectory:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return ScriptDirectory.from_config(cfg)


def _created_tables() -> set:
    """Every table name passed to op.create_table across all revisions, read statically."""
    tables = set()
    for path in VERSIONS_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "create_table"):
                continue
            if node.args and isinstance(node.args[0], ast.Constant):
                tables.add(node.args[0].value)
    return tables


def _model_tables() -> set:
    from app.core.database import Base
    from app.models import db_models  # noqa: F401  (registers the models on Base)

    return set(Base.metadata.tables)


class RevisionGraphTests(unittest.TestCase):
    def test_graph_has_exactly_one_head(self) -> None:
        heads = _script_directory().get_heads()
        self.assertEqual(len(heads), 1, f"expected a single head, got {heads}")

    def test_every_revision_is_reachable_from_head(self) -> None:
        script = _script_directory()
        head = script.get_heads()[0]
        reachable = {rev.revision for rev in script.iterate_revisions(head, "base")}
        allrevs = {rev.revision for rev in script.walk_revisions()}
        self.assertEqual(allrevs - reachable, set(), "orphaned revisions are unreachable on upgrade")

    def test_org_external_source_depends_on_the_revision_that_creates_organizations(self) -> None:
        # The regression guard. 002_org_external_source only ALTERs `organizations`;
        # 002_rbac_orgs creates it. Without this edge, branch order decides whether a
        # fresh install works, and it decided wrong.
        script = _script_directory()
        ancestors = {rev.revision for rev in script.iterate_revisions("002_org_external_source", "base")}
        self.assertIn(
            "002_rbac_orgs",
            ancestors,
            "002_org_external_source must descend from 002_rbac_orgs, which CREATEs the "
            "organizations table it ALTERs",
        )

    def test_merge_node_still_merges_both_branches(self) -> None:
        merge = _script_directory().get_revision("009_merge_org_heads")
        self.assertEqual(
            set(merge.down_revision),
            {"008_image_upload_sessions", "002_org_external_source"},
        )


class SchemaParityTests(unittest.TestCase):
    def test_every_model_has_a_migration_that_creates_its_table(self) -> None:
        missing = _model_tables() - _created_tables()
        self.assertEqual(
            missing,
            set(),
            f"models with no CREATE TABLE in any migration: {sorted(missing)}. "
            "Startup no longer runs create_all, so these tables would never exist.",
        )

    def test_no_migration_creates_a_table_the_models_do_not_declare(self) -> None:
        orphaned = _created_tables() - _model_tables()
        self.assertEqual(
            orphaned,
            set(),
            f"migrations create tables no model declares: {sorted(orphaned)}",
        )


if __name__ == "__main__":
    unittest.main()
