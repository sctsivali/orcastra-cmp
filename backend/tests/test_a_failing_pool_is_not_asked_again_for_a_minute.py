"""A storage pool whose resources read fails is not asked again for a minute.

A broken ZFS pool can make LXD fail its resources read after seconds, every time, and the node
status, the regions overview and the pool resources list each ask for it on every build. The
failure is remembered per worker for FAILURE_TTL_S; a success is never cached, so pool usage stays
live.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.lxd import pool_resource_failures as failures

APP = Path(__file__).resolve().parents[1] / "app"


def _boom():
    raise RuntimeError("Failed to run: zfs get -H -p -o value available central-pool")


def test_a_failure_is_remembered_and_the_next_read_fails_at_once():
    with pytest.raises(RuntimeError):
        failures.read("blizzard", "central-pool", _boom)
    calls = []
    with pytest.raises(failures.PoolResourcesUnavailable):
        failures.read("blizzard", "central-pool", lambda: calls.append(1))
    assert calls == []


def test_the_memory_lapses(monkeypatch):
    with pytest.raises(RuntimeError):
        failures.read("blizzard", "central-pool", _boom)
    real = failures.time.monotonic
    monkeypatch.setattr(failures.time, "monotonic", lambda: real() + failures.FAILURE_TTL_S + 1)
    assert failures.read("blizzard", "central-pool", lambda: "space") == "space"


def test_a_success_is_never_cached_and_clears_the_failure():
    reads = []
    assert failures.read("nuc2", "local", lambda: reads.append(1) or "a") == "a"
    assert failures.read("nuc2", "local", lambda: reads.append(1) or "b") == "b"
    assert reads == [1, 1]


def test_other_pools_and_clusters_are_unaffected():
    with pytest.raises(RuntimeError):
        failures.read("blizzard", "central-pool", _boom)
    assert failures.read("blizzard", "default", lambda: "ok") == "ok"
    assert failures.read("nuc2", "central-pool", lambda: "ok") == "ok"
    failures.forget("blizzard")
    assert failures.read("blizzard", "central-pool", lambda: "ok") == "ok"


def test_the_message_names_no_endpoint():
    with pytest.raises(RuntimeError):
        failures.read("blizzard", "central-pool", _boom)
    with pytest.raises(failures.PoolResourcesUnavailable) as caught:
        failures.read("blizzard", "central-pool", _boom)
    assert "zfs" not in str(caught.value) and "RuntimeError" in str(caught.value)


@pytest.mark.parametrize(
    "path", ["services/lxd/resources.py", "routers/regions.py", "routers/storage_pools.py"]
)
def test_every_fleet_reader_goes_through_the_memory(path):
    source = (APP / path).read_text()
    assert "pool_resource_failures.read(" in source
    # A direct pool resources read in a fleet loop would pay the broken pool's delay again.
    direct = re.findall(r"^\s*\w+ = (?:pool|client\.api\.storage_pools\[[^\]]+\])\.resources\.get\(\)", source, re.M)
    if path == "routers/storage_pools.py":
        # The single-pool route (GET /{pool_name}/resources) reads its one pool directly.
        assert len(direct) <= 1, direct
    else:
        assert direct == [], direct
