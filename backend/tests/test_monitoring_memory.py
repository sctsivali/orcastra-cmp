"""Top Consumers memory without reclaimable page cache, from one metrics read per member.

The state the fleet list carries reports a container's cgroup usage and a VM's MemTotal minus
MemFree, both counting page cache, so the dashboard ranked file I/O as memory while the instance
dialog, which reads /1.0/metrics, showed the honest figure. These tests pin the sweep's memory
phase: the dialog's gate, one read per member, a bounded phase, and a flag on every row saying
which figure it carries.
"""

import asyncio
import inspect
import threading

import pytest

from app.services import monitoring_memory
from app.services.lxd.metrics_scrape import MemberScrape
from app.services.lxd.metrics_text import InstanceSeries
from app.services.monitoring_memory import apply_member_memory
from app.services.monitoring_usage import read_instance_usage, summary
from tests._monitoring_usage_fakes import row

GIB = 1024**3


def _series(kind: str) -> InstanceSeries:
    """1 GiB in use: a container's from free and cache, a VM's from MemAvailable."""
    return InstanceSeries(
        instance_type=kind,
        mem_total=4 * GIB,
        mem_available=3 * GIB,
        mem_free=GIB,
        mem_cached=2 * GIB,
    )


def _usages(*rows):
    return asyncio.run(read_instance_usage(list(rows), {"c1": 8, "c2": 8}))


class _Service:
    def __init__(self):
        self.asked = []

    def get_client_with_reconnect(self, cluster):
        self.asked.append(cluster)
        return f"client-{cluster}"


@pytest.fixture
def scrapes(monkeypatch):
    """(cluster, member) -> MemberScrape or None; records every read."""
    answers = {}
    calls = []

    def fake(client, cluster, member, *, deadline):
        calls.append((client, cluster, member))
        return answers.get((cluster, member))

    monkeypatch.setattr(monitoring_memory, "member_metrics", fake)
    return answers, calls


def test_containers_and_vms_with_an_agent_take_memory_without_page_cache(scrapes):
    answers, calls = scrapes
    rows = _usages(
        row("web", memory=3 * GIB),
        row("db", kind="virtual-machine", memory=3 * GIB),
        row("old", kind="virtual-machine", memory=3 * GIB, processes=-1),
    )
    answers[("c1", "none")] = MemberScrape(
        collected_at=1.0,
        series={
            ("default", "web"): _series("container"),
            ("default", "db"): _series("virtual-machine"),
            ("default", "old"): _series("virtual-machine"),
        },
    )

    shown, counts = asyncio.run(apply_member_memory(_Service(), rows))

    by_name = {usage.vm.name: usage for usage in shown}
    assert (by_name["web"].memory_bytes, by_name["web"].memory_includes_cache) == (GIB, False)
    assert (by_name["db"].memory_bytes, by_name["db"].memory_includes_cache) == (GIB, False)
    assert (by_name["old"].memory_bytes, by_name["old"].memory_includes_cache) == (3 * GIB, True), (
        "without lxd-agent the metrics hold QEMU's host RSS, not the guest's memory"
    )
    assert counts == {"honest": 2, "with_cache": 1, "members": 1, "members_read": 1}
    assert [(cluster, member) for _, cluster, member in calls] == [("c1", "none")]


def test_one_read_per_member_and_a_silent_member_keeps_its_rows_labelled(scrapes):
    answers, calls = scrapes
    clustered = [
        row(name, host="c2").model_copy(update={"location": member})
        for name, member in (("a", "m1"), ("b", "m1"), ("c", "m2"))
    ]
    answers[("c2", "m1")] = MemberScrape(
        collected_at=1.0,
        series={("default", "a"): _series("container"), ("default", "b"): _series("container")},
    )

    shown, counts = asyncio.run(apply_member_memory(_Service(), _usages(*clustered)))

    assert sorted((cluster, member) for _, cluster, member in calls) == [("c2", "m1"), ("c2", "m2")]
    assert [usage.memory_includes_cache for usage in shown] == [False, False, True]
    assert counts["members_read"] == 1 and counts["with_cache"] == 1


def test_an_instance_missing_from_its_members_read_keeps_the_state_figure(scrapes):
    answers, _ = scrapes
    answers[("c1", "none")] = MemberScrape(collected_at=1.0, series={("other", "web"): _series("container")})

    (shown,), _ = asyncio.run(apply_member_memory(_Service(), _usages(row("web", memory=777))))

    assert (shown.memory_bytes, shown.memory_includes_cache) == (777, True), "same name, other project"


def test_reads_are_bounded_per_cluster_on_their_own_executor(monkeypatch):
    seen = []

    async def recorder(fn, *args, **options):
        seen.append(options)
        return None

    monkeypatch.setattr(monitoring_memory, "run_blocking", recorder)

    asyncio.run(apply_member_memory(_Service(), _usages(row("web"))))

    (options,) = seen
    assert options["executor"] is monitoring_memory._executor
    assert options["concurrency_key"] == "c1" and options["max_in_flight_per_key"] == 2
    assert options["timeout"] <= monitoring_memory.SCRAPE_SECONDS


def test_a_member_that_never_answers_costs_the_sweep_its_phase_wall_at_most(monkeypatch):
    release = threading.Event()

    def hangs(lxd_service, cluster, member, deadline):
        release.wait(30)
        return None

    monkeypatch.setattr(monitoring_memory, "_member_scrape", hangs)
    monkeypatch.setattr(monitoring_memory, "PHASE_SECONDS", 0.4)
    monkeypatch.setattr(monitoring_memory, "SCRAPE_SECONDS", 0.3)
    monkeypatch.setattr(monitoring_memory, "_MIN_SCRAPE_SECONDS", 0.1)
    try:
        (shown,), counts = asyncio.run(apply_member_memory(_Service(), _usages(row("web", memory=5))))
    finally:
        release.set()

    assert (shown.memory_bytes, shown.memory_includes_cache) == (5, True)
    assert counts["members_read"] == 0


def test_no_running_instance_means_no_read(scrapes):
    _, calls = scrapes

    shown, counts = asyncio.run(apply_member_memory(_Service(), []))

    assert shown == [] and calls == [] and counts["members"] == 0


def test_the_consumer_row_says_which_figure_it_carries(scrapes):
    answers, _ = scrapes
    answers[("c1", "none")] = MemberScrape(collected_at=1.0, series={("default", "web"): _series("container")})
    (shown,), _ = asyncio.run(apply_member_memory(_Service(), _usages(row("web"))))

    assert summary(shown).memory_includes_cache is False
    assert summary(_usages(row("web"))[0]).memory_includes_cache is True


def test_the_dashboard_runs_the_memory_phase_and_logs_its_counts():
    from app.routers import monitoring

    source = inspect.getsource(monitoring.get_monitoring_dashboard)

    assert 'timer.timed("scrapes", apply_member_memory(lxd_service, usages))' in source
    assert "memory=memory_counts" in source
    assert source.index("apply_member_memory") < source.index("select_consumers(usages)")
