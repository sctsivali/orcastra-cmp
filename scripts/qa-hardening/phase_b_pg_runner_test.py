"""Offline PostgreSQL schema-contract serialization and query-boundary tests."""

from __future__ import annotations

import copy
import hashlib
import json
import socket
import subprocess
import sys
import types
import unittest
from unittest import mock

import phase_b_pg_runner as runner

URL = "postgresql+asyncpg://synthetic-only/"
COLUMNS = [
    ("cluster_log", "id", "integer", "int4", "NO", "nextval('cluster_log_id_seq'::regclass)",
     None, 32, 0, "NO", None, "NEVER", None),
    ("task_journal", "id", "uuid", "uuid", "NO", None, None, None, None, "NO", None, "NEVER", None),
]
CONSTRAINTS = [
    ("task_journal", "task_journal_pkey", b"p", "PRIMARY KEY (id)", True, False, False),
    ("cluster_log", "cluster_log_task_fkey", b"f", "FOREIGN KEY (task_id) REFERENCES task_journal(id)", True, False, False),
]
INDEXES = [("task_journal", "task_journal_pkey", "CREATE UNIQUE INDEX task_journal_pkey ON public.task_journal USING btree (id)")]
SEQUENCES = [("cluster_log_id_seq", "bigint", 1, 1, 9223372036854775807, 1, False, 1)]


def digest(value):
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


class CatalogConnection:
    """Model asyncpg's internal char bytes versus a PostgreSQL text result."""

    def __init__(self):
        self.columns = copy.deepcopy(COLUMNS)
        self.constraints = copy.deepcopy(CONSTRAINTS)
        self.indexes = copy.deepcopy(INDEXES)
        self.sequences = copy.deepcopy(SEQUENCES)
        self.all_objects_present = True
        self.queries = []
        self.closed = False
        self.fail_constraints = False

    async def fetchval(self, query, *arguments):
        self.queries.append((query, arguments))
        if query == "SELECT version_num FROM alembic_version":
            return runner.HEAD
        if query == "SELECT to_regclass($1)::text":
            if len(arguments) != 1 or arguments[0] not in {"public." + name for name in runner.OBJECTS}:
                raise AssertionError("Only the reviewed public journal objects may be queried")
            return arguments[0] if self.all_objects_present else None
        if query.startswith("SELECT count(*) FROM ") and query.removeprefix("SELECT count(*) FROM ") in runner.OBJECTS[:3]:
            return 0
        raise AssertionError("An unreviewed catalog value query is refused")

    async def fetch(self, query):
        self.queries.append((query, ()))
        query = " ".join(query.split())
        if "FROM information_schema.columns" in query:
            if "WHERE table_schema='public'" not in query:
                raise AssertionError("The public-only columns boundary changed")
            return self.columns if "ORDER BY table_name, ordinal_position" in query else list(reversed(self.columns))
        if "FROM pg_constraint constraint_row" in query:
            if "WHERE namespace.nspname='public'" not in query:
                raise AssertionError("The public-only constraint boundary changed")
            if self.fail_constraints:
                raise RuntimeError("Synthetic catalog read failed")
            rows = self.constraints
            if "constraint_row.contype::text" in query:
                rows = [(*row[:2], row[2].decode("ascii"), *row[3:]) for row in rows]
            if "ORDER BY relation.relname, constraint_row.conname" in query:
                rows = sorted(rows, key=lambda row: row[:2])
            return rows
        if "FROM pg_indexes" in query:
            if "WHERE schemaname='public'" not in query or "ORDER BY tablename, indexname" not in query:
                raise AssertionError("The public-only deterministic index boundary changed")
            return self.indexes
        if "FROM pg_sequences" in query:
            if "WHERE schemaname='public'" not in query or "ORDER BY sequencename" not in query:
                raise AssertionError("The public-only deterministic sequence boundary changed")
            return self.sequences
        raise AssertionError("An unreviewed catalog row query is refused")

    async def close(self):
        self.closed = True


class SchemaSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = CatalogConnection()

        async def connect(url, **arguments):
            if url != "postgresql://synthetic-only/" or arguments != {"timeout": 15}:
                raise AssertionError("The fixed synthetic connection and existing timeout are required")
            return self.connection

        self.driver = mock.patch.dict(sys.modules, {"asyncpg": types.SimpleNamespace(connect=connect)})
        self.driver.start(); self.addCleanup(self.driver.stop)
        for owner, name in ((socket.socket, "connect"), (subprocess, "Popen")):
            refusal = mock.patch.object(owner, name, side_effect=AssertionError("No actual DB connection or subprocess in offline tests"))
            refusal.start(); self.addCleanup(refusal.stop)

    def expected_contract(self):
        return {"columns": [list(row) for row in self.connection.columns],
                "constraints": [[*row[:2], row[2].decode("ascii"), *row[3:]]
                                for row in sorted(self.connection.constraints, key=lambda row: row[:2])],
                "indexes": [list(row) for row in self.connection.indexes],
                "sequences": [list(row) for row in self.connection.sequences]}

    async def test_untyped_internal_char_counterexample_remains_bytes_and_std_json_refuses_it(self):
        rows = await self.connection.fetch("""SELECT relation.relname, constraint_row.conname,
            constraint_row.contype, pg_get_constraintdef(constraint_row.oid, true),
            constraint_row.convalidated, constraint_row.condeferrable, constraint_row.condeferred
            FROM pg_constraint constraint_row WHERE namespace.nspname='public'
            ORDER BY relation.relname, constraint_row.conname""")
        self.assertIs(type(rows[0][2]), bytes)
        with self.assertRaises(TypeError):
            json.dumps({"constraints": [list(row) for row in rows]})

    async def test_schema_snapshot_casts_internal_char_to_text_and_keeps_the_full_public_contract(self):
        result = await runner.schema_snapshot(URL)
        self.assertEqual(result, {
            "version": runner.HEAD, "objects": {name: True for name in runner.OBJECTS},
            "journal_row_counts": {name: 0 for name in runner.OBJECTS[:3]},
            "column_schema_sha256": digest([list(row) for row in COLUMNS]),
            "schema_contract_sha256": digest(self.expected_contract()),
        })
        self.assertTrue(self.connection.closed)
        self.assertIs(type(self.expected_contract()["constraints"][0][2]), str)
        self.assertIs(type(self.expected_contract()["constraints"][0][4]), bool)
        self.assertEqual(self.expected_contract()["sequences"][0][4], 9223372036854775807)

    async def test_server_order_is_stable_across_driver_row_input_order(self):
        first = await runner.schema_snapshot(URL)
        self.connection.closed = False
        self.connection.constraints.reverse()
        second = await runner.schema_snapshot(URL)
        self.assertEqual(first, second)
        self.assertTrue(self.connection.closed)

    async def test_constraint_kind_definition_and_security_flags_each_change_contract_hash(self):
        original = await runner.schema_snapshot(URL)
        for position, value in ((2, b"u"), (3, "UNIQUE (id)"), (4, False), (5, True), (6, True)):
            self.connection.constraints = copy.deepcopy(CONSTRAINTS)
            row = list(self.connection.constraints[0]); row[position] = value
            self.connection.constraints[0] = tuple(row)
            with self.subTest(position=position):
                changed = await runner.schema_snapshot(URL)
                self.assertNotEqual(changed["schema_contract_sha256"], original["schema_contract_sha256"])
                self.assertEqual(changed["column_schema_sha256"], original["column_schema_sha256"])

    async def test_unrelated_unserializable_values_are_not_hidden_by_generic_coercion(self):
        self.connection.indexes = [("task_journal", "task_journal_pkey", b"unreviewed bytes")]
        with self.assertRaises(TypeError):
            await runner.schema_snapshot(URL)
        self.assertTrue(self.connection.closed)

    async def test_database_read_failure_closes_connection_and_does_not_retry(self):
        self.connection.fail_constraints = True
        with self.assertRaises(RuntimeError):
            await runner.schema_snapshot(URL)
        self.assertTrue(self.connection.closed)
        self.assertEqual(sum("FROM pg_constraint" in query for query, _ in self.connection.queries), 1)

    async def test_missing_journal_objects_preserve_absence_without_counting_nonexistent_tables(self):
        self.connection.all_objects_present = False
        result = await runner.schema_snapshot(URL)
        self.assertEqual(result["objects"], {name: False for name in runner.OBJECTS})
        self.assertEqual(result["journal_row_counts"], {})
        self.assertFalse(any("SELECT count(*)" in query for query, _ in self.connection.queries))
        self.assertTrue(self.connection.closed)


if __name__ == "__main__":
    unittest.main()
