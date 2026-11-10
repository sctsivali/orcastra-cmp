"""A key-authenticated write of the upstream product's snapshot-schedule keys is recorded.

`snapshots.schedule` decides whether the console treats an instance as owned by the metered
snapshot product sold upstream, which decides whether an operator's own snapshot actions on it are
refused. The proxy prices an instance-config change by what it reaches inside the guest
(`config_reaches_inside`); these keys reach another product instead, so they cost plain `write`.
The raw proxy inspects no body at all.

The write is observed and still sent. Which callers legitimately set these keys cannot be known
from this repository, and enforcing on a guess would break the paid product. A later refusal can
be fitted to the record. The console's own create paths refuse the keys, since no
user-authenticated caller needs them (test_the_create_form_cannot_set_a_snapshot_schedule.py).
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
    """The control. Reporting every config change would bury the policy keys."""
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
    """The classifier shares the traversal guard's parser, so an encoded spelling reads the
    same to both."""
    body = {"config": {"snapshots.schedule": "@daily"}}
    assert policy_keys_in_raw_body("/1.0/%69nstances/web-01", body) == ["snapshots.schedule"]
    assert policy_keys_in_raw_body("/1.0/%2569nstances/web-01", body) == ["snapshots.schedule"]


def test_the_observation_did_not_reprice_the_capability():
    """These keys still cost plain `write`. Pricing them as `instance.exec` would refuse the
    product that legitimately writes them."""
    for key in POLICY_CONFIG_KEYS:
        assert config_reaches_inside({key: "@daily"}, None) is None
    # Keys that reach inside the guest still cost `instance.exec`.
    assert config_reaches_inside({"raw.lxc": "x"}, None) == "instance.exec"


def test_the_observer_cannot_refuse():
    """The observer contains no `raise`.

    Enforcement belongs behind an explicit never-inherited capability seeded for the owning
    application type.
    """
    from app.routers.integrations import _observe_policy_key_write

    tree = ast.parse(inspect.getsource(_observe_policy_key_write))
    raises = [node for node in ast.walk(tree) if isinstance(node, ast.Raise)]
    assert raises == [], "the observer raises; it is a gate now and is misnamed"

    # And it tolerates every shape the call sites can hand it, including the empty one.
    assert _observe_policy_key_write({}, [], cluster_id="nuc2", project=None, target="x") is None


def test_both_proxy_paths_call_the_observer():
    """The structured proxy and the raw proxy are separate surfaces; each calls the observer."""
    from app.routers import integrations

    source = inspect.getsource(integrations)
    assert source.count("_observe_policy_key_write(") >= 3, "definition plus two call sites"
    assert "policy_keys_in_raw_body(request.path, request.body)" in source
    assert "policy_keys_in_config((request.params or {}).get(\"config\"))" in source


def test_the_action_is_named_in_the_audit_catalogue():
    """An unregistered action falls back to LXD_INFRA/MEDIUM instead of its own category."""
    assert ACTION in ACTION_CATEGORIES
    assert ACTION in HIGH_SEVERITY_ACTIONS
