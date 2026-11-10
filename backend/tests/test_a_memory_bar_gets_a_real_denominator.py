"""The instance detail reading must name what it carries, and carry a usable denominator.

`get_resource_usage` fills its fields from an LXD state block. The memory denominator is
`state.memory["total"]`. `usage_peak` is a cgroup high-water mark that a VM does not keep and
reports as 0, so it cannot serve as a limit. `cpu_time_ns` is `state.cpu["usage"]`, cumulative
nanoseconds of CPU time rather than a percentage.

The fixtures are real LXD readings: one instance at 78.4 percent of its 4 GiB, and one running
guest that reports nothing at all, which must not become a confident zero.
"""

from types import SimpleNamespace

import pytest

from app.services.lxd import instance_metrics as metrics_module
from app.services.lxd.instance_metrics import (
    STATE_SAMPLER,
    instance_rate_identity,
    read_instance_metrics,
)
from app.services.lxd.resources import ResourceManager


# A real LXD reading. usage_peak is 0, so it cannot serve as the denominator.
ATLAS = {
    "memory": {"usage": 3196878848, "usage_peak": 0, "total": 4078297088, "swap_usage": 0},
    "cpu": {"usage": 56974987602000},
}
# Running, but the guest reports nothing. Every figure is a real zero.
SILENT = {
    "memory": {"usage": 0, "usage_peak": 0, "total": 0, "swap_usage": 0},
    "cpu": {"usage": 0},
}


def _instance(state_data, status="Running"):
    """A pylxd instance stub: `.state()` returns an object with cpu/memory/disk/network."""
    state = SimpleNamespace(
        cpu=state_data.get("cpu"),
        memory=state_data.get("memory"),
        disk=state_data.get("disk"),
        network=state_data.get("network"),
        processes=state_data.get("processes", 0),
        pid=state_data.get("pid", 0),
        started_at=state_data.get("started_at"),
    )
    return SimpleNamespace(
        name="fixture",
        status=status,
        status_code=103 if status == "Running" else 102,
        config={},
        last_used_at=None,
        state=lambda: state,
    )


@pytest.fixture
def manager():
    return ResourceManager()


def test_the_denominator_is_total_not_the_peak(manager):
    usage = manager.get_resource_usage(_instance(ATLAS))

    assert usage is not None
    assert usage.memory_total == 4078297088, "memory_total must come from state.memory['total']"
    assert usage.memory_usage == 3196878848

    # usage_peak is 0 here, so reading it as the limit would give a zero denominator and the
    # card could not draw a bar.
    assert usage.memory_total != ATLAS["memory"]["usage_peak"]
    assert usage.memory_total > usage.memory_usage

    percent = usage.memory_usage / usage.memory_total * 100
    assert 78.0 < percent < 79.0, f"expected the live 78.4 percent reading, got {percent}"


def test_cpu_time_is_carried_as_nanoseconds(manager):
    usage = manager.get_resource_usage(_instance(ATLAS))

    assert usage is not None
    assert usage.cpu_time_ns == 56974987602000
    # 15h 49m of CPU time; a percentage would be under 100.
    assert usage.cpu_time_ns / 1e9 > 56_000


def test_the_retired_names_are_gone(manager):
    """Fields are named for their source, so the interpretive names stay out of the model."""
    usage = manager.get_resource_usage(_instance(ATLAS))

    assert usage is not None
    dumped = usage.model_dump()
    assert "cpu_usage" not in dumped
    assert "memory_limit" not in dumped
    assert {"cpu_time_ns", "memory_usage", "memory_total"} <= set(dumped)


def test_a_silent_guest_yields_no_denominator(manager):
    """usage 0 of total 0 must stay 0 of 0, so the card can tell absent from idle."""
    usage = manager.get_resource_usage(_instance(SILENT))

    assert usage is not None
    assert usage.memory_total == 0, "a zero total must not be back-filled with anything"
    assert usage.memory_usage == 0
    assert usage.cpu_time_ns == 0


@pytest.mark.parametrize("status", ["Stopped", "Frozen", "Error"])
def test_a_stopped_instance_reports_nothing(manager, status):
    assert manager.get_resource_usage(_instance(ATLAS, status=status)) is None


def test_a_state_block_missing_its_keys_does_not_invent_them(manager):
    usage = manager.get_resource_usage(_instance({"memory": {}, "cpu": {}}))

    assert usage is not None
    assert usage.memory_total is None
    assert usage.memory_usage is None
    assert usage.cpu_time_ns is None


def _stored(identity):
    key = STATE_SAMPLER.key(identity)
    return STATE_SAMPLER._local.get_many([key])[0]


def test_an_idle_instance_still_records_a_cpu_baseline(monkeypatch):
    """A cumulative zero is a reading and must seed the baseline.

    A falsy check on `state.cpu["usage"]` would skip a cumulative 0, compute no percentage and
    store no baseline sample, so an instance idle through its first poll would never start
    accumulating history.
    """
    idle = _instance(SILENT)
    monkeypatch.setattr(metrics_module, "find_instance_in_projects", lambda *a, **k: (idle, "default"))

    metrics = read_instance_metrics(object(), "nuc2", "fixture")

    # The identity carries the project: a name is unique per project, not per host.
    stored = _stored(instance_rate_identity("nuc2", "default", "fixture", None))
    assert stored is not None, "a zero reading must still seed the baseline"
    assert stored.counters["cpu_ns"] == 0
    assert metrics.cpu_usage_percent == 0.0


def test_two_instances_of_one_name_in_different_projects_keep_separate_baselines(monkeypatch):
    """The baseline is cumulative CPU nanoseconds, so a shared key subtracts the wrong reading.

    Keyed on host and name alone, `default/web-01` and `acme/web-01` on one host would share a
    slot, and each poll would compute its delta against the other instance's counter, giving a
    percentage that belongs to neither and can be negative.
    """
    busy = _instance(ATLAS)

    monkeypatch.setattr(
        metrics_module, "find_instance_in_projects", lambda *a, **k: (busy, "default")
    )
    read_instance_metrics(object(), "nuc2", "web-01", "default")

    monkeypatch.setattr(
        metrics_module, "find_instance_in_projects", lambda *a, **k: (busy, "acme")
    )
    read_instance_metrics(object(), "nuc2", "web-01", "acme")

    assert _stored(instance_rate_identity("nuc2", "default", "web-01", None)) is not None
    assert _stored(instance_rate_identity("nuc2", "acme", "web-01", None)) is not None, (
        "the second project must not have written into the first one's slot"
    )
