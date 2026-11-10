"""IPAM reads one cluster's projects, networks and instances side by side.

The three reads overlap on their own pool, so the join costs the slowest read rather than their
sum, and it returns exactly what a serial read would, with losses named on the NodeRead.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from app.routers import ipam
from app.services.lxd.fanout import NodeRead


def _slow(value, delay=0.3):
    def read(*args, **kwargs):
        time.sleep(delay)
        return value() if callable(value) else value

    return read


def test_the_three_reads_overlap_and_join_as_before(monkeypatch):
    record = SimpleNamespace(project="default", name="web")
    monkeypatch.setattr(ipam, "list_project_configs", _slow({"default": {}, "tenant-a": {}}))
    monkeypatch.setattr(ipam, "list_across_projects", _slow(([{"project": "default", "name": "br0"}], True)))
    monkeypatch.setattr(ipam, "list_instance_records", _slow(([record], True)))
    read = NodeRead()
    started = time.monotonic()
    configs, networks, records, instances_ok = ipam._read_join_sources(object(), "central-1", read)
    elapsed = time.monotonic() - started
    assert elapsed < 0.6, f"the reads did not overlap: {elapsed:.2f} s"
    assert set(configs) == {"default", "tenant-a"}
    assert networks == {("default", "br0"): {"project": "default", "name": "br0"}}
    assert records == {("default", "web"): record}
    assert instances_ok is True and read.complete


def test_a_failed_read_is_named_and_the_others_still_join(monkeypatch):
    def boom(*args, **kwargs):
        raise TimeoutError("networks")

    monkeypatch.setattr(ipam, "list_project_configs", _slow({"default": {}}, 0))
    monkeypatch.setattr(ipam, "list_across_projects", boom)
    monkeypatch.setattr(ipam, "list_instance_records", _slow(([], False), 0))
    read = NodeRead()
    configs, networks, records, instances_ok = ipam._read_join_sources(object(), "central-1", read)
    assert networks == {} and configs == {"default": {}}
    assert instances_ok is False
    assert sorted(read.problems) == ["instances: partial read", "networks: TimeoutError"]
