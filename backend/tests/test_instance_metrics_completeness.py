"""Instance metrics must distinguish a zero reading from an unavailable one."""

from types import SimpleNamespace

import pytest

from app.services.lxd import instance_metrics as metrics_module
from app.services.lxd.instance_metrics import read_instance_metrics
from app.services.lxd.utils import StateWrapper


ALL_SECTIONS = {"cpu", "memory", "disk", "network", "processes"}


def _instance(state, *, status="Running", kind="container", config=None):
    return SimpleNamespace(
        name="fixture",
        status=status,
        status_code=103 if status == "Running" else 102,
        type=kind,
        config=config if config is not None else {"limits.cpu": "2", "volatile.uuid": "u-1"},
        expanded_config=None,
        last_used_at=None,
        location="none",
        state=lambda: state,
    )


@pytest.fixture
def clock(monkeypatch):
    """Wall clock for the state read; each read consumes a start and an end reading."""
    ticks = []
    monkeypatch.setattr(metrics_module, "_now", lambda: ticks.pop(0) if ticks else 1000.0)
    return ticks


def _sample(monkeypatch, state, *, status="Running", **kwargs):
    instance = _instance(state, status=status, **kwargs)
    monkeypatch.setattr(
        metrics_module,
        "find_instance_in_projects",
        lambda *_args, **_kwargs: (instance, "default"),
    )
    return read_instance_metrics(object(), "cluster-a", "fixture", "default")


def _complete_state(*, cpu_usage=0):
    return SimpleNamespace(
        cpu={"usage": cpu_usage},
        memory={"usage": 0, "total": 0},
        disk={},
        network={},
        processes=0,
        pid=None,
        started_at=None,
    )


def test_stopped_instance_does_not_request_state(monkeypatch):
    instance = _instance(None, status="Stopped")
    instance.state = lambda: (_ for _ in ()).throw(AssertionError("state must not be read"))
    monkeypatch.setattr(
        metrics_module,
        "find_instance_in_projects",
        lambda *_args, **_kwargs: (instance, "default"),
    )

    metrics = read_instance_metrics(object(), "cluster-a", "fixture", "default")

    assert metrics.status.value == "Stopped"
    assert metrics.cpu_sample_status == "not_applicable"
    assert metrics.unavailable_sections == []
    assert metrics.timestamp.tzinfo is not None
    assert metrics.timestamp.utcoffset() is not None


def test_first_cpu_sample_is_calculating(monkeypatch, clock):
    metrics = _sample(monkeypatch, _complete_state(cpu_usage=1_000_000))

    assert metrics.cpu_usage_percent == 0.0
    assert metrics.cpu_sample_status == "calculating"
    assert metrics.unavailable_sections == []


def test_second_identical_cpu_sample_is_a_ready_zero(monkeypatch, clock):
    clock.extend([100.0, 100.0, 105.0, 105.0])
    state = _complete_state(cpu_usage=1_000_000)

    _sample(monkeypatch, state)
    metrics = _sample(monkeypatch, state)

    assert metrics.cpu_usage_percent == 0.0
    assert metrics.cpu_busy_vcpus == 0.0
    assert metrics.cpu_sample_status == "ready"
    assert metrics.sample_window_seconds == 5.0
    assert metrics.unavailable_sections == []


def test_timestamp_is_the_midpoint_of_the_state_read(monkeypatch, clock):
    clock.extend([200.0, 201.0])

    metrics = _sample(monkeypatch, _complete_state())

    assert metrics.timestamp.timestamp() == 200.5


def test_absent_state_marks_every_section_unavailable(monkeypatch, clock):
    metrics = _sample(monkeypatch, None)

    assert metrics.cpu_sample_status == "unavailable"
    assert set(metrics.unavailable_sections) == ALL_SECTIONS


def test_missing_readings_mark_every_section_unavailable(monkeypatch, clock):
    state = SimpleNamespace(
        cpu=None,
        memory=None,
        disk=None,
        network=None,
        processes=None,
        pid=None,
        started_at=None,
    )

    metrics = _sample(monkeypatch, state)

    assert metrics.cpu_sample_status == "unavailable"
    assert set(metrics.unavailable_sections) == ALL_SECTIONS


def test_state_wrapper_preserves_legacy_defaults_but_marks_omitted_fields(monkeypatch, clock):
    state = StateWrapper({})

    metrics = _sample(monkeypatch, state)

    # Existing StateWrapper consumers keep their original fallback shapes.
    assert state.cpu == {}
    assert state.memory == {}
    assert state.disk == {}
    assert state.network == {}
    assert state.processes == 0
    # Metrics uses raw presence, so those fallbacks cannot masquerade as reported values.
    assert metrics.cpu_sample_status == "unavailable"
    assert set(metrics.unavailable_sections) == ALL_SECTIONS


def test_state_wrapper_keeps_explicit_zero_and_empty_sections_available(monkeypatch, clock):
    state = StateWrapper(
        {
            "cpu": {"usage": 0},
            "memory": {"usage": 0, "total": 0},
            "disk": {},
            "network": {},
            "processes": 0,
        }
    )

    metrics = _sample(monkeypatch, state)

    assert metrics.cpu_sample_status == "calculating"
    assert metrics.unavailable_sections == []
    assert metrics.memory_used_bytes == 0
    assert metrics.memory_total_bytes == 0
    assert metrics.disk_usage_bytes == {}
    assert metrics.network_rx_bytes == {}
    assert metrics.network_tx_bytes == {}
    assert metrics.processes == 0


def test_explicit_zeroes_and_empty_collections_are_available(monkeypatch, clock):
    metrics = _sample(monkeypatch, _complete_state())

    assert metrics.cpu_sample_status == "calculating"
    assert metrics.unavailable_sections == []
    assert metrics.memory_used_bytes == 0
    assert metrics.memory_total_bytes == 0
    assert metrics.disk_usage_bytes == {}
    assert metrics.network_rx_bytes == {}
    assert metrics.network_tx_bytes == {}
    assert metrics.processes == 0


def test_partial_state_names_only_incomplete_sections(monkeypatch, clock):
    state = SimpleNamespace(
        cpu={"usage": 0},
        memory={"usage": 4096},
        disk={},
        network={
            "eth0": {
                "counters": {"bytes_received": 128},
                "addresses": [],
            }
        },
        processes=0,
        pid=None,
        started_at=None,
    )

    metrics = _sample(monkeypatch, state)

    assert metrics.cpu_sample_status == "calculating"
    assert metrics.unavailable_sections == ["memory", "network"]
    assert metrics.memory_used_bytes == 4096
    assert metrics.memory_total_bytes == 0
    assert metrics.network_rx_bytes == {"eth0": 128}
    assert metrics.network_tx_bytes == {}


def test_disk_quota_is_reported_beside_usage(monkeypatch, clock):
    state = _complete_state()
    state.disk = {"root": {"usage": 5_000, "total": 20_000}, "data": {"usage": 7, "total": 0}}

    metrics = _sample(monkeypatch, state)

    assert metrics.disk_usage_bytes == {"root": 5_000, "data": 7}
    assert metrics.disk_total_bytes == {"root": 20_000}


def test_network_rates_follow_the_counters(monkeypatch, clock):
    clock.extend([100.0, 100.0, 104.0, 104.0])

    def state(rx, tx):
        value = _complete_state()
        value.network = {
            "eth0": {"counters": {"bytes_received": rx, "bytes_sent": tx}, "addresses": []},
            "lo": {"counters": {"bytes_received": 10**9, "bytes_sent": 10**9}, "addresses": []},
        }
        return value

    _sample(monkeypatch, state(1_000, 500))
    metrics = _sample(monkeypatch, state(5_000, 2_500))

    assert metrics.network_rx_bytes_per_second == {"eth0": 1_000.0}
    assert metrics.network_tx_bytes_per_second == {"eth0": 500.0}
    assert "lo" not in metrics.network_rx_bytes


def test_a_restart_reseeds_instead_of_subtracting(monkeypatch, clock):
    clock.extend([100.0, 100.0, 105.0, 105.0])
    before = _complete_state(cpu_usage=9_000_000_000_000)
    before.pid = 4242
    after = _complete_state(cpu_usage=1_000_000)
    after.pid = 5151

    _sample(monkeypatch, before)
    metrics = _sample(monkeypatch, after)

    assert metrics.cpu_sample_status == "calculating"
    assert metrics.cpu_usage_percent == 0.0
