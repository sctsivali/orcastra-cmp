"""The key-authenticated surface can set the upstream product's snapshot-schedule keys, and until
now nobody could tell that it had.

`snapshots.schedule` decides whether the console treats an instance as owned by the metered
snapshot product sold upstream, which decides whether an operator's own snapshot actions on it are
refused. The proxy prices an instance-config change by what it reaches inside the GUEST
(`config_reaches_inside`), and these keys reach sideways into another product instead, so they cost
plain `write`. The raw proxy is worse: it inspects no body at all.

This is observation, on purpose, and the tests below pin that. Which keys legitimately write these
is not answerable from this repository, and enforcing on an assumption about a live paid product is
how an outage happens. The record is what a refusal will later be fitted to.

The console's own create paths did not wait for that answer, because no user-authenticated caller
has a reason to set these. See test_the_create_form_cannot_set_a_snapshot_schedule.py.
"""

import ast
import inspect

import pytest

from app.core.api_key_capabilities import (
    config_reaches_inside,
    policy_keys_in_config,
    policy_keys_in_raw_body,
)
from app.core.lxd_names import POLICY_CONFIG_KEYS
from app.services.activity_logger import ACTION_CATEGORIES, HIGH_SEVERITY_ACTIONS

ACTION = "integration.instance.config.policy_key_write"


@pytest.mark.parametrize("key", POLICY_CONFIG_KEYS)
def test_every_policy_key_is_seen_in_a_config(key):
    assert policy_keys_in_config({key: "@daily"}) == [key]


def test_an_ordinary_config_is_not_reported():
    """The control. Reporting every config change would bury the four that matter."""
    assert policy_keys_in_config({"limits.cpu": "2", "user.tags": "a"}) == []
    assert policy_keys_in_config(None) == []
    assert policy_keys_in_config("not a dict") == []


def test_a_shouted_key_is_still_seen():
    assert policy_keys_in_config({"Snapshots.Schedule": "@daily"}) == ["snapshots.schedule"]


def test_the_raw_body_is_read_only_for_instances():
    body = {"config": {"snapshots.schedule": "@daily"}}
    assert policy_keys_in_raw_body("/1.0/instances/web-01", body) == ["snapshots.schedule"]
    # A storage volume carries the same key names and is not this observation's business.
    assert policy_keys_in_raw_body("/1.0/storage-pools/default/volumes/custom/v", body) == []
    assert policy_keys_in_raw_body("/1.0/instances/web-01", None) == []
    assert policy_keys_in_raw_body("/1.0/instances/web-01", {"config": {}}) == []


def test_a_percent_encoded_path_cannot_hide_the_write():
    """One parser with the traversal guard, so an encoded spelling cannot answer this
    classifier differently than it answers that one."""
    body = {"config": {"snapshots.schedule": "@daily"}}
    assert policy_keys_in_raw_body("/1.0/%69nstances/web-01", body) == ["snapshots.schedule"]
    assert policy_keys_in_raw_body("/1.0/%2569nstances/web-01", body) == ["snapshots.schedule"]


def test_the_observation_did_not_reprice_the_capability():
    """Non-regression. These keys must still cost plain `write`: making them cost
    `instance.exec` would be an undeclared refusal shipped as a side effect of adding a log
    line, and it would break the product that legitimately writes them."""
    for key in POLICY_CONFIG_KEYS:
        assert config_reaches_inside({key: "@daily"}, None) is None
    # and the real guest-reaching keys still cost what they did
    assert config_reaches_inside({"raw.lxc": "x"}, None) == "instance.exec"


def test_the_observer_cannot_refuse():
    """The load-bearing assertion of this file.

    If someone later turns this into a gate, they have to change the name and the docstring that
    both say it observes. Enforcement belongs behind an explicit never-inherited capability
    seeded for the owning application type, not behind a function called `_observe`.
    """
    from app.routers.integrations import _observe_policy_key_write

    tree = ast.parse(inspect.getsource(_observe_policy_key_write))
    raises = [node for node in ast.walk(tree) if isinstance(node, ast.Raise)]
    assert raises == [], "the observer raises; it is a gate now and is misnamed"

    # And it tolerates every shape the call sites can hand it, including the empty one.
    assert _observe_policy_key_write({}, [], cluster_id="nuc2", project=None, target="x") is None


def test_both_proxy_paths_call_the_observer():
    """A helper nothing calls is the defect this repo keeps finding. Both paths, because the
    structured proxy and the raw proxy are separate surfaces and only one was ever guarded."""
    from app.routers import integrations

    source = inspect.getsource(integrations)
    assert source.count("_observe_policy_key_write(") >= 3, "definition plus two call sites"
    assert "policy_keys_in_raw_body(request.path, request.body)" in source
    assert "policy_keys_in_config((request.params or {}).get(\"config\"))" in source


def test_the_action_is_named_in_the_audit_catalogue():
    """An unregistered action silently defaults to LXD_INFRA/MEDIUM, so it would be recorded
    under a category nobody chose and be unalertable."""
    assert ACTION in ACTION_CATEGORIES
    assert ACTION in HIGH_SEVERITY_ACTIONS
