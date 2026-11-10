"""CPU is reported as a share of the instance's effective vCPUs, never as "percent of one core".

A fully busy instance reads 100% whatever its vCPU count, matching the "across all vCPUs" label;
busy vCPUs are reported beside the percent.
"""

from types import SimpleNamespace

import pytest

from app.services.lxd import instance_metrics as metrics_module
from app.services.lxd.cpu_allocation import effective_vcpus
from app.services.lxd.instance_metrics import read_instance_metrics


def _instance(*, kind="container", limits=None, expanded=None, cpu_ns, location="none"):
    config = {"volatile.uuid": "u-1"}
    if limits is not None:
        config["limits.cpu"] = limits
    state = SimpleNamespace(
        cpu={"usage": cpu_ns},
        memory={"usage": 1, "total": 2},
        disk={},
        network={},
        processes=12,
        pid=777,
        started_at=None,
    )
    return SimpleNamespace(
        name="fixture",
        status="Running",
        status_code=103,
        type=kind,
        config=config,
        expanded_config=expanded,
        last_used_at="2026-10-07T00:00:00Z",
        location=location,
        state=lambda: state,
    )


def _two_samples(monkeypatch, first, second, *, client=None):
    ticks = [100.0, 100.0, 110.0, 110.0]
    monkeypatch.setattr(metrics_module, "_now", lambda: ticks.pop(0))
    current = {"instance": first}
    monkeypatch.setattr(
        metrics_module,
        "find_instance_in_projects",
        lambda *_a, **_k: (current["instance"], "default"),
    )
    read_instance_metrics(client or object(), "cluster-a", "fixture", "default")
    current["instance"] = second
    return read_instance_metrics(client or object(), "cluster-a", "fixture", "default")


@pytest.mark.parametrize(
    "limits, vcpus",
    [("4", 4), ("0-3", 4), ("0,2", 2), ("0-3,8-11", 8), ("2", 2)],
)
def test_vm_limits_are_the_denominator(monkeypatch, limits, vcpus):
    # 2 busy vCPUs for 10 s.
    metrics = _two_samples(
        monkeypatch,
        _instance(kind="virtual-machine", limits=limits, cpu_ns=0),
        _instance(kind="virtual-machine", limits=limits, cpu_ns=20_000_000_000),
    )

    assert metrics.cpu_sample_status == "ready"
    assert metrics.cpu_effective_vcpus == vcpus
    assert metrics.cpu_busy_vcpus == 2.0
    assert metrics.cpu_usage_percent == round(min(2 / vcpus, 1) * 100, 2)


def test_a_vm_without_limits_runs_one_vcpu(monkeypatch):
    metrics = _two_samples(
        monkeypatch,
        _instance(kind="virtual-machine", cpu_ns=0),
        _instance(kind="virtual-machine", cpu_ns=5_000_000_000),
    )

    assert metrics.cpu_effective_vcpus == 1
    assert metrics.cpu_usage_percent == 50.0


def test_expanded_config_wins_over_the_local_config(monkeypatch):
    metrics = _two_samples(
        monkeypatch,
        _instance(limits="1", expanded={"limits.cpu": "4"}, cpu_ns=0),
        _instance(limits="1", expanded={"limits.cpu": "4"}, cpu_ns=10_000_000_000),
    )

    assert metrics.cpu_effective_vcpus == 4
    assert metrics.cpu_usage_percent == 25.0


def test_an_unlimited_container_is_measured_against_its_host_threads(monkeypatch):
    class _Resources:
        def get(self, params=None):
            assert params == {}, "a standalone host takes no target"
            return SimpleNamespace(json=lambda: {"metadata": {"cpu": {"total": 16}}})

    client = SimpleNamespace(api=SimpleNamespace(resources=_Resources()))
    metrics_module._host_threads_cache.clear()

    metrics = _two_samples(
        monkeypatch,
        _instance(cpu_ns=0),
        _instance(cpu_ns=40_000_000_000),
        client=client,
    )

    assert metrics.cpu_effective_vcpus == 16
    assert metrics.cpu_usage_percent == 25.0


def test_a_clustered_member_is_asked_for_its_own_threads():
    seen = []

    class _Resources:
        def get(self, params=None):
            seen.append(params)
            return SimpleNamespace(json=lambda: {"metadata": {"cpu": {"total": 8}}})

    client = SimpleNamespace(api=SimpleNamespace(resources=_Resources()))
    metrics_module._host_threads_cache.clear()

    assert metrics_module.host_threads(client, "c", "member-2") == 8
    assert metrics_module.host_threads(client, "c", "member-2") == 8
    assert seen == [{"target": "member-2"}], "the thread count is cached, not re-read per poll"


def test_unknown_vcpus_keep_the_busy_figure_but_no_percentage(monkeypatch):
    class _Broken:
        def get(self, params=None):
            raise RuntimeError("resources unavailable")

    metrics_module._host_threads_cache.clear()
    metrics = _two_samples(
        monkeypatch,
        _instance(cpu_ns=0),
        _instance(cpu_ns=30_000_000_000),
        client=SimpleNamespace(api=SimpleNamespace(resources=_Broken())),
    )

    assert metrics.cpu_sample_status == "unavailable"
    assert metrics.cpu_usage_percent == 0.0
    assert metrics.cpu_busy_vcpus == 3.0


def test_scheduler_jitter_never_reads_above_the_allocation(monkeypatch):
    metrics = _two_samples(
        monkeypatch,
        _instance(kind="virtual-machine", limits="2", cpu_ns=0),
        _instance(kind="virtual-machine", limits="2", cpu_ns=20_300_000_000),
    )

    assert metrics.cpu_usage_percent == 100.0
    assert metrics.cpu_busy_vcpus == 2.0


@pytest.mark.parametrize(
    "instance, reported, host, expected",
    [
        ({"type": "container", "config": {}}, 6.0, None, 6),
        ({"type": "virtual-machine", "config": {"limits.cpu": "2"}}, 0.0, None, 2),
        ({"type": "virtual-machine", "config": {}}, None, 32, 1),
        ({"type": "container", "config": {}}, None, 32, 32),
        ({"type": "container", "config": {}}, None, None, None),
        ({"type": "container", "config": {"limits.cpu": "abc"}}, None, None, None),
    ],
)
def test_effective_vcpu_lookup_order(instance, reported, host, expected):
    assert effective_vcpus(instance, reported=reported, host_threads=host) == expected
