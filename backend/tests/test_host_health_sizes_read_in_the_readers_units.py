"""Host Health size facts are collected once and read by many: each reader gets the sizes in their
own units, while the stored report keeps the byte counts and a decimal reading."""

from __future__ import annotations

from types import SimpleNamespace

from app.core.units import reset_byte_units, use_byte_units
from app.services.host_health.checks import cache, storage
from app.services.host_health.context import CheckContext
from app.services.host_health.models import HostHealthReport, localized

GiB = 1024**3


def _ctx(**settings) -> CheckContext:
    return CheckContext(settings=SimpleNamespace(**settings), store=None, now=1_700_000_000.0)


def _storage(monkeypatch):
    fs = SimpleNamespace(device="/dev/sda1", fstype="ext4", percent=50.0, free=10 * GiB)
    monkeypatch.setattr("app.services.system_resources.host_filesystems", lambda: [fs])
    monkeypatch.setattr("app.services.lxd.cert_files.describe_usage",
                        lambda: {"available": True, "percent_used": 1.0, "total_mib": 256.0, "degraded": False})
    return storage.check(_ctx())


def _report(*checks) -> HostHealthReport:
    return HostHealthReport(checks=list(checks), overall="ok", attention=0, collected_at=1.0,
                            source="cached", store="shared", interval_seconds=60)


def _facts(report: HostHealthReport) -> dict:
    return {f.label: f.value for check in report.checks for f in check.facts}


def test_a_collected_fact_is_decimal_even_inside_a_binary_request(monkeypatch) -> None:
    token = use_byte_units("binary")
    try:
        result = _storage(monkeypatch)
    finally:
        reset_byte_units(token)
    facts = {f.label: f for f in result.facts}
    assert facts["Host Disk /dev/sda1"].value == "50.0% used, 10.74 GB free"
    assert facts["Host Disk /dev/sda1"].sizes == {"free": 10 * GiB}
    assert facts["Certificate Scratch Space"].value == "1.0% used of 268.44 MB"


def test_each_reader_gets_their_own_units(monkeypatch) -> None:
    stored = _report(_storage(monkeypatch))

    token = use_byte_units("binary")
    try:
        binary = localized(stored)
    finally:
        reset_byte_units(token)
    decimal = localized(stored)

    assert _facts(binary)["Host Disk /dev/sda1"] == "50.0% used, 10 GiB free"
    assert _facts(binary)["Certificate Scratch Space"] == "1.0% used of 256 MiB"
    assert _facts(decimal)["Host Disk /dev/sda1"] == "50.0% used, 10.74 GB free"
    assert _facts(stored)["Host Disk /dev/sda1"] == "50.0% used, 10.74 GB free", "the stored report is untouched"


def test_the_served_report_carries_no_template(monkeypatch) -> None:
    served = localized(_report(_storage(monkeypatch))).model_dump()
    for check in served["checks"]:
        for fact in check["facts"]:
            assert fact.get("template") is None and fact.get("sizes") is None


def test_redis_memory_reads_in_the_readers_units(monkeypatch) -> None:
    class Client:
        def ping(self):
            return True

        def info(self, section):
            return {"used_memory": 512 * 1024**2, "maxmemory": GiB} if section == "memory" else {"redis_version": "8.0"}

    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: SimpleNamespace(redis_client=Client()))
    stored = _report(cache.check(_ctx(redis_url="redis://redis:6379/0")))
    assert _facts(stored)["Memory Used"] == "536.87 MB of 1.07 GB"
    token = use_byte_units("binary")
    try:
        assert _facts(localized(stored))["Memory Used"] == "512 MiB of 1 GiB"
    finally:
        reset_byte_units(token)


def test_redis_without_a_limit_or_a_reading(monkeypatch) -> None:
    class Client:
        def ping(self):
            return True

        def info(self, section):
            return {"used_memory": None, "maxmemory": 0} if section == "memory" else {"redis_version": "8.0"}

    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: SimpleNamespace(redis_client=Client()))
    stored = _report(cache.check(_ctx(redis_url="redis://redis:6379/0")))
    assert _facts(localized(stored))["Memory Used"] == "unknown (no limit set)"


def test_a_report_stored_by_an_older_build_reads_as_it_was() -> None:
    old = HostHealthReport.model_validate({
        "checks": [{"id": "storage", "label": "Storage", "status": "ok", "summary": "ok", "checked_at": 1.0,
                    "facts": [{"label": "Host Disk /dev/sda1", "value": "50.0% used, 10.74 GB free"}]}],
        "overall": "ok", "attention": 0, "collected_at": 1.0, "source": "cached", "store": "shared",
        "interval_seconds": 60,
    })
    token = use_byte_units("binary")
    try:
        assert _facts(localized(old))["Host Disk /dev/sda1"] == "50.0% used, 10.74 GB free"
    finally:
        reset_byte_units(token)
