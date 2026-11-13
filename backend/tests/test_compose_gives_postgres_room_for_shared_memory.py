"""Postgres gets a /dev/shm large enough for every pooled connection's shared memory.

Docker gives a container 64 MiB of /dev/shm. Postgres keeps a session segment (192 KiB, often grown
by a 1 MiB segment) for the life of every connection that has run a parallel query, and parallel
plans appear once a table passes min_parallel_table_scan_size (8 MB), which cluster_log,
metrics_snapshots and task_journal do on a working deployment. Four backend workers hold about 80
pooled connections, so the default filled up and every query that needed shared memory failed with
"could not resize shared memory segment ... No space left on device", answered as HTTP 500.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.prod.yml")
_VAR = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")
_SIZE = re.compile(r"^(\d+)\s*([kmg]?)b?$", re.IGNORECASE)
_UNIT = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}

# Postgres max_connections (100, not overridden) times the measured per-connection footprint
# (192 KiB + 1 MiB), plus room for the segments of parallel queries running at the same time.
NEEDED = 256 * 1024**2


def _bytes(value: str) -> int:
    match = _SIZE.match(value.strip())
    assert match, f"shm_size '{value}' is not a byte size"
    return int(match.group(1)) * _UNIT[match.group(2).lower()]


def _shm_size(compose: str) -> str:
    service = yaml.safe_load((ROOT / compose).read_text())["services"]["postgres"]
    assert "shm_size" in service, f"{compose}: postgres runs with Docker's 64 MiB /dev/shm"
    # An unset override falls back to the default written in the file.
    return _VAR.sub(lambda m: m.group(2) or "", str(service["shm_size"]))


@pytest.mark.parametrize("compose", COMPOSE_FILES)
def test_postgres_has_room_for_every_pooled_connection(compose):
    assert _bytes(_shm_size(compose)) >= NEEDED


@pytest.mark.parametrize("value, expected", [("256mb", NEEDED), ("1g", 1024**3), ("268435456", NEEDED)])
def test_the_size_reads_the_way_docker_reads_it(value, expected):
    assert _bytes(value) == expected
