"""One reading of `limits.cpu`, one way to count what a member has allocated.

Three counters used to exist (member dialog, node card, project pre-flight) and they
disagreed: two read the instance-local `config`, so a limit inherited from a profile counted
as one core; one summed every instance on the cluster against a single member's threads; a
mixed set ("0-3,8-11") came out as one core in one place and four in another; a reversed
range gave a negative number. The partner's MicroCloud showed "307 / 80 cores, 383.8 %" and a
project create was refused for "exceeding available CPU (-227)".

The rules pinned here follow LXD's own `limits.cpu` grammar: a count, a range, a set, or a
union of ranges and ids, with duplicates counted once. Anything LXD would refuse to store
falls back to the default rather than inventing capacity. Allocation reads the expanded
config, which is what the instance actually runs with.
"""

import pytest

from app.services.lxd.cpu_allocation import (
    CpuAllocation,
    allocated_cpu,
    allocation_ratio,
    clamp_available,
    instance_cpu_allocation,
    parse_cpu_limit,
)


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, 1),
        ("", 1),
        ("   ", 1),
        (4, 4),
        ("4", 4),
        (" 4 ", 4),
        ("0-3", 4),
        ("0,2", 2),
        ("0-3,8-11", 8),
        ("0-3,2", 4),        # 2 is inside 0-3, counted once
        ("1,1,1", 1),
        ("0", 1),            # zero cores is not a thing an instance runs with
        ("-2", 1),
        ("3-0", 1),          # reversed, LXD refuses it
        ("1-", 1),
        ("-3", 1),
        ("abc", 1),
        ("2.5", 1),
        ("0-3-5", 1),
    ],
)
def test_parse_cpu_limit(value, expected):
    assert parse_cpu_limit(value) == expected


def test_the_default_is_the_callers_choice():
    """The project usage read counts an unset limit as zero, because LXD refuses to create an
    instance without limits.cpu in a project that limits it; the member read counts one."""
    assert parse_cpu_limit(None, default=0) == 0
    assert parse_cpu_limit("garbage", default=0) == 0


class _Instance:
    def __init__(self, location="", status="Running", config=None, expanded_config=None):
        self.location = location
        self.status = status
        self.config = config or {}
        self.expanded_config = expanded_config if expanded_config is not None else dict(self.config)


def test_expanded_config_wins_over_config():
    inherited = _Instance(config={}, expanded_config={"limits.cpu": "8"})
    assert instance_cpu_allocation(inherited) == 8, "a profile-inherited limit is what the instance runs with"
    own = _Instance(config={"limits.cpu": "2"}, expanded_config={"limits.cpu": "2"})
    assert instance_cpu_allocation(own) == 2


def test_a_record_without_expanded_config_falls_back_to_config():
    class _Bare:
        config = {"limits.cpu": "3"}

    assert instance_cpu_allocation(_Bare()) == 3


def test_a_member_filter_ignores_instances_placed_elsewhere():
    rows = [
        _Instance("mc1", "Running", {"limits.cpu": "4"}),
        _Instance("mc1", "Running", {"limits.cpu": "4"}),
        _Instance("mc1", "Stopped", {"limits.cpu": "16"}),
        _Instance("mc2", "Running", {"limits.cpu": "32"}),
    ]
    got = allocated_cpu(rows, member="mc1")
    assert got == CpuAllocation(instances=3, running=2, cores=8)


def test_stopped_instances_can_be_counted_when_asked():
    rows = [_Instance("mc1", "Running", {"limits.cpu": "4"}), _Instance("mc1", "Stopped", {"limits.cpu": "16"})]
    assert allocated_cpu(rows, member="mc1", running_only=False).cores == 20


def test_standalone_counts_every_instance():
    rows = [
        _Instance("", "Running", {"limits.cpu": "2"}),
        _Instance("none", "Running", {"limits.cpu": "2"}),
        _Instance("nuc1", "Running", {"limits.cpu": "2"}),
    ]
    assert allocated_cpu(rows, member=None).cores == 6


def test_over_commit_yields_ratio_and_zero_available():
    assert allocation_ratio(307, 80) == pytest.approx(3.84, abs=0.005)
    assert allocation_ratio(0, 0) == 0.0
    assert clamp_available(80, 307) == 0
    assert clamp_available(80, 12) == 68


def test_a_huge_range_is_refused_without_building_it():
    """parse_cpu_limit used to materialise every id; four billion would exhaust a worker."""
    import time

    started = time.monotonic()
    assert parse_cpu_limit("0-4000000000") == 1
    assert parse_cpu_limit("0-65535") == 65536
    assert time.monotonic() - started < 0.5


def test_adjacent_and_overlapping_ranges_merge():
    assert parse_cpu_limit("0-3,4-7") == 8
    assert parse_cpu_limit("4-7,0-5") == 8
    assert parse_cpu_limit("0-3,2,3,10") == 5
