"""A virtual machine whose guest agent is unreachable reports no in-guest figures.

LXD renders such a VM with processes -1 and CPU and memory as zero; passed through, the dialog
would show "0.0%", "0 B / 0 B" and "-1" processes as if they were readings. Network and disk come
from the host side and stay available.
"""

from types import SimpleNamespace

from app.services.lxd import instance_metrics as metrics_module
from app.services.lxd.instance_metrics import build_state_sample, read_instance_metrics


def _vm(state, *, expanded=None):
    return SimpleNamespace(
        name="vm",
        status="Running",
        status_code=103,
        type="virtual-machine",
        config={"volatile.uuid": "u-9"},
        expanded_config=expanded,
        last_used_at="2026-10-07T00:00:00Z",
        location="none",
        state=lambda: state,
    )


def _agentless_state():
    return SimpleNamespace(
        cpu={"usage": 0},
        memory={"usage": 0, "usage_peak": 0, "total": 0, "swap_usage": 0},
        disk={"root": {"usage": 1_000, "total": 10_000}},
        network={"eth0": {"counters": {"bytes_received": 10, "bytes_sent": 20}, "addresses": []}},
        processes=-1,
        pid=4321,
        started_at=None,
    )


def _read(monkeypatch, instance):
    monkeypatch.setattr(
        metrics_module, "find_instance_in_projects", lambda *_a, **_k: (instance, "default")
    )
    return read_instance_metrics(object(), "cluster-a", "vm", "default")


def test_unreachable_agent_marks_guest_sections_unavailable(monkeypatch):
    metrics = _read(monkeypatch, _vm(_agentless_state()))

    assert metrics.guest_agent == "unreachable"
    assert metrics.cpu_sample_status == "unavailable"
    assert metrics.unavailable_sections == ["cpu", "memory", "processes"]
    assert metrics.processes == 0, "-1 is LXD's 'no agent', never a process count"
    assert metrics.memory_total_bytes == 0
    assert metrics.disk_usage_bytes == {"root": 1_000}
    assert metrics.network_rx_bytes == {"eth0": 10}


def test_disabled_agent_metrics_is_reported_as_disabled(monkeypatch):
    state = _agentless_state()
    state.processes = 3
    metrics = _read(monkeypatch, _vm(state, expanded={"security.agent.metrics": "false"}))

    assert metrics.guest_agent == "disabled"
    assert "cpu" in metrics.unavailable_sections
    assert "memory" in metrics.unavailable_sections


def test_connected_agent_is_reported(monkeypatch):
    state = _agentless_state()
    state.processes = 30
    state.cpu = {"usage": 123}
    state.memory = {"usage": 5, "total": 10}
    metrics = _read(monkeypatch, _vm(state))

    assert metrics.guest_agent == "connected"
    assert metrics.unavailable_sections == []
    assert metrics.processes == 30


def test_a_container_has_no_guest_agent(monkeypatch):
    instance = _vm(_agentless_state())
    instance.type = "container"
    instance.state().processes = 5
    metrics = _read(monkeypatch, instance)

    assert metrics.guest_agent == "not_applicable"


def test_no_cpu_counter_is_sampled_while_the_agent_is_away():
    _, sample, agent = build_state_sample(
        cluster="c",
        project="p",
        name="vm",
        instance_type="virtual-machine",
        config={},
        expanded_config={},
        last_used_at=None,
        state={"cpu": {"usage": 0}, "processes": -1, "network": {}},
        taken_at=1.0,
    )

    assert agent == "unreachable"
    assert "cpu_ns" not in sample.counters


def test_dialog_and_fleet_build_the_same_sample_from_one_state():
    """The fleet sweep passes the list row's raw dict; the dialog passes the state wrapper's."""
    raw = {
        "cpu": {"usage": 5},
        "network": {"eth0": {"counters": {"bytes_received": 1, "bytes_sent": 2}}},
        "processes": 4,
        "pid": 99,
    }
    common = dict(
        cluster="c",
        project="p",
        name="n",
        instance_type="container",
        config={"volatile.uuid": "u"},
        expanded_config={},
        last_used_at="t",
        taken_at=5.0,
    )
    from app.services.lxd.instance_metrics_sections import state_mapping
    from app.services.lxd.utils import StateWrapper

    from_row = build_state_sample(state=state_mapping(raw), **common)
    from_dialog = build_state_sample(state=state_mapping(StateWrapper(raw)), **common)

    assert from_row == from_dialog
    assert from_row[0] == "c:p:n:u"


def test_the_start_time_matches_whether_it_arrives_as_text_or_a_datetime():
    """The fleet's VMInfo parses last_used_at; the dialog's wrapper keeps LXD's string."""
    from datetime import datetime, timezone

    common = dict(
        cluster="c",
        project="p",
        name="n",
        instance_type="container",
        config={"volatile.uuid": "u"},
        expanded_config={},
        state={"cpu": {"usage": 5}, "pid": 7},
        taken_at=5.0,
    )

    as_text = build_state_sample(last_used_at="2026-10-07T01:02:03.123456789Z", **common)
    as_datetime = build_state_sample(
        last_used_at=datetime(2026, 10, 7, 1, 2, 3, 123456, tzinfo=timezone.utc), **common
    )

    assert as_text[1].epoch == as_datetime[1].epoch


def test_an_infinite_cpu_counter_is_not_a_reading():
    _, sample, _ = build_state_sample(
        cluster="c",
        project="p",
        name="n",
        instance_type="container",
        config={},
        expanded_config={},
        last_used_at=None,
        state={"cpu": {"usage": float("inf")}, "processes": 3},
        taken_at=1.0,
    )

    assert "cpu_ns" not in sample.counters
