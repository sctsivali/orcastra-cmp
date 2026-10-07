"""Monitoring CPU is busy hardware threads from the instances' own counters, never a guess.

The old sweep kept its own per-worker baselines, divided by `int(limits.cpu)` from the
instance-local config (so a range or an inherited limit counted as one vCPU), reported every
cold instance as 0.0 and flipped the gauge between allocation and usage depending on how many
Clusters were in scope.
"""

import asyncio

from app.services import monitoring_usage
from app.services.lxd.instance_metrics import STATE_SAMPLER, read_instance_metrics
from app.services.monitoring_usage import (
    legacy_top_lists,
    read_instance_usage,
    rollup,
    select_consumers,
)
from tests._monitoring_usage_fakes import node, row


def _usage(vms, threads=None):
    return asyncio.run(read_instance_usage(vms, threads or {"c1": 8}))


def test_a_cold_instance_is_calculating_not_zero():
    usages = _usage([row("a", cpu_ns=5_000_000_000)])

    assert usages[0].cpu_status == "calculating"
    assert usages[0].cpu_percent is None
    assert usages[0].busy_vcpus is None
    cpu, _memory = legacy_top_lists(usages)
    assert cpu == [], "a calculating instance must not reach a list that formats a number"


def test_a_second_sweep_reports_busy_vcpus_as_a_share_of_effective_vcpus():
    _usage([row("a", cpu_ns=0, read_at=1000.0, limits_cpu="0-3")])
    usages = _usage([row("a", cpu_ns=60_000_000_000, read_at=1030.0, limits_cpu="0-3")])

    usage = usages[0]
    assert usage.cpu_status == "ready"
    assert usage.busy_vcpus == 2.0
    assert usage.vcpus == 4, "a range counts every core it names, not one"
    assert usage.cpu_percent == 50.0


def test_an_unlimited_container_on_a_standalone_host_uses_its_threads():
    _usage([row("a", cpu_ns=0, read_at=1000.0, limits_cpu=None)], {"c1": 16})
    usages = _usage([row("a", cpu_ns=120_000_000_000, read_at=1030.0, limits_cpu=None)], {"c1": 16})

    assert usages[0].vcpus == 16
    assert usages[0].cpu_percent == 25.0


def test_rows_without_live_state_are_unavailable():
    usages = _usage([row("a", read_at=None)])

    assert usages[0].cpu_status == "unavailable"
    assert usages[0].memory_bytes == 0


def test_cluster_and_fleet_cpu_are_busy_hardware_threads():
    first = [row("a", cpu_ns=0, read_at=1000.0), row("b", host="c2", cpu_ns=0, read_at=1000.0)]
    _usage(first, {"c1": 8, "c2": 8})
    usages = _usage(
        [
            row("a", cpu_ns=60_000_000_000, read_at=1030.0),
            row("b", host="c2", cpu_ns=30_000_000_000, read_at=1030.0),
        ],
        {"c1": 8, "c2": 8},
    )
    nodes = [node("c1", threads=8, running=1), node("c2", threads=8, running=1)]

    fleet = rollup(nodes, usages)

    assert nodes[0].cpu_busy_threads == 2.0
    assert nodes[0].cpu_busy_percent == 25.0
    assert nodes[1].cpu_busy_threads == 1.0
    assert fleet.busy_threads == 3.0
    assert fleet.busy_percent == round(3 / 16 * 100, 2)
    assert fleet.status == "ready"
    assert fleet.reporting_instances == 2
    assert fleet.reporting_threads == 16


def test_a_cluster_without_readings_is_left_out_not_counted_idle():
    _usage([row("a", cpu_ns=0, read_at=1000.0)])
    usages = _usage([row("a", cpu_ns=60_000_000_000, read_at=1030.0)]) + _usage(
        [row("b", host="c2", read_at=None)]
    )
    nodes = [node("c1", threads=8, running=1), node("c2", threads=8, running=1)]

    fleet = rollup(nodes, usages)

    assert nodes[1].cpu_status == "unavailable"
    assert nodes[1].cpu_busy_percent is None
    assert fleet.busy_percent == 25.0, "c2's threads must not dilute c1's reading"
    assert fleet.reporting_threads == 8


def test_an_idle_cluster_reads_zero_ready():
    nodes = [node("c1", threads=8, running=0)]

    fleet = rollup(nodes, [])

    assert nodes[0].cpu_busy_percent == 0.0
    assert fleet.status == "ready"
    assert fleet.busy_percent == 0.0


def test_an_idle_cluster_never_vouches_for_a_fleet_still_calculating():
    """Seen live on the first sweep after a restart: every busy Cluster calculating, one idle."""
    usages = _usage([row("a", cpu_ns=0, read_at=1000.0)])
    nodes = [node("c1", threads=8, running=1), node("idle", threads=16, running=0)]

    fleet = rollup(nodes, usages)

    assert nodes[0].cpu_status == "calculating"
    assert nodes[1].cpu_busy_percent == 0.0, "the idle Cluster itself is a true zero"
    assert fleet.status == "calculating"
    assert fleet.busy_percent is None, "0.0% here would be drawn as the fleet idling"


def test_an_idle_cluster_counts_once_a_busy_one_reports():
    _usage([row("a", cpu_ns=0, read_at=1000.0)])
    usages = _usage([row("a", cpu_ns=60_000_000_000, read_at=1030.0)])
    nodes = [node("c1", threads=8, running=1), node("idle", threads=8, running=0)]

    fleet = rollup(nodes, usages)

    assert fleet.status == "ready"
    assert fleet.busy_percent == round(2 / 16 * 100, 2)


def test_an_offline_cluster_is_never_rolled_up():
    nodes = [node("c1", threads=8, running=2, online=False)]

    fleet = rollup(nodes, [])

    assert nodes[0].cpu_busy_percent is None
    assert fleet.status == "unavailable"


def test_each_resource_is_ranked_on_its_own():
    """Top Storage and Network used to be drawn from the top 20 by CPU."""
    vms = [row(f"busy{i}", cpu_ns=0, read_at=1000.0) for i in range(25)]
    vms.append(row("big-disk", cpu_ns=0, disk=10**12, read_at=1000.0))
    vms.append(row("chatty", cpu_ns=0, rx=0, read_at=1000.0))
    _usage(vms)
    later = [row(f"busy{i}", cpu_ns=50_000_000_000, read_at=1030.0) for i in range(25)]
    later.append(row("big-disk", cpu_ns=0, disk=10**12, read_at=1030.0))
    later.append(row("chatty", cpu_ns=0, rx=3_000_000_000, read_at=1030.0))
    usages = _usage(later)

    chosen = {u.vm.name for u in select_consumers(usages, per_resource=20)}

    assert "big-disk" in chosen, "an idle instance holding the most disk must still be listed"
    assert "chatty" in chosen, "the heaviest network user must be listed by rate"
    assert len(chosen) <= len(usages)


def test_the_sweep_warms_the_dialog_baseline(monkeypatch):
    """A dialog opened after a sweep answers at once instead of saying Calculating."""
    _usage([row("a", cpu_ns=0, read_at=1000.0)])

    class _State:
        cpu = {"usage": 20_000_000_000}
        memory = {"usage": 1, "total": 2}
        disk = {}
        network = {}
        processes = 5
        pid = 100
        started_at = None

    class _Instance:
        status = "Running"
        status_code = 103
        type = "container"
        config = {"volatile.uuid": "uuid-a", "limits.cpu": "2"}
        expanded_config = {"limits.cpu": "2"}
        last_used_at = "2026-10-07T00:00:00Z"
        location = "none"

        def state(self):
            return _State()

    from app.services.lxd import instance_metrics as metrics_module

    monkeypatch.setattr(metrics_module, "find_instance_in_projects", lambda *_a, **_k: (_Instance(), "default"))
    monkeypatch.setattr(metrics_module, "_now", lambda: 1020.0)

    metrics = read_instance_metrics(object(), "c1", "a", "default")

    assert metrics.cpu_sample_status == "ready"
    assert metrics.cpu_busy_vcpus == 1.0
    assert metrics.cpu_usage_percent == 50.0


def test_a_dialog_poll_moments_ago_is_reused_not_overwritten():
    """The sweep's list timestamp is coarser than a dialog's state read, so it must not win."""
    _usage([row("a", cpu_ns=0, read_at=1000.0)])
    _usage([row("a", cpu_ns=10_000_000_000, read_at=1010.0)])
    stored_before = STATE_SAMPLER._local.get_many([STATE_SAMPLER.key("c1:default:a:uuid-a")])[0]

    _usage([row("a", cpu_ns=10_500_000_000, read_at=1015.0)])

    stored_after = STATE_SAMPLER._local.get_many([STATE_SAMPLER.key("c1:default:a:uuid-a")])[0]
    assert stored_after.taken_at == stored_before.taken_at == 1010.0
    assert monitoring_usage.FLEET_MIN_WINDOW_SECONDS == 10.0


def test_a_malformed_row_marks_only_itself_unavailable():
    """A real LXD never sends this; an endpoint that does must not take the dashboard down."""
    bad = row("bad", cpu_ns=0, read_at=1000.0)
    bad.live_state["memory"] = [1]
    bad.live_state["network"] = {"eth0": "junk", "eth1": {"counters": [1, 2]}}
    good = row("good", cpu_ns=0, read_at=1000.0)

    usages = _usage([bad, good])

    assert [u.vm.name for u in usages] == ["bad", "good"]
    assert usages[0].memory_bytes == 0
    assert usages[1].cpu_status == "calculating"


def test_long_names_and_many_interfaces_stay_bounded():
    from app.services.lxd.instance_metrics import build_state_sample

    identity, sample, _agent = build_state_sample(
        cluster="c",
        project="p" * 5000,
        name="n",
        instance_type="container",
        config={"volatile.uuid": "u"},
        expanded_config={},
        last_used_at="x" * 10_000,
        state={
            "cpu": {"usage": 1},
            "pid": 7,
            "network": {
                f"if{n}": {"counters": {"bytes_received": n, "bytes_sent": n}} for n in range(500)
            },
        },
        taken_at=1.0,
    )

    assert len(identity) < 400
    assert len(sample.epoch) < 100
    assert len(sample.counters) <= 1 + 2 * 64
