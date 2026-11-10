"""Structural guards on the Alembic revision graph. No database is touched.

Ordering: `002_org_external_source` ALTERs `organizations`, which `002_rbac_orgs` creates.
Without that dependency Alembic takes the branch order from the tuple in `009_merge_org_heads`,
and a fresh install can fail on `relation "organizations" does not exist`.

Drift: startup does not run `Base.metadata.create_all`, so a model without a migration is a
table that never gets created. The parity check below catches that.
"""

import ast
import os
import unittest
from pathlib import Path

# app.core.database raises at import time when DATABASE_URL is unset, and this module
# imports it to read Base.metadata. conftest.py sets the same placeholder, but only
# pytest loads conftest, and these tests must also run under `python -m unittest`.
# Nothing here opens a connection, so the value only has to parse.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_DIR = Path(__file__).resolve().parents[1]
VERSIONS_DIR = BACKEND_DIR / "alembic" / "versions"


def _script_directory() -> ScriptDirectory:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return ScriptDirectory.from_config(cfg)


def _create_table_sites() -> list:
    """(revision file name, table name) for every op.create_table across all revisions, read statically."""
    sites = []
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "create_table"):
                continue
            if node.args and isinstance(node.args[0], ast.Constant):
                sites.append((path.name, node.args[0].value))
    return sites


def _created_tables() -> set:
    """Every table name passed to op.create_table across all revisions, read statically."""
    return {table for _name, table in _create_table_sites()}


def _model_modules() -> list:
    """Every module under app/models that declares a table.

    Discovered rather than named, so a model module added beside `db_models` cannot slip past
    the parity check with no migration.
    """
    import importlib

    modules = []
    for path in sorted((BACKEND_DIR / "app" / "models").glob("*.py")):
        if path.name == "__init__.py":
            continue
        module = importlib.import_module(f"app.models.{path.stem}")
        if any(
            isinstance(getattr(module, name, None), type)
            and hasattr(getattr(module, name), "__tablename__")
            for name in dir(module)
        ):
            modules.append(path.stem)
    return modules


def _model_tables() -> set:
    from app.core.database import Base

    _model_modules()  # Importing them registers the tables on Base.
    return set(Base.metadata.tables)


class RevisionGraphTests(unittest.TestCase):
    def test_revision_ids_fit_the_default_alembic_version_column(self) -> None:
        revisions = [revision.revision for revision in _script_directory().walk_revisions()]
        too_long = sorted(revision for revision in revisions if len(revision) > 32)
        self.assertEqual(
            too_long,
            [],
            "Alembic's default version_num column is VARCHAR(32); longer revision IDs fail "
            "only after their migration DDL runs on PostgreSQL.",
        )

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
        # 002_org_external_source only ALTERs `organizations`; 002_rbac_orgs creates it.
        # Without this edge, branch order decides whether a fresh install works.
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

    def test_the_owner_set_merge_joins_the_hotfix_revision(self) -> None:
        # A database from an earlier release line can sit at 015a, which branches from 015. This
        # merge is its only path to head, so both edges are pinned.
        script = _script_directory()
        self.assertEqual(
            set(script.get_revision("038_organization_owner_keys").down_revision),
            {"037_migration_unknown_phase", "015a_organization_owner_keys"},
        )
        self.assertEqual(
            script.get_revision("015a_organization_owner_keys").down_revision,
            "015_session_audit_bigint",
        )

    def test_only_015a_creates_the_owner_set(self) -> None:
        # A database from that line already ran 015a, so DDL in the merge, or a second
        # create_table of the same table, would fail its upgrade.
        merge = next(VERSIONS_DIR.glob("*_038_organization_owner_keys.py"))
        calls = [node for node in ast.walk(ast.parse(merge.read_text())) if isinstance(node, ast.Call)]
        self.assertEqual(calls, [], "038 is a merge revision; schema changes belong in a new revision")
        self.assertEqual(
            [name for name, table in _create_table_sites() if table == "organization_owner_keys"],
            ["20261009_130000_015a_organization_owner_keys.py"],
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


class MetadataRegistrationTests(unittest.TestCase):
    def test_alembic_env_imports_every_model_module(self) -> None:
        """A module Base.metadata never sees produces no autogenerate diff and no parity failure.

        `alembic/env.py` names each model module. A module it misses has tables autogenerate
        cannot see, so a later autogenerate run would offer a diff that drops them.
        """
        env = (BACKEND_DIR / "alembic" / "env.py").read_text()
        missing = [m for m in _model_modules() if f"import {m}" not in env]
        self.assertEqual(
            missing,
            [],
            f"alembic/env.py does not import these model modules: {missing}. "
            "Their tables are invisible to autogenerate.",
        )


if __name__ == "__main__":
    unittest.main()
