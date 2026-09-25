"""The profiles a project-limited key's write applies are held to the host-reach rule, as its body is.

A profile's config and devices land on the instance as the instance's own do, and `default` is in
every project-limited key's reach. A privileged or passthrough profile an administrator left in a
project such a key can use would carry it past its bound, so the profiles are read where LXD
resolves them and judged before the write is sent. A create that names no profile gets LXD's
`default`, so that one is read.

Ported from the RC4-hotfix2 line (18db202) and adapted to this line: the plan is typed steps
rather than tuples, an instance name is read off the canonical path (fully decoded, as the sent URL
is built), and the dashboard's two create routes are not here. On this line the dashboard keeps its
own allowlist (services/instance_security_policy), which judges the effective payload.
"""

from typing import Any, Dict

import pytest
from fastapi import HTTPException

from app.core.limited_key_writes import (
    CarriedFrom,
    ImageApplied,
    NamedProfiles,
    limited_write_plan,
    profile_reach,
)
from tests._limited_write_fakes import (  # noqa: F401 - fixtures are registered by import
    FLEET,
    NARROW,
    TWO,
    WILDCARD,
    FakeClient,
    ProfileSession,
    ProjectSession,
    _no_webhook_events,
    proxy_call,
    raw_write,
)
from tests._raw_proxy_fakes import _clean_breaker, key  # noqa: F401

# note: the capabilities are granted so the limited-key rule is what refuses, not the price of the
# guest capability a host-reaching config also carries on /proxy.
TWO_WITH_GUEST = key(["tenant-a", "tenant-b"], capabilities=["lxd.proxy", "instance.exec", "instance.files"])

# -- which reads a write depends on -----------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,body,plan",
    [
        ("POST", "/1.0/instances", {"name": "c1"}, [NamedProfiles(("default",), "p")]),
        ("POST", "/1.0/instances", {"name": "c1", "profiles": ["web", "priv"]}, [NamedProfiles(("web", "priv"), "p")]),
        ("POST", "/1.0/instances", {"name": "c1", "profiles": []}, [NamedProfiles((), "p")]),
        ("POST", "/1.0/instances?project=x", {"name": "c1"}, [NamedProfiles(("default",), "p")]),
        ("POST", "/1.0/instances", {"name": "c1", "source": {"type": "copy", "source": "web", "project": "q"}},
         [CarriedFrom("web", None, "q", "p", True, True)]),
        ("POST", "/1.0/instances", {"name": "c1", "source": {"type": "copy", "source": "web/s0"}},
         [CarriedFrom("web", "s0", "p", "p", True, True)]),
        ("POST", "/1.0/instances", {"name": "c1", "source": {"type": "copy", "source": "web/s0"}, "profiles": []},
         [NamedProfiles((), "p"), CarriedFrom("web", "s0", "p", "p", True, False)]),
        ("POST", "/1.0/instances", {"name": "c1", "source": {"type": "copy", "source": "web"},
                                    "config": {"b": "1", "a": "2"}, "devices": {"eth0": {}}},
         [CarriedFrom("web", None, "p", "p", True, True, ("a", "b"), ("eth0",))]),
        ("POST", "/1.0/instances", {"name": "c1", "source": {"type": "image", "alias": "ubuntu"}},
         [ImageApplied("p", "container", alias="ubuntu")]),
        ("POST", "/1.0/instances", {"name": "c1", "type": "virtual-machine",
                                    "source": {"type": "image", "alias": "ubuntu"}},
         [ImageApplied("p", "virtual-machine", alias="ubuntu")]),
        ("POST", "/1.0/instances", {"name": "c1", "source": {"type": "image", "fingerprint": "f0",
                                                             "server": "https://s.example"}},
         [ImageApplied("p", "container", fingerprint="f0", server="https://s.example")]),
        ("POST", "/1.0/instances", {"name": "c1", "source": {"type": "image", "alias": "ubuntu"}, "profiles": ["web"]},
         [NamedProfiles(("web",), "p")]),
        ("PATCH", "/1.0/instances/web", {"profiles": ["priv"]}, [NamedProfiles(("priv",), "p")]),
        ("PUT", "/1.0/instances/web", {"restore": "s0"}, [CarriedFrom("web", "s0", "p", "p", True, True)]),
        ("PATCH", "/1.0/instances/web", {"config": {"limits.cpu": "2"}}, []),
        ("PUT", "/1.0/instances/web/state", {"action": "stop"}, []),
        ("POST", "/1.0/instances/web", {"name": "renamed"}, []),
        ("POST", "/1.0/instances/web", {"project": "p", "profiles": ["priv"]}, [NamedProfiles(("priv",), "p")]),
        ("POST", "/1.0/instances/web", {"project": "q", "profiles": ["priv"]}, [NamedProfiles(("priv",), "q")]),
        ("POST", "/1.0/instances/web", {"project": "q"}, [CarriedFrom("web", None, "p", "q", False, True)]),
        ("POST", "/1.0/instances/w%65b", {"project": "q"}, [CarriedFrom("web", None, "p", "q", False, True)]),
        ("POST", "/1.0/instances/w%2565b", {"project": "q"}, [CarriedFrom("web", None, "p", "q", False, True)]),
        ("POST", "/1.0/instances/web/snapshots", {"name": "s1"}, []),
        ("PUT", "/1.0/images/abc", {"profiles": ["priv"]}, [NamedProfiles(("priv",), "p")]),
        ("POST", "/1.0/images", {"source": {"type": "instance", "name": "web"}, "profiles": ["priv"]},
         [NamedProfiles(("priv",), "p")]),
        ("PATCH", "/1.0/images/abc", {"public": True}, []),
        ("GET", "/1.0/instances", None, []),
        ("POST", "/1.0/profiles", {"name": "x"}, []),
    ],
)
def test_the_reads_a_limited_write_needs(method: str, path: str, body: Any, plan: list) -> None:
    assert limited_write_plan(path, method, body, "p") == plan


def test_the_first_reaching_profile_is_named_and_a_missing_one_is_left_to_lxd() -> None:
    read = FLEET.get
    assert profile_reach(read, ["default", "web"]) is None
    assert profile_reach(read, ["missing", "web"]) is None
    assert profile_reach(read, ["web", "priv", "disk"]) == "profile priv with config security.privileged"
    assert "unix-block" in (profile_reach(read, ["disk"]) or "")


# -- a project-limited key on the raw proxy and /proxy create --------------------------------------


async def _raw(api_key: Dict[str, Any], method: str, path: str, body: Dict[str, Any]) -> tuple:
    return await raw_write(method, path, "tenant-a", body, api_key=api_key, session=ProfileSession())


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/1.0/instances", {"name": "c1", "profiles": ["web", "priv"]}),
        ("PATCH", "/1.0/instances/web", {"profiles": ["disk"]}),
        ("PUT", "/1.0/instances/web", {"profiles": ["default", "priv"], "config": {}}),
    ],
)
async def test_a_narrow_key_cannot_apply_a_reaching_profile_through_the_raw_proxy(method, path, body) -> None:
    outcome, session = await _raw(NARROW, method, path, body)
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403
    assert "profile" in outcome.detail and "tenant-a" in outcome.detail
    assert not session.sent, "the write was sent"
    assert all(project == "tenant-a" for _m, _u, project in session.calls), "a profile was read outside the grant"


async def test_a_narrow_key_cannot_apply_a_reaching_profile_through_a_move() -> None:
    outcome, session = await _raw(NARROW, "POST", "/1.0/instances/web", {"migration": True, "pool": "p2",
                                                                          "profiles": ["priv"]})
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403
    assert [call[0] for call in session.calls] == ["GET"]


async def test_a_narrow_key_create_reads_default_and_goes_on_when_it_is_plain() -> None:
    outcome, session = await _raw(NARROW, "POST", "/1.0/instances", {"name": "c1"})
    assert outcome.status_code == 200
    assert [(verb, url.split("?")[0].rsplit("/", 1)[-1]) for verb, url, _ in session.calls] == [
        ("GET", "default"), ("POST", "instances")]


async def test_a_star_key_reads_no_profile() -> None:
    outcome, session = await _raw(WILDCARD, "POST", "/1.0/instances", {"name": "c1", "profiles": ["priv"]})
    assert outcome.status_code == 200 and [call[0] for call in session.calls] == ["POST"]


async def test_a_narrow_key_cannot_apply_a_reaching_profile_through_proxy_create() -> None:
    client = FakeClient(ProfileSession())
    outcome, _ = await proxy_call("create_instance", "tenant-a",
                                  {"name": "vm", "source": {"type": "none"}, "profiles": ["priv"]},
                                  api_key=NARROW, client=client)
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403 and "profile priv" in outcome.detail
    assert client.created == []


@pytest.mark.parametrize("api_key,params", [
    (NARROW, {"name": "vm", "source": {"type": "none"}}),
    (NARROW, {"name": "vm", "source": {"type": "none"}, "profiles": ["web", "missing"]}),
    (WILDCARD, {"name": "vm", "source": {"type": "none"}, "profiles": ["priv"]}),
])
async def test_proxy_create_goes_on_with_plain_profiles_or_a_star_key(api_key, params) -> None:
    client = FakeClient(ProfileSession())
    answer, _ = await proxy_call("create_instance", "tenant-a", params, api_key=api_key, client=client)
    assert answer.success is True and len(client.created) == 1


# -- a move to another project applies that project's profiles --------------------------------------


async def _move(body: Dict[str, Any], path: str = "/1.0/instances/web") -> tuple:
    return await raw_write("POST", path, "tenant-a", body, api_key=TWO)


async def test_a_move_naming_profiles_reads_them_in_the_target_project() -> None:
    outcome, session = await _move({"project": "tenant-b", "profiles": ["priv"]})
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403
    assert [(m, p) for m, _u, p in session.calls] == [("GET", "tenant-b")]


async def test_a_move_naming_no_profiles_judges_the_instances_own_in_the_target() -> None:
    outcome, session = await _move({"project": "tenant-b"})
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403
    reads = [(u.rsplit("/", 2)[-2], p) for m, u, p in session.calls]
    assert reads == [("instances", "tenant-a"), ("profiles", "tenant-b")] and not session.sent


@pytest.mark.parametrize("body", [{"project": "tenant-b", "profiles": []}, {"name": "renamed"},
                                  {"project": "tenant-a", "profiles": ["priv"]}])
async def test_a_move_that_applies_nothing_reaching_is_sent(body) -> None:
    outcome, session = await _move(body)
    assert outcome.status_code == 200 and session.calls[-1][0] == "POST"


async def test_a_move_of_an_encoded_name_reads_the_instance_that_is_moved() -> None:
    """The name is sent decoded, so w%65b is web: the read looks up web and refuses."""
    outcome, session = await _move({"project": "tenant-b"}, path="/1.0/instances/w%65b")
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403 and not session.sent
    assert session.calls[0][1].endswith("/instances/web")


async def test_a_move_whose_instance_cannot_be_read_is_refused() -> None:
    outcome, session = await _move({"project": "tenant-b"}, path="/1.0/instances/ghost")
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403
    assert "could not be read" in outcome.detail and not session.sent


async def test_a_legitimate_move_of_an_encoded_name_is_still_sent() -> None:
    outcome, session = await _move({"project": "tenant-b"}, path="/1.0/instances/%61pp")
    assert outcome.status_code == 200 and session.calls[-1][0] == "POST"
    assert session.calls[-1][1].split("?")[0].endswith("/instances/app")


# -- dev-only /proxy writes: update_instance and restore_snapshot --------------------------------


@pytest.mark.parametrize(
    "operation,params,why",
    [
        ("update_instance", {"name": "web", "profiles": ["default", "priv"]}, "profile priv"),
        ("update_instance", {"name": "web", "config": {"security.privileged": "true"}}, "security.privileged"),
        ("update_instance", {"name": "web", "devices": {"d": {"type": "unix-char", "source": "/dev/kvm"}}},
         "unix-char"),
        ("update_instance", {"name": "web", "devices": {"r": {"type": "disk", "source": "ceph:rbd/v", "path": "/r"}}},
         "outside a storage pool"),
        ("restore_snapshot", {"name": "app", "snapshot_name": "s0"}, "security.privileged"),
        ("restore_snapshot", {"name": "app", "snapshot_name": "gone"}, "could not be read"),
    ],
    ids=["update-profile", "update-config", "update-device", "update-ceph", "restore-privileged", "restore-unread"],
)
async def test_a_limited_proxy_write_is_judged_by_what_it_applies(operation, params, why) -> None:
    outcome, client = await proxy_call(operation, "tenant-b" if "profiles" in params else "tenant-a", params,
                                       api_key=TWO_WITH_GUEST)
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403 and why in outcome.detail
    assert client.sent == [], "the write reached the cluster"


@pytest.mark.parametrize(
    "operation,params",
    [
        ("update_instance", {"name": "web", "profiles": ["default", "priv"]}),
        ("update_instance", {"name": "web", "config": {"limits.cpu": "2", "security.privileged": "false"}}),
        ("update_instance", {"name": "web", "devices": {"g": {"type": "gpu"}}}),
    ],
)
async def test_a_limited_proxy_write_applying_nothing_reaching_is_sent(operation, params) -> None:
    answer, client = await proxy_call(operation, "tenant-a", params, api_key=TWO_WITH_GUEST)
    assert answer.success is True and [verb for verb, *_ in client.sent] == ["save"]


async def test_a_star_key_proxy_restore_reads_nothing() -> None:
    answer, client = await proxy_call("restore_snapshot", "tenant-a", {"name": "app", "snapshot_name": "s0"},
                                      api_key=WILDCARD)
    assert answer.success is True and client.sent == [("restore", "app", "s0")]
    assert client.api.session.calls == []
