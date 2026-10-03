"""Run the explicit PostgreSQL contract inside a fresh restricted QA container."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import stat
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

LAB = "orcastra-qa-hardening"
HEAD = "037_migration_unknown_phase"
DOWNGRADE = "023_a_schedule_orcastra_owns"
DATABASES = {"journal": "orcastra_journal_test", "migration": "orcastra_migration_scratch"}
OBJECTS = ("task_journal", "cluster_log", "task_follower_clusters", "task_journal_seq")
SOURCE = Path("/source/backend")
EVIDENCE = Path("/evidence")


def regular_private(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise ValueError("A private regular gate file is required")
        body = source.read(limit + 1)
    if len(body) > limit:
        raise ValueError("The private gate file exceeds its bound")
    return body


def junit_summary(path: Path, expected_names: list[str]) -> dict:
    tree = ET.parse(path)
    cases = list(tree.getroot().iter("testcase"))
    names = [case.get("name", "") for case in cases]
    if len(names) != 14 or len(set(names)) != 14 or set(names) != set(expected_names):
        raise ValueError("The test report differs from the fourteen reviewed journal cases")
    counts = {name: sum(case.find(name) is not None for case in cases) for name in ("failure", "error", "skipped")}
    if any(sum(case.find(name) is not None for name in counts) > 1 for case in cases):
        raise ValueError("A test case has conflicting terminal outcomes")
    durations = [float(case.get("time", "0")) for case in cases]
    duration = sum(durations)
    if any(not math.isfinite(value) or value < 0 for value in durations) or not math.isfinite(duration):
        raise ValueError("A test duration is unavailable or invalid")
    return {"collected": len(cases), "passed": len(cases) - sum(counts.values()),
            **counts, "test_names": sorted(names), "duration_seconds": duration}


def run_command(arguments: list[str], environment: dict, name: str, timeout: int = 180) -> None:
    # important: command output may contain a driver URL. Keep it guest-private.
    fd = os.open(EVIDENCE / (name + "-private.log"), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as log:
        result = subprocess.run(arguments, cwd=SOURCE, env=environment, stdout=log,
                                stderr=subprocess.STDOUT, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError("An isolated PostgreSQL gate command failed; inspect its private guest artifact")


async def schema_snapshot(url: str) -> dict:
    import asyncpg
    connection = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://", 1), timeout=15)
    try:
        version = await connection.fetchval("SELECT version_num FROM alembic_version")
        objects = {name: await connection.fetchval("SELECT to_regclass($1)::text", "public." + name) is not None
                   for name in OBJECTS}
        rows = await connection.fetch("""SELECT table_name, column_name, data_type, udt_name, is_nullable,
            column_default, character_maximum_length, numeric_precision, numeric_scale,
            is_identity, identity_generation, is_generated, generation_expression
            FROM information_schema.columns WHERE table_schema='public'
            ORDER BY table_name, ordinal_position""")
        schema = [list(row) for row in rows]
        constraints = await connection.fetch("""SELECT relation.relname, constraint_row.conname,
            constraint_row.contype::text, pg_get_constraintdef(constraint_row.oid, true),
            constraint_row.convalidated, constraint_row.condeferrable, constraint_row.condeferred
            FROM pg_constraint constraint_row
            JOIN pg_class relation ON relation.oid=constraint_row.conrelid
            JOIN pg_namespace namespace ON namespace.oid=relation.relnamespace
            WHERE namespace.nspname='public'
            ORDER BY relation.relname, constraint_row.conname""")
        indexes = await connection.fetch("""SELECT tablename, indexname, indexdef FROM pg_indexes
            WHERE schemaname='public' ORDER BY tablename, indexname""")
        sequences = await connection.fetch("""SELECT sequencename, data_type, start_value, min_value,
            max_value, increment_by, cycle, cache_size FROM pg_sequences
            WHERE schemaname='public' ORDER BY sequencename""")
        contract = {"columns": schema, "constraints": [list(row) for row in constraints],
                    "indexes": [list(row) for row in indexes], "sequences": [list(row) for row in sequences]}
        counts = {}
        if all(objects.values()):
            for table in OBJECTS[:3]:
                counts[table] = await connection.fetchval("SELECT count(*) FROM " + table)
        return {"version": version, "objects": objects, "journal_row_counts": counts,
                "column_schema_sha256": hashlib.sha256(json.dumps(schema, separators=(",", ":")).encode()).hexdigest(),
                "schema_contract_sha256": hashlib.sha256(json.dumps(contract, separators=(",", ":")).encode()).hexdigest()}
    finally:
        await connection.close()


async def require_empty_database(url: str, expected_name: str) -> None:
    import asyncpg
    connection = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://", 1), timeout=15)
    try:
        name = await connection.fetchval("SELECT current_database()")
        objects = await connection.fetchval("""SELECT count(*) FROM pg_class c
            JOIN pg_namespace n ON c.relnamespace=n.oid
            WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S','f')""")
        if name != expected_name or objects != 0:
            raise ValueError("Refusing to adopt a populated or differently named database")
    finally:
        await connection.close()


def require_schema(snapshot: dict, version: str, objects_present: bool) -> None:
    if (snapshot["version"] != version or set(snapshot["objects"]) != set(OBJECTS)
        or any(value is not objects_present for value in snapshot["objects"].values())):
        raise ValueError("The migration version or journal object state differs from the expected boundary")


def run(plan: dict, approved_digest: str | None = None) -> dict:
    gate = plan["gate_id"]
    document = dict(plan)
    recorded_digest = document.pop("proposal_digest", None)
    canonical = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if (plan.get("phase") != "B-PG" or not plan.get("ready_for_execution")
        or not approved_digest or recorded_digest != approved_digest or canonical != approved_digest
        or uuid.UUID(gate).version != 4 or str(uuid.UUID(gate)) != gate
        or plan["databases"] != DATABASES or plan["migration"]["head"] != HEAD
        or plan["migration"]["downgrade"] != DOWNGRADE):
        raise ValueError("Unexpected scratch database gate contract")
    if regular_private(Path("/run/qa-gate/owner"), 128).strip() != gate.encode():
        raise ValueError("The fresh runner ownership marker differs")
    secret = regular_private(Path("/run/qa-gate/password"), 256).decode().strip()
    if len(secret) < 32 or not all(c in "0123456789abcdef" for c in secret):
        raise ValueError("A fresh hexadecimal gate password is required")
    user = "orcastra_qa_journal"
    host = LAB + "-pg-db-" + gate[:8]
    if plan["database_user"] != user or plan["containers"]["postgres"] != host:
        raise ValueError("Only the exact owned database container is supported")
    urls = {key: f"postgresql+asyncpg://{user}:{quote(secret, safe='')}@{host}:5432/{name}"
            for key, name in DATABASES.items()}
    environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(SOURCE),
                   "PYTHONUNBUFFERED": "1", "DATABASE_URL": urls["journal"], "JOURNAL_TEST_DATABASE_URL": urls["journal"]}
    for key, url in urls.items():
        asyncio.run(require_empty_database(url, DATABASES[key]))
    run_command([sys.executable, "-m", "alembic", "upgrade", "head"], environment, "journal-upgrade")
    journal_before = asyncio.run(schema_snapshot(urls["journal"]))
    require_schema(journal_before, HEAD, True)
    run_command([sys.executable, "-m", "pytest", "-q", "--tb=short", "--disable-warnings", "-p", "no:cacheprovider",
                 "--junitxml=" + str(EVIDENCE / "journal-tests.xml"),
                 "tests/test_the_journal_sql_round_trips_on_postgres.py"], environment, "journal-tests", timeout=300)
    summary = junit_summary(EVIDENCE / "journal-tests.xml", plan["source"]["inventory"]["tests"])
    if summary["passed"] != 14 or any(summary[name] for name in ("failure", "error", "skipped")):
        raise ValueError("All fourteen journal tests must run and pass")
    journal_after = asyncio.run(schema_snapshot(urls["journal"]))
    require_schema(journal_after, HEAD, True)
    if (set(journal_after["journal_row_counts"]) != set(OBJECTS[:3])
        or any(type(value) is not int or value != 0 for value in journal_after["journal_row_counts"].values())):
        raise ValueError("The journal fixture left rows outside its declared final state")
    environment["DATABASE_URL"] = urls["migration"]
    environment.pop("JOURNAL_TEST_DATABASE_URL")
    run_command([sys.executable, "-m", "alembic", "upgrade", "head"], environment, "migration-upgrade")
    migration_before = asyncio.run(schema_snapshot(urls["migration"]))
    require_schema(migration_before, HEAD, True)
    run_command([sys.executable, "-m", "alembic", "downgrade", DOWNGRADE], environment, "migration-downgrade")
    migration_down = asyncio.run(schema_snapshot(urls["migration"]))
    require_schema(migration_down, DOWNGRADE, False)
    run_command([sys.executable, "-m", "alembic", "upgrade", "head"], environment, "migration-reupgrade")
    migration_after = asyncio.run(schema_snapshot(urls["migration"]))
    require_schema(migration_after, HEAD, True)
    if (migration_before["column_schema_sha256"] != migration_after["column_schema_sha256"]
        or migration_after["column_schema_sha256"] != journal_before["column_schema_sha256"]
        or migration_before["schema_contract_sha256"] != migration_after["schema_contract_sha256"]
        or migration_after["schema_contract_sha256"] != journal_before["schema_contract_sha256"]):
        raise ValueError("The restored public schema contract differs from the fresh migrated head")
    return {"phase": "B-PG", "gate_id": gate, "status": "journal_and_migration_verified", "tests": summary,
            "journal_before": journal_before, "journal_after": journal_after,
            "migration_before": migration_before, "migration_down": migration_down, "migration_after": migration_after,
            "scope_limit": "Public column/default, constraint, index and sequence configuration equality plus journal objects; excludes physical state, sequence current values, grants and data restoration"}


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--approved-digest", required=True)
    args = parser.parse_args()
    try:
        result = run(json.loads(regular_private(args.proposal, 4 * 1024 * 1024)), args.approved_digest)
        print(json.dumps(result, sort_keys=True))
    # important: driver and command exceptions can include private credentials.
    except Exception as error:  # noqa: BLE001
        print(json.dumps({"status": "stopped", "failure_type": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
