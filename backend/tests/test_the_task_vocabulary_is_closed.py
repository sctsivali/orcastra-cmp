"""The journal's vocabulary is closed: every action it can write is one the catalogue knows.

A task row's `action` is the audit action, so the panel, the audit log and the events stream
name one thing one way. An action the vocabulary can produce that the catalogue has never
heard of would show in the panel with no category and no severity. The lookup tables are
pinned here too, because each one is a place a typo turns into a row that quietly says the
wrong thing.
"""

import pytest

from app.services.activity_logger import ACTION_CATEGORIES
from app.services.task_journal.vocabulary import (
    DESCRIPTION_TO_ACTION,
    LIFECYCLE_TO_ACTION,
    OPEN,
    TASK_ACTIONS,
    TERMINAL,
    requestor_kind,
    resource_from_operation,
    status_from_lxd,
)

# Operations LXD runs that the dashboard never records under an audit action of its own: an
# exec or console session is audited as terminal.session.create / console.session.create, and
# the rest have no dashboard control. They are journal vocabulary only, and a description that
# maps to an action outside this set and outside the catalogue fails the ratchet below.
LXD_ONLY_ACTIONS = frozenset({
    "instance.console",
    "instance.exec",
    "instance.pause",
    "instance.rename",
    "instance.resume",
    "instance.snapshot.rename",
    "instance.update",
})


def test_every_task_action_is_in_the_catalogue():
    missing = sorted(a for a in TASK_ACTIONS if a not in ACTION_CATEGORIES)
    assert not missing, missing


def test_every_description_maps_to_a_catalogued_or_lxd_only_action():
    unknown = sorted(
        a for a in set(DESCRIPTION_TO_ACTION.values())
        if a not in ACTION_CATEGORIES and a not in LXD_ONLY_ACTIONS
    )
    assert not unknown, unknown
    stale = sorted(a for a in LXD_ONLY_ACTIONS if a in ACTION_CATEGORIES)
    assert not stale, f"{stale} are catalogued now; drop them from LXD_ONLY_ACTIONS"


def test_every_lifecycle_action_is_catalogued_or_lxd_only():
    unknown = sorted(
        a for a in set(LIFECYCLE_TO_ACTION.values())
        if a not in ACTION_CATEGORIES and a not in LXD_ONLY_ACTIONS
    )
    assert not unknown, unknown


def test_the_two_status_sets_partition_the_vocabulary():
    assert TERMINAL == {"success", "failure", "cancelled", "lost"}
    assert OPEN == {"pending", "running"}
    assert not (TERMINAL & OPEN)


@pytest.mark.parametrize(
    "word,code,expected",
    [
        ("Pending", 100, "pending"),
        ("Running", 103, "running"),
        ("Success", 200, "success"),
        ("Failure", 400, "failure"),
        ("Cancelled", 401, "cancelled"),
        (None, None, "running"),
        ("cancelled", None, "cancelled"),
        ("Bogus", None, "running"),
        ("Running", 200, "success"),
    ],
)
def test_status_from_lxd_table(word, code, expected):
    assert status_from_lxd(word, code) == expected


def test_resource_from_operation_strips_the_project_query():
    assert resource_from_operation({"instances": ["/1.0/instances/web-01?project=cust-a"]}) == (
        "instance", "web-01",
    )
    assert resource_from_operation({"containers": ["/1.0/containers/c1"]}) == ("instance", "c1")
    assert resource_from_operation({"images": ["/1.0/images/abc123"]}) == ("image", "abc123")
    assert resource_from_operation({}) == (None, None)
    assert resource_from_operation(None) == (None, None)


@pytest.mark.parametrize(
    "requestor,expected",
    [
        ({"protocol": "unix", "username": "root"}, ("external", "cli")),
        ({"protocol": "tls", "username": "ABCDEF"}, ("system", "events")),
        ({"protocol": "tls", "username": "someone-else"}, ("external", "events")),
        ({"protocol": "oidc", "username": "a@b"}, ("external", "events")),
        (None, ("system", "events")),
    ],
)
def test_requestor_kind_table(requestor, expected):
    assert requestor_kind(requestor, frozenset({"abcdef"})) == expected
