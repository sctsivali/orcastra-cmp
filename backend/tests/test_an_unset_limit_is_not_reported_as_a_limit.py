"""An instance without a limit is reported as unlimited, not as the size of its host.

A container with no `limits.cpu` is measured against the host's hardware threads, and LXD reports
a container with no `limits.memory` as having the host's memory. The console then printed "0.9 of
64 vCPUs busy" and "2 GiB / 62 GiB", which reads as the instance's allocation when it is the
host's size. The readings carry whether the instance's own config sets each limit, so a surface
can say "no CPU limit" instead.

note: the figures stay. Inside an unlimited container lxcfs shows the same host values, so
withholding them here would hide nothing; the real control is setting the limits.
"""

from types import SimpleNamespace

import pytest

from app.models.schemas import VMInfo
from app.services.lxd import instance_metrics as metrics_module
from app.services.lxd.cpu_allocation import limits_set
from app.services.lxd.instance_metrics import read_instance_metrics
from app.services.monitoring_usage import InstanceUsage, summary


@pytest.mark.parametrize(
    "kind, config, complete, expected",
    [
        ("container", {}, True, (False, False)),
        ("container", {"limits.cpu": "2", "limits.memory": "4GiB"}, True, (True, True)),
        ("container", {"limits.cpu": "0-3"}, True, (True, False)),
        ("container", {"limits.cpu": "", "limits.memory": " "}, True, (False, False)),
        ("virtual-machine", {}, True, (True, True)),
        ("container", {}, False, (None, None)),
        ("container", {"limits.memory": "1GiB"}, False, (None, True)),
    ],
)
def test_limits_set_reads_the_instance_config(kind, config, complete, expected):
    assert limits_set(kind, config, complete=complete) == expected


def _vm(**fields) -> VMInfo:
    base = dict(name="web", host="nuc2", status="Running", status_code=103, type="container", project="default")
    base.update(fields)
    return VMInfo(**base)


def _usage(vm: VMInfo) -> InstanceUsage:
    return InstanceUsage(vm, "ready", 0.9, 64, 1.41, 2 * 1024**3, 62 * 1024**3, 0, 0, 0, None, None)


def test_a_consumer_row_says_its_limits_are_unset():
    vm = _vm(effective_cpu_limit=None, effective_memory_limit=None, effective_limits_known=True)
    row = summary(_usage(vm))
    assert (row.cpu_limit_set, row.memory_limit_set) == (False, False)
    assert row.cpu_vcpus == 64, "the figure stays; only its meaning is stated"


def test_a_consumer_row_with_limits_says_so():
    vm = _vm(effective_cpu_limit="4", effective_memory_limit="8GiB", effective_limits_known=True)
    row = summary(_usage(vm))
    assert (row.cpu_limit_set, row.memory_limit_set) == (True, True)


def test_a_consumer_row_whose_profiles_were_not_read_does_not_guess():
    vm = _vm(config={}, effective_limits_known=False)
    row = summary(_usage(vm))
    assert (row.cpu_limit_set, row.memory_limit_set) == (None, None)


def _instance(kind, expanded):
    state = SimpleNamespace(
        cpu={"usage": 1}, memory={"usage": 1024, "total": 62 * 1024**3}, disk={}, network={},
        processes=1, pid=10, started_at=None,
    )
    return SimpleNamespace(
        name="fixture", status="Running", status_code=103, type=kind, config={},
        expanded_config=expanded, last_used_at=None, location="none", state=lambda: state,
    )


@pytest.mark.parametrize(
    "kind, expanded, expected",
    [
        ("container", {"volatile.uuid": "u-1"}, (False, False)),
        ("container", {"limits.cpu": "2", "limits.memory": "4GiB", "volatile.uuid": "u-2"}, (True, True)),
        ("virtual-machine", {"volatile.uuid": "u-3"}, (True, True)),
    ],
)
def test_instance_metrics_say_whether_the_limits_are_set(monkeypatch, kind, expanded, expected):
    instance = _instance(kind, expanded)
    monkeypatch.setattr(metrics_module, "find_instance_in_projects", lambda *_a, **_k: (instance, "default"))
    metrics = read_instance_metrics(object(), "cluster-a", "fixture", "default")
    assert (metrics.cpu_limit_set, metrics.memory_limit_set) == expected


def test_a_local_config_alone_does_not_claim_an_unset_limit(monkeypatch):
    # With no expanded_config the reading falls back to the local config, where a profile's limit
    # is absent; absent is then unknown, not unset.
    instance = _instance("container", None)
    monkeypatch.setattr(metrics_module, "find_instance_in_projects", lambda *_a, **_k: (instance, "default"))
    metrics = read_instance_metrics(object(), "cluster-a", "fixture", "default")
    assert (metrics.cpu_limit_set, metrics.memory_limit_set) == (None, None)
