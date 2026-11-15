"""A key creates a project on a shared cluster with the limits the browser sets.

The browser sends max_cpu, max_ram and max_storage, which the LXD service writes as limits.cpu,
limits.memory and limits.disk after the shared-cluster policy check. The key path ran the same
policy over the raw config and refused those three keys, so an OrcaHub purchase that carries its
quota was refused with a policy object a string-reading client shows as a bare 403. The key path
now accepts the three, validated, and the refusal message names what it refused and what is
allowed. Nothing else is widened: limits.processes, raw.* and restricted.* stay refused.
"""

import pytest
from fastapi import HTTPException

from app.services.instance_security_policy import (
    InstanceSecurityPolicyViolation,
    prepare_project_create_config,
)
from app.services.key_party_bound import _shared_policy, shared_project_config

CLUSTER = "cluster-a"


def test_the_three_browser_limits_are_kept_on_a_key_create() -> None:
    config = shared_project_config(CLUSTER, {"limits.cpu": "4", "limits.memory": "8GiB", "limits.disk": "50GiB"})
    assert config["limits.cpu"] == "4"
    assert config["limits.memory"] == "8GiB"
    assert config["limits.disk"] == "50GiB"
    assert config["restricted"] == "true", "isolation is still forced"


@pytest.mark.parametrize("key,value", [
    ("limits.cpu", "0"), ("limits.cpu", "two"), ("limits.memory", "lots"), ("limits.disk", "-1GiB"),
    ("limits.memory", "50%"),
])
def test_a_malformed_limit_is_refused(key: str, value: str) -> None:
    with pytest.raises(HTTPException) as refused:
        shared_project_config(CLUSTER, {key: value})
    assert refused.value.status_code == 403
    assert refused.value.detail["violations"] == [{"code": "invalid_limit_value", "field": f"config.{key}"}]


@pytest.mark.parametrize("key", ["limits.processes", "limits.instances", "restricted.devices.nic", "raw.lxc"])
def test_other_keys_stay_refused(key: str) -> None:
    with pytest.raises(HTTPException) as refused:
        shared_project_config(CLUSTER, {key: "1"})
    assert refused.value.detail["violations"] == [{"code": "project_config_key_not_allowed", "field": f"config.{key}"}]


def test_the_refusal_message_names_the_refused_field_and_the_allowed_keys() -> None:
    with pytest.raises(HTTPException) as refused:
        shared_project_config(CLUSTER, {"limits.processes": "100"})
    message = refused.value.detail["message"]
    assert "config.limits.processes" in message
    assert "limits.cpu, limits.memory, limits.disk" in message
    assert "user.*" in message


def test_the_browser_policy_itself_is_unchanged() -> None:
    # The browser sends limits through max_* fields, never through config, so its policy keeps
    # refusing them in config.
    with pytest.raises(InstanceSecurityPolicyViolation):
        prepare_project_create_config(_shared_policy(CLUSTER), {"limits.cpu": "4"})
