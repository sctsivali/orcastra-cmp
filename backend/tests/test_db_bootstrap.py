"""Unit tests for the legacy-database reconciliation in app/core/db_bootstrap.py.

`_plan` decides whether a database gets stamped, left alone, or refused, and getting
that wrong either corrupts a partner's data or bricks their upgrade. It is a pure
function over a set of table names, so it is tested directly with no database.
"""

import unittest

from app.core.db_bootstrap import (
    _POST_RC2_TABLES,
    _RC2_CORE_TABLES,
    _asyncpg_dsn,
    _plan,
)


class PlanTests(unittest.TestCase):
    def test_stamped_database_is_left_alone(self) -> None:
        self.assertEqual(_plan(set(_RC2_CORE_TABLES), stamped=True), "managed")

    def test_stamped_wins_even_when_the_schema_looks_unfamiliar(self) -> None:
        self.assertEqual(_plan({"something_else"}, stamped=True), "managed")

    def test_empty_database_is_built_from_base(self) -> None:
        self.assertEqual(_plan(set(), stamped=False), "fresh")

    def test_database_with_only_unrelated_tables_is_treated_as_fresh(self) -> None:
        self.assertEqual(_plan({"spatial_ref_sys", "pg_stat_statements"}, stamped=False), "fresh")

    def test_complete_rc2_schema_is_stamped(self) -> None:
        self.assertEqual(_plan(set(_RC2_CORE_TABLES), stamped=False), "rc2")

    def test_rc2_detection_ignores_extra_unknown_tables(self) -> None:
        tables = set(_RC2_CORE_TABLES) | {"some_operator_scratch_table"}
        self.assertEqual(_plan(tables, stamped=False), "rc2")

    def test_partial_rc2_schema_is_refused_and_names_what_is_missing(self) -> None:
        # A single core table is NOT enough. Stamping this would claim 001-007 had run,
        # and the upgrade would then die at 010 against a table that never existed.
        outcome = _plan({"organizations"}, stamped=False)
        self.assertIsInstance(outcome, tuple)
        kind, missing = outcome
        self.assertEqual(kind, "partial")
        self.assertNotIn("organizations", missing)
        for table in _RC2_CORE_TABLES:
            if table != "organizations":
                self.assertIn(table, missing)

    def test_every_single_core_table_alone_is_refused(self) -> None:
        for table in _RC2_CORE_TABLES:
            with self.subTest(table=table):
                kind, _missing = _plan({table}, stamped=False)
                self.assertEqual(kind, "partial")

    def test_schema_newer_than_rc2_is_refused_and_names_the_offenders(self) -> None:
        tables = set(_RC2_CORE_TABLES) | {"rebuild_jobs", "iso_upload_sessions"}
        kind, found = _plan(tables, stamped=False)
        self.assertEqual(kind, "unknown")
        self.assertIn("rebuild_jobs", found)
        self.assertIn("iso_upload_sessions", found)

    def test_post_rc2_evidence_outranks_an_incomplete_core_set(self) -> None:
        # Refusing as "unknown" is the safer message here: the schema is clearly past RC2.
        kind, found = _plan({"organizations", "rebuild_jobs"}, stamped=False)
        self.assertEqual(kind, "unknown")
        self.assertEqual(found, ["rebuild_jobs"])

    def test_post_rc2_table_list_is_disjoint_from_the_core_list(self) -> None:
        self.assertEqual(set(_RC2_CORE_TABLES) & set(_POST_RC2_TABLES), set())


class AsyncpgDsnTests(unittest.TestCase):
    def test_strips_the_sqlalchemy_driver_suffix(self) -> None:
        self.assertEqual(
            _asyncpg_dsn("postgresql+asyncpg://u:p@h:5432/db"),
            "postgresql://u:p@h:5432/db",
        )

    def test_normalises_the_short_postgres_scheme(self) -> None:
        self.assertEqual(_asyncpg_dsn("postgres://u:p@h:5432/db"), "postgresql://u:p@h:5432/db")

    def test_leaves_a_plain_dsn_untouched(self) -> None:
        self.assertEqual(_asyncpg_dsn("postgresql://u:p@h:5432/db"), "postgresql://u:p@h:5432/db")

    def test_preserves_query_parameters(self) -> None:
        self.assertEqual(
            _asyncpg_dsn("postgresql+asyncpg://u:p@h:5432/db?sslmode=require"),
            "postgresql://u:p@h:5432/db?sslmode=require",
        )

    def test_only_rewrites_the_scheme_not_the_path(self) -> None:
        # A database literally named "postgres" must not be rewritten.
        self.assertEqual(_asyncpg_dsn("postgres://u:p@h:5432/postgres"), "postgresql://u:p@h:5432/postgres")


if __name__ == "__main__":
    unittest.main()
