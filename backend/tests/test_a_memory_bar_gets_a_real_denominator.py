"""The instance detail reading must name what it carries, and carry a usable denominator.

`get_resource_usage` filled three fields from an LXD state block and two of them were wrong in a
way no gate could see, because nothing in this suite touched the function at all.

`memory_limit` was `state.memory["usage_peak"]`, a cgroup high-water mark. A VM does not keep
that counter, so every VM on the fleet reports it as 0 and the detail dialog printed
"Memory Limit 0" for all of them while the usage figure beside it had nothing to divide into.
`cpu_usage` was `state.cpu["usage"]`, cumulative nanoseconds of CPU time, declared as a
percentage and rendered as a bare fifteen-digit counter.

The numbers below are a live read of nuc2 rather than invented ones, so this file and the
browser suite assert against the same instances: ATLAS-Semanan sits at 78.4 percent of its
4 GiB, and pf-semanan runs while its guest reports nothing at all, which is the case that must
not become a confident zero.
"""

from types import SimpleNamespace

import pytest

from app.services.lxd import resources as resources_module
from app.services.lxd.resources import ResourceManager


# Live values from nuc2. usage_peak is 0 on all of them, which is the whole point.
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

    # The bug this file exists for: usage_peak is 0 here, so reading it as the limit produced a
    # denominator of zero and the card had to print a raw byte count instead of a bar.
    assert usage.memory_total != ATLAS["memory"]["usage_peak"]
    assert usage.memory_total > usage.memory_usage

    percent = usage.memory_usage / usage.memory_total * 100
    assert 78.0 < percent < 79.0, f"expected the live 78.4 percent reading, got {percent}"


def test_cpu_time_is_carried_as_nanoseconds(manager):
    usage = manager.get_resource_usage(_instance(ATLAS))

    assert usage is not None
    assert usage.cpu_time_ns == 56974987602000
    # 15h 49m of CPU time. If this were ever a percentage it would have to be under 100, and the
    # field's old name and description both claimed exactly that.
    assert usage.cpu_time_ns / 1e9 > 56_000


def test_the_retired_names_are_gone(manager):
    """A field named for an interpretation rather than its source is how this bug survived."""
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


def test_an_idle_instance_still_records_a_cpu_baseline(manager, monkeypatch):
    """A cumulative zero is a reading, and the metrics path used to drop it as falsy.

    `if state.cpu and state.cpu.get("usage")` skipped the whole block on a cumulative 0, so no
    percentage was computed and, worse, no baseline sample was stored. An instance that idled
    through its first poll never began accumulating history and read 0 percent forever after.
    """
    idle = _instance(SILENT)
    monkeypatch.setattr(resources_module, "find_instance_in_projects", lambda *a, **k: (idle, "default"))

    metrics = manager.get_instance_metrics(object(), "nuc2", "fixture")

    assert "nuc2:fixture" in manager._cpu_history, "a zero reading must still seed the history"
    assert manager._cpu_history["nuc2:fixture"]["usage_ns"] == 0
    assert metrics.cpu_usage_percent == 0.0
