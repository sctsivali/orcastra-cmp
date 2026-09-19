"""Instance metrics must distinguish a zero reading from an unavailable one."""

from types import SimpleNamespace

from app.services.lxd import resources as resources_module
from app.services.lxd.resources import ResourceManager
from app.services.lxd.utils import StateWrapper


ALL_SECTIONS = {"cpu", "memory", "disk", "network", "processes"}


def _instance(state, *, status="Running"):
    return SimpleNamespace(
        name="fixture",
        status=status,
        status_code=103 if status == "Running" else 102,
        config={},
        last_used_at=None,
        state=lambda: state,
    )


def _sample(manager, monkeypatch, state, *, status="Running"):
    instance = _instance(state, status=status)
    monkeypatch.setattr(
        resources_module,
        "find_instance_in_projects",
        lambda *_args, **_kwargs: (instance, "default"),
    )
    return manager.get_instance_metrics(object(), "cluster-a", "fixture", "default")


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
    manager = ResourceManager()
    instance = _instance(None, status="Stopped")
    instance.state = lambda: (_ for _ in ()).throw(AssertionError("state must not be read"))
    monkeypatch.setattr(
        resources_module,
        "find_instance_in_projects",
        lambda *_args, **_kwargs: (instance, "default"),
    )

    metrics = manager.get_instance_metrics(object(), "cluster-a", "fixture", "default")

    assert metrics.status.value == "Stopped"
    assert metrics.cpu_sample_status == "not_applicable"
    assert metrics.unavailable_sections == []
    assert metrics.timestamp.tzinfo is not None
    assert metrics.timestamp.utcoffset() is not None


def test_first_cpu_sample_is_calculating(monkeypatch):
    metrics = _sample(ResourceManager(), monkeypatch, _complete_state(cpu_usage=1_000_000))

    assert metrics.cpu_usage_percent == 0.0
    assert metrics.cpu_sample_status == "calculating"
    assert metrics.unavailable_sections == []


def test_second_identical_cpu_sample_is_a_ready_zero(monkeypatch):
    manager = ResourceManager()
    times = iter((100.0, 105.0))
    monkeypatch.setattr(resources_module.time, "time", lambda: next(times))
    state = _complete_state(cpu_usage=1_000_000)

    _sample(manager, monkeypatch, state)
    metrics = _sample(manager, monkeypatch, state)

    assert metrics.cpu_usage_percent == 0.0
    assert metrics.cpu_sample_status == "ready"
    assert metrics.unavailable_sections == []


def test_absent_state_marks_every_section_unavailable(monkeypatch):
    metrics = _sample(ResourceManager(), monkeypatch, None)

    assert metrics.cpu_sample_status == "unavailable"
    assert set(metrics.unavailable_sections) == ALL_SECTIONS


def test_missing_readings_mark_every_section_unavailable(monkeypatch):
    state = SimpleNamespace(
        cpu=None,
        memory=None,
        disk=None,
        network=None,
        processes=None,
        pid=None,
        started_at=None,
    )

    metrics = _sample(ResourceManager(), monkeypatch, state)

    assert metrics.cpu_sample_status == "unavailable"
    assert set(metrics.unavailable_sections) == ALL_SECTIONS


def test_state_wrapper_preserves_legacy_defaults_but_marks_omitted_fields(monkeypatch):
    state = StateWrapper({})

    metrics = _sample(ResourceManager(), monkeypatch, state)

    # Existing StateWrapper consumers keep their original fallback shapes.
    assert state.cpu == {}
    assert state.memory == {}
    assert state.disk == {}
    assert state.network == {}
    assert state.processes == 0
    # Metrics uses raw presence, so those fallbacks cannot masquerade as reported values.
    assert metrics.cpu_sample_status == "unavailable"
    assert set(metrics.unavailable_sections) == ALL_SECTIONS


def test_state_wrapper_keeps_explicit_zero_and_empty_sections_available(monkeypatch):
    state = StateWrapper(
        {
            "cpu": {"usage": 0},
            "memory": {"usage": 0, "total": 0},
            "disk": {},
            "network": {},
            "processes": 0,
        }
    )

    metrics = _sample(ResourceManager(), monkeypatch, state)

    assert metrics.cpu_sample_status == "calculating"
    assert metrics.unavailable_sections == []
    assert metrics.memory_used_bytes == 0
    assert metrics.memory_total_bytes == 0
    assert metrics.disk_usage_bytes == {}
    assert metrics.network_rx_bytes == {}
    assert metrics.network_tx_bytes == {}
    assert metrics.processes == 0


def test_explicit_zeroes_and_empty_collections_are_available(monkeypatch):
    metrics = _sample(ResourceManager(), monkeypatch, _complete_state())

    assert metrics.cpu_sample_status == "calculating"
    assert metrics.unavailable_sections == []
    assert metrics.memory_used_bytes == 0
    assert metrics.memory_total_bytes == 0
    assert metrics.disk_usage_bytes == {}
    assert metrics.network_rx_bytes == {}
    assert metrics.network_tx_bytes == {}
    assert metrics.processes == 0


def test_partial_state_names_only_incomplete_sections(monkeypatch):
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

    metrics = _sample(ResourceManager(), monkeypatch, state)

    assert metrics.cpu_sample_status == "calculating"
    assert metrics.unavailable_sections == ["memory", "network"]
    assert metrics.memory_used_bytes == 4096
    assert metrics.memory_total_bytes == 0
    assert metrics.network_rx_bytes == {"eth0": 128}
    assert metrics.network_tx_bytes == {}
