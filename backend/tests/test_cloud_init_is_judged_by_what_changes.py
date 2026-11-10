"""
A key write to an existing instance or profile pays instance.exec for cloud-init data only when it
changes a value.

LXD reruns cloud-init when an expanded cloud-init value differs after an update (driver_lxc.go Update
diffs the expanded config, needsNewInstanceID reads the diff). A value the object already holds
changes nothing, so the integrator's read-modify-write PUT keeps working on an instance the dashboard
built with user-data. A new, changed or removed value costs exec, and so does any cloud-init key in a
create or a copy. The object is read only for such a body from a key without exec; a read that
fails sends nothing and leaves a record. The cluster is a recording fake (tests/_cloud_init_fakes.py).
"""

from typing import Any, Dict, List, Optional, Tuple

import pytest
import requests
from fastapi import HTTPException

from app.core.api_key_capabilities import APP_TYPE_CAPABILITIES
from app.core.blocking import BlockingPoolFull, BlockingTimeout
from app.core.cloud_init_config import changed_cloud_init_keys, cloud_init_compared_at, proxy_cloud_init_compared_at
from app.mcp import tools_write
from app.routers import integrations
from app.services.lxd import health, limited_write_reach
from tests._cloud_init_fakes import ConfigSession, holding
from tests._limited_write_fakes import FakeClient, _no_webhook_events, mcp_call, proxy_call  # noqa: F401
from tests._raw_proxy_fakes import CLUSTER_ID, ENDPOINT, _clean_breaker, call_raw_proxy, fence_calls, key  # noqa: F401

USER_DATA = "#cloud-config\npackages: [nginx]\n"
OTHER = "#cloud-config\nruncmd: [true]\n"
UD, VD, CUD, CVD = "user.user-data", "user.vendor-data", "cloud-init.user-data", "cloud-init.vendor-data"

STAR_NO_EXEC = key(["*"], capabilities=["inventory.read", "lxd.proxy", "instance.files"])
NARROW_NO_EXEC = key(["tenant-a"], capabilities=["inventory.read", "lxd.proxy"])
ORCAHUB = key(["*"], capabilities=sorted(APP_TYPE_CAPABILITIES["orcahub"]))
WITH_EXEC = key(["*"], capabilities=["inventory.read", "lxd.proxy", "instance.exec"])

# Every raw-proxy write to an existing object, with the object it is judged against.
UPDATES: List[Tuple[str, str, str, Dict[str, Any]]] = [
    ("PUT", "/1.0/instances/web-1", "/instances/web-1",
     {"config": {UD: USER_DATA, "limits.cpu": "2"}, "devices": {}, "profiles": ["default"]}),
    ("PATCH", "/1.0/instances/web-1", "/instances/web-1", {"config": {CUD: USER_DATA}}),
    ("PATCH", "/1.0/containers/web-1", "/containers/web-1", {"config": {VD: USER_DATA}}),
    ("PATCH", "/1.0/virtual-machines/vm-1", "/virtual-machines/vm-1", {"config": {CVD: USER_DATA}}),
    ("POST", "/1.0/instances/web-1", "/instances/web-1", {"name": "web-1", "project": "p2", "config": {UD: USER_DATA}}),
    ("PUT", "/1.0/profiles/default", "/profiles/default", {"config": {UD: USER_DATA}, "devices": {}}),
    ("PATCH", "/1.0/profiles/gpu", "/profiles/gpu", {"config": {CUD: USER_DATA, VD: USER_DATA}}),
    ("PATCH", "/1.0/%69nstances/web-1/", "/instances/web-1", {"config": {CUD: USER_DATA}}),
    ("PUT", "/1.0/PROFILES/default", "/profiles/default", {"config": {UD: USER_DATA}}),
]
UPDATE_IDS = ["put", "patch", "patch-containers", "patch-vm", "move", "profile-put", "profile-patch-two-keys",
              "encoded-path", "upper-family"]


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """What the handlers write to the audit trail, as the real _audit_key_action receives it."""
    recorded: List[Dict[str, Any]] = []
    monkeypatch.setattr(integrations, "_audit_key_action", lambda api_key, **fields: recorded.append(fields))
    return recorded


def _refusals(audits: List[Dict[str, Any]]) -> List[Optional[str]]:
    return [record["details"].get("refused") for record in audits if record["result"] == "failure"]


def _as_held(body: Dict[str, Any]) -> Dict[str, Any]:
    """The object's own config, already holding every value the body sends."""
    return dict(body["config"])


# -- what is compared, and how -------------------------------------------------------------------


@pytest.mark.parametrize(
    "config,current,changed",
    [
        ({UD: USER_DATA}, {UD: USER_DATA}, []),
        ({UD: USER_DATA, "limits.cpu": "4"}, {UD: USER_DATA, "limits.cpu": "2"}, []),
        ({UD: USER_DATA}, {}, [UD]),
        ({UD: USER_DATA}, {UD: OTHER}, [UD]),
        ({UD: USER_DATA}, {CUD: USER_DATA}, [UD]),
        ({UD: USER_DATA, CVD: USER_DATA}, {UD: USER_DATA, CVD: OTHER}, [CVD]),
        ({UD: ""}, {}, []),
        ({UD: None}, {}, []),
        ({UD: ""}, {UD: OTHER}, [UD]),
        ({UD: None}, {UD: OTHER}, [UD]),
        ({UD: 1}, {UD: "1"}, [UD]),
        ({UD: ["x"]}, {}, [UD]),
        ({"User.User-Data": USER_DATA}, {UD: USER_DATA}, ["User.User-Data"]),
        ({"cloud-init.network-config": USER_DATA}, {}, []),
    ],
    ids=["same", "same-beside-a-resize", "new", "changed", "moved-between-keys", "one-of-two", "empty-on-absent",
         "null-on-absent", "removed", "removed-by-null", "not-a-string", "a-list", "other-spelling",
         "network-config"],
)
def test_a_value_is_compared_the_way_lxd_diffs_a_config(
    config: Dict[str, Any], current: Dict[str, Any], changed: List[str]
) -> None:
    assert changed_cloud_init_keys(config, current) == changed


@pytest.mark.parametrize("method,path,target,body", UPDATES, ids=UPDATE_IDS)
def test_an_update_is_compared_with_the_object_it_writes(method: str, path: str, target: str, body: Any) -> None:
    assert cloud_init_compared_at(path, method) == target


@pytest.mark.parametrize(
    "method,path",
    [("POST", "/1.0/instances"), ("POST", "/1.0/virtual-machines"), ("POST", "/1.0/profiles"),
     ("POST", "/1.0/profiles/default"), ("POST", "/1.0/instances/web-1/snapshots"), ("GET", "/1.0/instances/web-1"),
     ("PUT", "/1.0/instances/web-1/state")],
    ids=["create", "create-vm", "profile-create", "profile-rename", "snapshot", "read", "state"],
)
def test_a_create_or_another_write_has_nothing_to_compare(method: str, path: str) -> None:
    assert cloud_init_compared_at(path, method) is None


def test_proxy_compares_an_update_with_its_instance() -> None:
    assert proxy_cloud_init_compared_at("update_instance", {"name": "web"}) == "/instances/web"
    assert proxy_cloud_init_compared_at("update_instance", {"name": "a?project=b"}) == "/instances/a%3Fproject%3Db"
    assert proxy_cloud_init_compared_at("create_instance", {"name": "web"}) is None
    assert proxy_cloud_init_compared_at("update_instance", {}) is None
    with pytest.raises(HTTPException) as refused:
        proxy_cloud_init_compared_at("update_instance", {"name": ".."})
    assert refused.value.status_code == 400


# -- the raw proxy -----------------------------------------------------------------------------------


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize("api_key", [STAR_NO_EXEC, ORCAHUB], ids=["star", "orcahub"])
@pytest.mark.parametrize("method,path,target,body", UPDATES, ids=UPDATE_IDS)
async def test_an_unchanged_value_is_sent_without_exec(
    method: str, path: str, target: str, body: Dict[str, Any], api_key: Dict[str, Any],
    audits: List[Dict[str, Any]],
) -> None:
    session = holding(_as_held(body), target)

    outcome, session = await call_raw_proxy(api_key, method, path, body=body, session=session)

    assert outcome.status_code == 200, outcome
    assert session.reads == [(ENDPOINT + target, "default")]
    assert session.writes == [(method, body)]
    assert _refusals(audits) == []


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize(
    "held",
    [{}, {UD: OTHER, CUD: OTHER, VD: OTHER, CVD: OTHER}, {"limits.cpu": "2"}],
    ids=["new-or-removed-then-added", "changed", "other-keys-only"],
)
@pytest.mark.parametrize("method,path,target,body", UPDATES, ids=UPDATE_IDS)
async def test_a_new_or_changed_value_costs_exec_after_one_read(
    method: str, path: str, target: str, body: Dict[str, Any], held: Dict[str, Any], audits: List[Dict[str, Any]]
) -> None:
    outcome, session = await call_raw_proxy(STAR_NO_EXEC, method, path, body=body, session=holding(held, target))

    assert isinstance(outcome, HTTPException) and outcome.status_code == 403 and "instance.exec" in outcome.detail
    assert session.reads == [(ENDPOINT + target, "default")] and session.writes == []
    assert _refusals(audits) == ["capability_not_granted"]


@pytest.mark.usefixtures("fence_calls")
async def test_removing_a_value_is_a_change(audits: List[Dict[str, Any]]) -> None:
    body = {"config": {UD: ""}}
    outcome, session = await call_raw_proxy(STAR_NO_EXEC, "PATCH", "/1.0/instances/web-1", body=body,
                                            session=holding({UD: OTHER}, "/instances/web-1"))

    assert isinstance(outcome, HTTPException) and outcome.status_code == 403
    assert session.writes == []


@pytest.mark.usefixtures("fence_calls")
async def test_the_instances_own_config_is_compared_not_its_profiles(audits: List[Dict[str, Any]]) -> None:
    """A value a profile gives the instance is not its own, so writing it locally is new."""
    session = ConfigSession({("default", "/instances/web-1"): {
        "config": {}, "expanded_config": {UD: USER_DATA}, "profiles": ["with-user-data"]}})

    outcome, session = await call_raw_proxy(STAR_NO_EXEC, "PATCH", "/1.0/instances/web-1",
                                            body={"config": {UD: USER_DATA}}, session=session)

    assert isinstance(outcome, HTTPException) and outcome.status_code == 403
    assert session.writes == []


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize(
    "body",
    [{"name": "c1", "source": {"type": "image", "alias": "ubuntu/24.04"}, "config": {UD: USER_DATA}},
     {"name": "c1", "source": {"type": "copy", "source": "web-1"}, "config": {UD: USER_DATA}}],
    ids=["image", "copy-with-the-sources-value"],
)
async def test_a_create_costs_exec_without_a_read(body: Dict[str, Any], audits: List[Dict[str, Any]]) -> None:
    session = holding({UD: USER_DATA}, "/instances/web-1")

    outcome, session = await call_raw_proxy(STAR_NO_EXEC, "POST", "/1.0/instances", body=body, session=session)

    assert isinstance(outcome, HTTPException) and outcome.status_code == 403
    assert session.calls == []
    assert _refusals(audits) == ["capability_not_granted"]


@pytest.mark.usefixtures("fence_calls")
async def test_a_key_holding_exec_is_never_read_for(audits: List[Dict[str, Any]]) -> None:
    body = {"config": {UD: USER_DATA}}
    outcome, session = await call_raw_proxy(WITH_EXEC, "PATCH", "/1.0/instances/web-1", body=body,
                                            session=holding({}, "/instances/web-1"))

    assert outcome.status_code == 200, outcome
    assert session.reads == [] and session.writes == [("PATCH", body)]


@pytest.mark.usefixtures("fence_calls")
async def test_the_read_is_made_in_the_authorised_project(audits: List[Dict[str, Any]]) -> None:
    body = {"config": {UD: USER_DATA}}
    session = holding({UD: USER_DATA}, "/instances/web-1", project="tenant-a")

    outcome, session = await call_raw_proxy(NARROW_NO_EXEC, "PATCH", "/1.0/instances/web-1?project=tenant-b",
                                            project="tenant-a", body=body, session=session)

    assert outcome.status_code == 200, outcome
    assert session.reads == [(ENDPOINT + "/instances/web-1", "tenant-a")]
    assert session.writes == [("PATCH", body)] and session.calls[-1][1].endswith("?project=tenant-a")


# The integrator's read-modify-write PUT for limit, GPU and volume edits: it reads the instance and
# sends back its whole editable form.
ORCAHUB_EDITABLE = {
    "architecture": "aarch64",
    "config": {"limits.cpu": "2", "limits.memory": "2GiB", "volatile.cloud-init.instance-id": "5b1e0a4c",
               "volatile.uuid": "5b1e0a4c-0000-4000-8000-000000000002"},
    "devices": {"root": {"type": "disk", "path": "/", "pool": "default", "size": "20GiB"}},
    "profiles": ["default"],
    "description": "Plan - Order 1",
    "ephemeral": False,
    "stateful": False,
}


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize("held_key", [UD, CUD], ids=["dashboard-create", "dashboard-rebuild"])
async def test_the_integrators_round_trip_put_is_sent(held_key: str, audits: List[Dict[str, Any]]) -> None:
    """The dashboard's create writes user.user-data and its rebuild cloud-init.user-data."""
    held = {**ORCAHUB_EDITABLE, "config": {**ORCAHUB_EDITABLE["config"], held_key: USER_DATA}}
    session = ConfigSession({("cust-buyer", "/instances/web-9"): held})
    body = {**held, "config": {**held["config"], "limits.cpu": "4", "limits.memory": "4GiB"}}

    outcome, session = await call_raw_proxy(ORCAHUB, "PUT", "/1.0/instances/web-9", project="cust-buyer",
                                            body=body, session=session)

    assert outcome.status_code == 200, outcome
    assert session.reads == [(ENDPOINT + "/instances/web-9", "cust-buyer")]
    assert session.writes == [("PUT", body)]
    assert _refusals(audits) == []


NOT_READ = [
    (None, 404, "not_found"),
    (500, 502, "cluster_unavailable"),
    (503, 502, "cluster_unavailable"),
    (403, 502, "cluster_unavailable"),
    (requests.exceptions.ReadTimeout("read timed out, host 10.0.0.9"), 504, "timeout"),
    (requests.exceptions.ConnectTimeout("connect timed out, host 10.0.0.9"), 504, "timeout"),
    (requests.exceptions.ConnectionError("refused, host 10.0.0.9"), 502, "cluster_unavailable"),
]
NOT_READ_IDS = ["missing", "500", "503", "403", "read-timeout", "connect-timeout", "unreachable"]


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize("fail,status,code", NOT_READ, ids=NOT_READ_IDS)
async def test_a_read_that_fails_sends_nothing_and_is_recorded(
    fail: Any, status: int, code: str, audits: List[Dict[str, Any]]
) -> None:
    session = ConfigSession({}, fail=fail)

    outcome, session = await call_raw_proxy(STAR_NO_EXEC, "PATCH", "/1.0/instances/web-1",
                                            body={"config": {UD: USER_DATA}}, session=session)

    assert not isinstance(outcome, HTTPException), outcome
    assert outcome.status_code == status and "The write was not sent" in outcome.error
    assert "10.0.0.9" not in outcome.error
    assert len(session.reads) == 1 and session.writes == []
    assert _refusals(audits) == ["cloud_init_not_compared"]
    assert not health.is_tripped(CLUSTER_ID)


@pytest.mark.usefixtures("fence_calls")
async def test_an_answer_without_a_config_sends_nothing(audits: List[Dict[str, Any]]) -> None:
    session = ConfigSession({("default", "/instances/web-1"): {"config": "not a map"}})

    outcome, session = await call_raw_proxy(STAR_NO_EXEC, "PATCH", "/1.0/instances/web-1",
                                            body={"config": {UD: USER_DATA}}, session=session)

    assert outcome.status_code == 502 and session.writes == []
    assert _refusals(audits) == ["cloud_init_not_compared"]


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize("raised,status", [(BlockingTimeout("checks", 5.0), 504), (BlockingPoolFull("checks", 9), 503)],
                         ids=["budget", "pool-full"])
async def test_a_read_that_does_not_finish_sends_nothing(
    raised: Exception, status: int, monkeypatch: pytest.MonkeyPatch, audits: List[Dict[str, Any]]
) -> None:
    async def _never(*_args: Any, **_kwargs: Any) -> None:
        raise raised

    monkeypatch.setattr(limited_write_reach, "run_blocking", _never)

    outcome, session = await call_raw_proxy(STAR_NO_EXEC, "PATCH", "/1.0/instances/web-1",
                                            body={"config": {UD: USER_DATA}}, session=holding({}, "/instances/web-1"))

    assert outcome.status_code == status and session.calls == []
    assert _refusals(audits) == ["cloud_init_not_compared"]


# -- /proxy update_instance, and MCP lxd_update_instance which calls it ------------------------------------


def _client(held: Optional[Dict[str, Any]], project: str = "default", fail: Any = None) -> FakeClient:
    records = {} if held is None else {(project, "/instances/web"): {"name": "web", "config": held}}
    return FakeClient(ConfigSession(records, fail=fail))


async def test_proxy_saves_an_unchanged_value_without_exec(audits: List[Dict[str, Any]]) -> None:
    params = {"name": "web", "config": {UD: USER_DATA, "limits.cpu": "4"}}

    outcome, client = await proxy_call("update_instance", "default", params, api_key=STAR_NO_EXEC,
                                       client=_client({UD: USER_DATA, "limits.cpu": "2"}))

    assert not isinstance(outcome, HTTPException) and outcome.success, outcome
    assert client.api.session.reads == [(client.api._api_endpoint + "/instances/web", "default")]
    assert [(verb, name, body["config"]) for verb, name, body in client.sent] == [
        ("save", "web", {UD: USER_DATA, "limits.cpu": "4"})
    ]
    assert _refusals(audits) == []


@pytest.mark.parametrize("held", [{}, {UD: OTHER}, {UD: USER_DATA, CUD: OTHER}], ids=["new", "changed", "one-of-two"])
async def test_proxy_refuses_a_new_or_changed_value_without_exec(
    held: Dict[str, Any], audits: List[Dict[str, Any]]
) -> None:
    params = {"name": "web", "config": {UD: USER_DATA, CUD: USER_DATA}}

    outcome, client = await proxy_call("update_instance", "default", params, api_key=STAR_NO_EXEC,
                                       client=_client(held))

    assert isinstance(outcome, HTTPException) and outcome.status_code == 403 and "instance.exec" in outcome.detail
    assert client.sent == [] and client.api.session.writes == []
    assert _refusals(audits) == ["capability_not_granted"]


async def test_proxy_reads_the_instance_in_the_requests_project(audits: List[Dict[str, Any]]) -> None:
    outcome, client = await proxy_call("update_instance", "tenant-a", {"name": "web", "config": {UD: USER_DATA}},
                                       api_key=NARROW_NO_EXEC, client=_client({UD: USER_DATA}, project="tenant-a"))

    assert not isinstance(outcome, HTTPException) and outcome.success, outcome
    assert client.api.session.reads == [(client.api._api_endpoint + "/instances/web", "tenant-a")]
    assert [verb for verb, *_ in client.sent] == ["save"]


@pytest.mark.parametrize("fail,code", [(None, "not_found"), (500, "cluster_unavailable"),
                                       (requests.exceptions.ReadTimeout("host 10.0.0.9"), "timeout")],
                         ids=["missing", "500", "timeout"])
async def test_proxy_sends_nothing_when_the_read_fails(fail: Any, code: str, audits: List[Dict[str, Any]]) -> None:
    outcome, client = await proxy_call("update_instance", "default", {"name": "web", "config": {UD: USER_DATA}},
                                       api_key=STAR_NO_EXEC, client=_client(None, fail=fail))

    assert not isinstance(outcome, HTTPException) and outcome.success is False, outcome
    assert outcome.error_code == code and "The write was not sent" in outcome.error and "10.0.0.9" not in outcome.error
    assert client.sent == [] and client.api.session.writes == []
    assert _refusals(audits) == ["cloud_init_not_compared"]
    assert audits[-1]["details"]["cloud_init_keys"] == [UD]


async def test_proxy_sends_nothing_when_the_cluster_is_not_connected(audits: List[Dict[str, Any]]) -> None:
    from types import SimpleNamespace

    service = SimpleNamespace(connection_manager=SimpleNamespace(get_client=lambda _cluster: None,
                                                                 get_project_client=lambda *_a, **_k: None))
    outcome = await integrations.proxy_operation(
        request=integrations.ProxyRequest(cluster_id=CLUSTER_ID, operation="update_instance", project="default",
                                          params={"name": "web", "config": {UD: USER_DATA}}),
        api_key=STAR_NO_EXEC, lxd_service=service, _=None,
    )

    assert outcome.success is False and outcome.error_code == "cluster_unavailable"
    assert "The write was not sent" in outcome.error
    assert _refusals(audits) == ["cloud_init_not_compared"]


async def test_proxy_never_reads_for_a_key_holding_exec_or_a_create(audits: List[Dict[str, Any]]) -> None:
    exec_key, client = await proxy_call("update_instance", "default", {"name": "web", "config": {UD: USER_DATA}},
                                        api_key=WITH_EXEC, client=_client({}))
    assert exec_key.success and client.api.session.calls == []

    create, client = await proxy_call("create_instance", "default",
                                      {"name": "c1", "source": {"type": "image", "alias": "a"}, "config": {UD: "x"}},
                                      api_key=STAR_NO_EXEC, client=_client({}))
    assert isinstance(create, HTTPException) and create.status_code == 403 and client.api.session.calls == []


async def test_mcp_update_with_an_unchanged_value_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client({CUD: USER_DATA})
    result = await mcp_call(tools_write, "lxd_update_instance", client, STAR_NO_EXEC, monkeypatch,
                            cluster_id=CLUSTER_ID, name="web", project="default",
                            config={CUD: USER_DATA, "limits.memory": "4GiB"})

    assert result.is_error is False, result.content
    assert [verb for verb, *_ in client.sent] == ["save"]
