"""The dot-name rules on the one-instance read, MCP orcastra_get_instance, snapshot create and rename bodies,
an image create's alias entries, and the helpers the caller-name ratchet names.

Every case goes through the real route (tests/_guest_gate_doors.py) or the real helper, with fakes that
record what would have left the process. Literal and double-encoded dot paths are sent through the ASGI app
with the path decoded once, the way uvicorn decodes it, because Starlette's TestClient decodes the path a
second time and cannot send either shape faithfully.
"""

import json
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

import pytest
from fastapi import HTTPException
from pylxd.client import _APINode

from app.core import cloud_init_config
from app.mcp import tools_inventory, tools_write
from app.services.lxd import proxy_path_params
from app.services.lxd.proxy_operations import PROXY_OPERATION_LEVELS
from app.services.lxd.instance_metrics import read_instance_metrics
from app.services.lxd.utils import find_instance_in_projects
from tests._guest_gate_doors import (  # noqa: F401
    EVERY, LEGACY, NARROW, NO_GUEST, ORCAHUB, SPELLING_IDS, TWO, Cluster, SnapshotClient, _http, _STATE,
    door_fixture, spell,
)
from tests._limited_write_fakes import FakeClient, _no_webhook_events  # noqa: F401
from tests._raw_proxy_fakes import CLUSTER_ID, ENDPOINT, RecordingSession, _clean_breaker, fence_calls  # noqa: F401
from tests.test_guest_gates_refuse_a_dot_name_on_every_door import _LxdRoot, _root_cluster

DOT_SPELLINGS = ["%2E", "%2e", "%2E%2E", "%2e%2e", ".%2E", "%2E.", ".%2e", "%2e."]
READ_KEYS = [(NARROW, "tenant-a"), (TWO, None), (EVERY, None), (LEGACY, None), (ORCAHUB, "cust-buyer")]
READ_KEY_IDS = ["narrow", "two-projects", "every", "legacy", "orcahub"]


def _no_request(cluster: Cluster) -> None:
    assert cluster.session.calls == [], cluster.session.calls


# The one-instance read through the TestClient (single-encoded spellings reach the route decoded once).


@pytest.mark.parametrize("with_cluster", [True, False], ids=["cluster-id", "every-cluster"])
@pytest.mark.parametrize("api_key,project", READ_KEYS, ids=READ_KEY_IDS)
@pytest.mark.parametrize("spelled", DOT_SPELLINGS)
def test_instance_read_refuses_every_dot_spelling(door: Any, monkeypatch: pytest.MonkeyPatch, spelled: str,
                                                     api_key: Dict[str, Any], project: Any, with_cluster: bool) -> None:
    cluster = _root_cluster(monkeypatch)
    params = {**({"project": project} if project else {}), **({"cluster_id": CLUSTER_ID} if with_cluster else {})}
    seen = door._send(api_key, cluster, "GET", f"/api/v1/integrations/instances/{spelled}", params=params)
    assert seen.status == 400 and "cannot name an LXD object" in seen.text, (seen.status, seen.text)
    assert "core.https_address" not in seen.text and seen.records == []
    _no_request(cluster)


# The same read with the path decoded once, as uvicorn hands it to the app.


async def _asgi_get(raw_path: bytes, query: bytes) -> Tuple[int, Any]:
    app = _http().app
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": unquote(raw_path.decode("ascii")), "raw_path": raw_path, "root_path": "", "query_string": query,
             "headers": [(b"host", b"testserver")], "client": ("127.0.0.1", 1), "server": ("testserver", 80)}
    sent: List[Dict[str, Any]] = []

    async def receive() -> Dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(body or b"{}")


@pytest.mark.parametrize("tail", [b"..", b".", b"%2E%2E", b"%2e", b".%2E"])
async def test_instance_read_refuses_a_literal_dot_path_as_uvicorn_decodes_it(
    door: Any, monkeypatch: pytest.MonkeyPatch, tail: bytes
) -> None:
    cluster = _root_cluster(monkeypatch)
    _STATE.update(key=NARROW, cluster=cluster)
    status, body = await _asgi_get(b"/api/v1/integrations/instances/" + tail,
                                   b"project=tenant-a&cluster_id=" + CLUSTER_ID.encode())
    assert status == 400 and "cannot name an LXD object" in body["detail"], (status, body)
    _no_request(cluster)


async def test_a_double_encoded_dot_name_is_a_literal_name_and_never_reads_the_root(
    door: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = _root_cluster(monkeypatch)
    _STATE.update(key=NARROW, cluster=cluster)
    status, body = await _asgi_get(b"/api/v1/integrations/instances/%252E%252E",
                                   b"project=tenant-a&cluster_id=" + CLUSTER_ID.encode())
    assert status == 404 and "core.https_address" not in json.dumps(body), (status, body)
    assert [url for _verb, url, _kw in cluster.session.calls] == [f"{ENDPOINT}/instances/%252E%252E?project=tenant-a"]


# MCP orcastra_get_instance.


@pytest.mark.parametrize("api_key,project", [(NARROW, "tenant-a"), (EVERY, None), (TWO, None), (LEGACY, None)],
                         ids=["narrow", "every", "two-projects", "legacy"])
@pytest.mark.parametrize("value", ["", ".", ".."], ids=repr)
async def test_mcp_instance_read_refuses_a_dot_name(door: Any, monkeypatch: pytest.MonkeyPatch, value: str,
                                                       api_key: Dict[str, Any], project: Any) -> None:
    cluster = _root_cluster(monkeypatch)
    arguments = {"name": value, "cluster_id": CLUSTER_ID, **({"project": project} if project else {})}
    seen = await door.mcp(tools_inventory, "orcastra_get_instance", api_key, cluster, **arguments)
    assert seen.status == 0 and "cannot name an LXD object" in seen.text, seen.text
    assert "core.https_address" not in seen.text
    _no_request(cluster)


@pytest.mark.parametrize("value", ["%2E%2E", " ..", ".. ", "..."])
async def test_mcp_instance_read_sends_a_near_dot_name_encoded_and_never_reads_the_root(
    door: Any, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    cluster = _root_cluster(monkeypatch)
    seen = await door.mcp(tools_inventory, "orcastra_get_instance", NARROW, cluster, name=value,
                          cluster_id=CLUSTER_ID, project="tenant-a")
    assert "core.https_address" not in seen.text, seen.text
    urls = [url for _verb, url, _kw in cluster.session.calls]
    assert len(urls) == 1 and not urls[0].startswith(f"{ENDPOINT}/?") and "/instances/" in urls[0], urls


# The helpers the caller-name ratchet names.


class _Projects:
    def __init__(self, calls: List[str]) -> None:
        self.calls = calls

    def all(self) -> List[Any]:
        self.calls.append("projects.all")
        return [type("P", (), {"name": "default"})(), type("P", (), {"name": "tenant-a"})()]


def _helper_client() -> Tuple[Any, List[str]]:
    calls: List[str] = []
    client = type("Client", (), {})()
    client.projects = _Projects(calls)
    client.api = _APINode(ENDPOINT, session=_LxdRoot(), timeout=(3, 8))
    return client, calls


@pytest.mark.parametrize("project_filter", [None, lambda project: project == "tenant-a"], ids=["no-filter", "filter"])
@pytest.mark.parametrize("project", [None, "tenant-a"], ids=["every-project", "one-project"])
@pytest.mark.parametrize("value", ["", ".", ".."], ids=repr)
def test_find_instance_in_projects_refuses_before_any_request(value: str, project: Any, project_filter: Any) -> None:
    client, calls = _helper_client()
    with pytest.raises(HTTPException) as refused:
        find_instance_in_projects(client, value, project, project_filter=project_filter)
    assert refused.value.status_code == 400 and "Instance name" in str(refused.value.detail)
    assert calls == [] and client.api.session.calls == []


@pytest.mark.parametrize("value,wire", [("web", "web"), ("%2E%2E", "%252E%252E"), ("a/..", "a%2F.."),
                                        ("x?project=other&", "x%3Fproject%3Dother%26")])
def test_find_instance_in_projects_encodes_every_other_name_as_one_segment(value: str, wire: str) -> None:
    client, _calls = _helper_client()
    assert find_instance_in_projects(client, value, "tenant-a") == (None, None)
    assert [url for _verb, url, _kw in client.api.session.calls] == [f"{ENDPOINT}/instances/{wire}?project=tenant-a"]


@pytest.mark.parametrize("value", ["", ".", ".."], ids=repr)
def test_the_dashboard_metrics_read_sends_nothing_for_a_dot_name(value: str) -> None:
    client, calls = _helper_client()
    with pytest.raises(Exception):
        read_instance_metrics(client, "nuc-1", value, "default")
    assert calls == [] and client.api.session.calls == []


@pytest.mark.parametrize("method", ["PUT", "PATCH", "POST"])
@pytest.mark.parametrize("path", ["/1.0/instances/%2E%2E", "/1.0/instances/.", "/1.0/profiles/%2e",
                                  "/1.0/virtual-machines/%252E%252E", "/1.0/containers/..%2F"])
def test_raw_cloud_init_compare_never_reads_a_dot_target(door: Any, method: str, path: str) -> None:
    seen = door.raw(NO_GUEST, method, path, body={"config": {"cloud-init.ssh-keys.root": "root:ssh-ed25519 AAAA"}})
    assert seen.status == 400 and seen.cluster.untouched and seen.records == [], (seen.status, seen.text)


@pytest.mark.parametrize("value", [".", ".."])
@pytest.mark.parametrize("key_name", ["cloud-init.user-data", "cloud-init.ssh-keys.root"])
def test_proxy_cloud_init_compare_refuses_a_dot_name_before_the_read(door: Any, value: str, key_name: str) -> None:
    seen = door.proxy(NO_GUEST, "update_instance", {"name": value, "config": {key_name: "x"}})
    assert seen.status == 400 and "cannot name an LXD object" in seen.text and seen.cluster.untouched
    assert seen.records == []
    with pytest.raises(HTTPException):
        cloud_init_config.proxy_cloud_init_compared_at("update_instance", {"name": value})


def test_proxy_update_with_an_empty_name_and_cloud_init_is_refused_and_untouched(door: Any) -> None:
    """Priced like a create (403 exec plus a record) instead of the 400 an empty name gets elsewhere."""
    seen = door.proxy(NO_GUEST, "update_instance", {"name": "", "config": {"cloud-init.ssh-keys.root": "x"}})
    assert seen.status in (400, 403) and seen.cluster.untouched, (seen.status, seen.text)


@pytest.mark.parametrize("operation", ["create_snapshot", "delete_snapshot"])
@pytest.mark.parametrize("value", ["", ".", ".."], ids=repr)
def test_assert_path_params_refuses_a_dot_snapshot_name(operation: str, value: str) -> None:
    with pytest.raises(HTTPException) as refused:
        proxy_path_params.assert_path_params(operation, {"name": "web", "snapshot_name": value})
    assert refused.value.status_code == 400 and "Snapshot name" in str(refused.value.detail)


# Snapshot create and rename bodies on the raw proxy.

FAMILIES = ["instances", "containers", "virtual-machines"]
VOLUME_TYPES = ["custom", "container", "virtual-machine"]
SNAPSHOT_PATHS = [
    *[(f"{family}-create", f"/1.0/{family}/web-1/snapshots") for family in FAMILIES],
    *[(f"{family}-rename", f"/1.0/{family}/web-1/snapshots/s0") for family in FAMILIES],
    *[(f"volume-{vt}-create", f"/1.0/storage-pools/default/volumes/{vt}/v1/snapshots") for vt in VOLUME_TYPES],
    *[(f"volume-{vt}-rename", f"/1.0/storage-pools/default/volumes/{vt}/v1/snapshots/s0") for vt in VOLUME_TYPES],
]
SNAPSHOT_IDS = [case_id for case_id, _ in SNAPSHOT_PATHS]
REFUSED_NAMES = [".", "..", "", None, "%2e", "%2E%2E", ".%2e", "%2e.", "%252e%252e", "%25252E%25252E", "%2E%2e"]
SENT_NAMES = ["snap-1", "pre-restore-1790000000000", ".hidden", "a.b", "snap0"]
BODY_KEYS = [(EVERY, None), (LEGACY, None), (ORCAHUB, "cust-buyer")]
BODY_KEY_IDS = ["every", "legacy", "orcahub"]


def _refused(seen: Any) -> None:
    assert seen.status == 400 and "cannot name an LXD object" in seen.text, (seen.status, seen.text)
    assert seen.cluster.untouched and seen.records == []


def _sent(seen: Any, method: str = "POST") -> None:
    assert seen.status == 200 and seen.refusals == [], (seen.status, seen.text)
    assert [verb for verb, *_ in seen.cluster.session.calls] == [method]


@pytest.mark.parametrize("api_key,project", BODY_KEYS, ids=BODY_KEY_IDS)
@pytest.mark.parametrize("value", REFUSED_NAMES, ids=repr)
@pytest.mark.parametrize("case_id,path", SNAPSHOT_PATHS, ids=SNAPSHOT_IDS)
def test_snapshot_body_with_a_dot_name_is_refused(door: Any, case_id: str, path: str, value: Any,
                                                     api_key: Dict[str, Any], project: Any) -> None:
    _refused(door.raw(api_key, "POST", path, body={"name": value, "expires_at": "2026-10-01T00:00:00Z"},
                      project=project))


@pytest.mark.parametrize("value", SENT_NAMES)
@pytest.mark.parametrize("case_id,path", SNAPSHOT_PATHS, ids=SNAPSHOT_IDS)
def test_snapshot_body_with_a_real_name_is_sent(door: Any, case_id: str, path: str, value: str) -> None:
    seen = door.raw(ORCAHUB, "POST", path, body={"name": value}, project="cust-buyer")
    _sent(seen)
    assert seen.cluster.session.calls[0][2]["json"] == {"name": value}


@pytest.mark.parametrize("value", [1, 0, True, False, [".."], {"n": ".."}], ids=repr)
@pytest.mark.parametrize("case_id,path", SNAPSHOT_PATHS[:2], ids=SNAPSHOT_IDS[:2])
def test_snapshot_body_with_a_non_string_name_is_left_to_lxd(door: Any, case_id: str, path: str, value: Any) -> None:
    _sent(door.raw(EVERY, "POST", path, body={"name": value}))


@pytest.mark.parametrize("value", ["..a", "a..b", "...", " ..", ".. "], ids=repr)
@pytest.mark.parametrize("case_id,path", [SNAPSHOT_PATHS[0], SNAPSHOT_PATHS[6]], ids=[SNAPSHOT_IDS[0], SNAPSHOT_IDS[6]])
def test_a_name_holding_dots_is_sent_though_the_raw_proxy_cannot_address_it(
    door: Any, case_id: str, path: str, value: str
) -> None:
    _sent(door.raw(EVERY, "POST", path, body={"name": value}))
    later = door.raw(EVERY, "DELETE", f"{path}/{quote(value, safe='')}")
    assert later.status == 400 and "traversal" in later.text and later.cluster.untouched


@pytest.mark.parametrize("method", ["post", "Post", "POST"])
@pytest.mark.parametrize("case_id,path", SNAPSHOT_PATHS, ids=SNAPSHOT_IDS)
def test_snapshot_body_is_judged_whatever_the_method_case(door: Any, case_id: str, path: str, method: str) -> None:
    _refused(door.raw(EVERY, method, path, body={"name": ".."}))


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "GET"])
@pytest.mark.parametrize("case_id,path", SNAPSHOT_PATHS, ids=SNAPSHOT_IDS)
def test_snapshot_body_is_not_judged_on_other_methods(door: Any, case_id: str, path: str, method: str) -> None:
    seen = door.raw(EVERY, method, path, body={"name": "..", "expires_at": "2026-10-01T00:00:00Z"})
    assert seen.status < 500 and seen.status not in (400, 403) and seen.refusals == [], (seen.status, seen.text)
    assert [verb for verb, *_ in seen.cluster.session.calls] == [method]


TEMPLATES = ["/1.0/{instances}/web-1/{snapshots}", "/1.0/{containers}/web-1/{snapshots}/s0",
             "/1.0/{virtual-machines}/web-1/{snapshots}",
             "/1.0/{storage-pools}/default/{volumes}/custom/v1/{snapshots}",
             "/1.0/{storage-pools}/default/{volumes}/custom/v1/{snapshots}/s0"]


@pytest.mark.parametrize("how", SPELLING_IDS)
@pytest.mark.parametrize("template", TEMPLATES,
                         ids=["inst-create", "ct-rename", "vm-create", "vol-create", "vol-rename"])
def test_every_spelling_of_a_snapshot_path_is_judged(door: Any, how: str, template: str) -> None:
    path = spell(template, how)
    _refused(door.raw(EVERY, "POST", path, body={"name": "%2e%2e"}))
    _sent(door.raw(EVERY, "POST", path, body={"name": "snap-1"}))


def test_a_limited_key_is_refused_a_dot_snapshot_name(door: Any) -> None:
    _refused(door.raw(NARROW, "POST", "/1.0/instances/web-1/snapshots", body={"name": ".."}, project="tenant-a"))
    _refused(door.raw(NARROW, "POST", "/1.0/instances/web-1/snapshots/s0", body={"name": "."}, project="tenant-a"))


@pytest.mark.parametrize("case_id,path", SNAPSHOT_PATHS, ids=SNAPSHOT_IDS)
def test_a_snapshot_migration_naming_a_dot_target_is_refused_and_an_unnamed_one_sent(door: Any, case_id: str,
                                                                                        path: str) -> None:
    _refused(door.raw(EVERY, "POST", path, body={"migration": True, "name": ".."}))
    if path.endswith("/s0"):
        _sent(door.raw(EVERY, "POST", path, body={"migration": True}))


@pytest.mark.parametrize("body", [{"Name": ".."}, {"name": "ok", "NAME": ".."}, {"NAME": "..", "name": "ok"}],
                         ids=["upper-only", "lower-then-upper", "upper-then-lower"])
@pytest.mark.parametrize("case_id,path", SNAPSHOT_PATHS, ids=SNAPSHOT_IDS)
def test_a_case_folded_snapshot_name_is_refused_by_the_body_rule(door: Any, case_id: str, path: str,
                                                                    body: Dict[str, Any]) -> None:
    seen = door.raw(EVERY, "POST", path, body=body)
    assert seen.status == 400 and "lowercase ASCII" in seen.text and seen.cluster.untouched
    assert [reason for reason, _ in seen.refusals] == ["field_name_not_canonical"]


# The image create's alias entries share the rule (core/lxd_body_names.py). LXD decodes an alias entry
# with Go's encoding/json, which folds case and keeps the last spelling, so each entry below names a dot or
# nothing. The body rule refuses the entry's odd field name first, so the name rule reads the only spelling.

IMAGE_SOURCE = {"type": "snapshot", "name": "web-1/s0"}


def _field_refused(seen: Any, field: str) -> None:
    assert seen.status == 400 and "lowercase ASCII" in seen.text and repr(field) in seen.text, (seen.status, seen.text)
    assert seen.cluster.untouched and [reason for reason, _ in seen.refusals] == ["field_name_not_canonical"]


@pytest.mark.parametrize("api_key,project", BODY_KEYS, ids=BODY_KEY_IDS)
@pytest.mark.parametrize("entry,field", [({"name": "ok", "NAME": ".."}, "NAME"), ({"name": "ok", "Name": ".."}, "Name"),
                                         ({"name": "ok", "nAME": ""}, "nAME"), ({"NAME": "..", "name": "ok"}, "NAME"),
                                         ({"name": "ok", "\u017fname": ".."}, "\u017fname"),
                                         ({"name": "ok", "Description": "x"}, "Description")],
                         ids=["upper", "title", "empty", "upper-first", "long-s", "other-field"])
def test_an_alias_entry_whose_folded_spelling_names_a_dot_is_refused(door: Any, entry: Dict[str, Any], field: str,
                                                                     api_key: Dict[str, Any], project: Any) -> None:
    seen = door.raw(api_key, "POST", "/1.0/images", body={"source": IMAGE_SOURCE, "aliases": [entry]}, project=project)
    _field_refused(seen, field)


def test_a_later_alias_entry_is_judged_too(door: Any) -> None:
    aliases = [{"name": "mkt/acme/appliance"}, {"name": "ok", "NAME": ".."}]
    _field_refused(door.raw(EVERY, "POST", "/1.0/images", body={"source": IMAGE_SOURCE, "aliases": aliases}), "NAME")


@pytest.mark.parametrize("path", ["/1.0/images/aliases", "/1.0/images/aliases/ok", "/1.0/images/aliases/mkt/acme/app"],
                         ids=["alias-create", "alias-rename", "alias-rename-with-slashes"])
def test_an_alias_create_or_rename_body_with_a_folded_name_is_refused(door: Any, path: str) -> None:
    _field_refused(door.raw(EVERY, "POST", path, body={"name": "ok", "NAME": "..", "target": "0f3c9a1b2c3d"}), "NAME")


def test_map_keys_beside_an_alias_entry_keep_their_case(door: Any) -> None:
    """Only field names are held to the rule. Image properties, config keys and device names are the caller's."""
    image = {"source": IMAGE_SOURCE, "aliases": [{"name": "mkt/acme/appliance", "description": "Up"}],
             "properties": {"OS": "Ubuntu", "Release": "24.04"}}
    _sent(door.raw(EVERY, "POST", "/1.0/images", body=image))
    instance = {"name": "c1", "source": {"type": "none"}, "config": {"environment.HOME": "/root", "user.Tier": "Gold"},
                "devices": {"Data": {"type": "disk", "pool": "default", "source": "vol-1", "path": "/data"}}}
    _sent(door.raw(EVERY, "POST", "/1.0/instances", body=instance))


# /proxy and MCP snapshot create.


def _proxy_wait(door: Any, api_key: Dict[str, Any], params: Dict[str, Any], wait: bool, project: str = "default",
                cluster: Optional[Cluster] = None) -> Any:
    payload = {"cluster_id": CLUSTER_ID, "operation": "create_snapshot", "project": project, "params": params,
               "wait": wait}
    cluster = cluster if cluster is not None else Cluster(client=SnapshotClient())
    return door._send(api_key, cluster, "POST", "/api/v1/integrations/proxy", json=payload)


@pytest.mark.parametrize("wait", [True, False], ids=["wait", "no-wait"])
@pytest.mark.parametrize("api_key,project", [(EVERY, "default"), (LEGACY, "default"), (NARROW, "tenant-a")],
                         ids=["every", "legacy", "narrow"])
@pytest.mark.parametrize("value", [".", "..", "", None, "missing"], ids=repr)
def test_proxy_create_snapshot_refuses_a_dot_or_empty_name(door: Any, value: Any, api_key: Dict[str, Any],
                                                              project: str, wait: bool) -> None:
    params = {"name": "web"} if value == "missing" else {"name": "web", "snapshot_name": value}
    seen = _proxy_wait(door, api_key, params, wait, project)
    assert seen.status == 400 and ("cannot name an LXD object" in seen.text or "is required" in seen.text), seen.text
    assert seen.cluster.untouched and seen.cluster.client.sent == []


def test_proxy_create_snapshot_without_waiting_posts_the_name_in_the_body(door: Any) -> None:
    session = RecordingSession()
    client = type("Client", (), {})()
    client.api = _APINode(ENDPOINT, session=session, timeout=(3, 8))
    client.instances = type("Instances", (), {"get": staticmethod(lambda name: type("I", (), {"name": name})())})()
    seen = _proxy_wait(door, EVERY, {"name": "web", "snapshot_name": "snap-1"}, False, cluster=Cluster(client=client))
    assert seen.status == 200, seen.text
    assert [(verb, url, kw.get("json")) for verb, url, kw in session.calls] == [
        ("POST", f"{ENDPOINT}/instances/web/snapshots", {"name": "snap-1", "stateful": False})]


def test_proxy_create_snapshot_sends_an_encoded_dot_name_literally(door: Any) -> None:
    """/proxy never decodes a name, so %2e%2e is a literal name there (/proxy delete_snapshot reaches it)."""
    seen = _proxy_wait(door, EVERY, {"name": "web", "snapshot_name": "%2e%2e"}, True)
    assert seen.status == 200 and seen.cluster.client.sent == [("snapshot", "web", "%2e%2e")], seen.text


@pytest.mark.parametrize("wait", [True, False], ids=["wait", "no-wait"])
@pytest.mark.parametrize("value", [".", "..", ""], ids=repr)
async def test_mcp_create_snapshot_refuses_a_dot_or_empty_name(door: Any, value: str, wait: bool) -> None:
    seen = await door.mcp(tools_write, "lxd_create_snapshot", EVERY, Cluster(client=SnapshotClient()),
                          cluster_id=CLUSTER_ID, name="web", snapshot_name=value, wait=wait)
    assert seen.status == 0 and ("cannot name an LXD object" in seen.text or "is required" in seen.text), seen.text
    assert seen.cluster.untouched and seen.cluster.client.sent == []


def test_no_proxy_operation_or_mcp_tool_renames_a_snapshot() -> None:
    assert [op for op in PROXY_OPERATION_LEVELS if "rename" in op or "move" in op] == []
    assert "lxd_rename_snapshot" not in dir(tools_write)


# OrcaHub v1.32.0's own named bodies (orcastra.client-snapshot.js, -image.js, -volume.js) are sent.

ORCAHUB_NAMED = [
    ("/1.0/instances/vm-7/snapshots", {"name": "snap-1", "stateful": False, "expires_at": "2026-10-01T00:00:00.000Z"}),
    ("/1.0/instances/vm-7/snapshots", {"name": "pre-restore-1790000000000", "stateful": False,
                                       "expires_at": "2026-10-08T00:00:00.000Z"}),
    ("/1.0/instances/vm-7/snapshots", {"name": "snap-2", "stateful": False}),
    ("/1.0/storage-pools/default/volumes/custom/vol-1/snapshots", {"name": "snap-1",
                                                                    "expires_at": "2026-10-01T00:00:00.000Z"}),
    ("/1.0/storage-pools/default/volumes/custom/vol-1/snapshots", {"name": "pre-restore-1790000000000"}),
    ("/1.0/storage-pools/default/volumes/custom", {"name": "vol-1", "content_type": "filesystem",
                                                   "config": {"size": "10GiB"}}),
    ("/1.0/images", {"source": {"type": "snapshot", "name": "vm-7/snap-1"},
                     "aliases": [{"name": "mkt/acme/appliance", "description": "Appliance"}], "public": False,
                     "compression_algorithm": "gzip"}),
    ("/1.0/images", {"source": {"type": "image", "mode": "pull", "protocol": "simplestreams",
                                "server": "https://images.lxd.canonical.com", "alias": "ubuntu/24.04",
                                "image_type": "virtual-machine"},
                     "aliases": [{"name": "ubuntu/24.04"}], "public": False, "auto_update": True}),
    ("/1.0/images/aliases", {"name": "mkt/acme/appliance", "target": "0f3c9a1b2c3d", "description": "Appliance"}),
]


@pytest.mark.parametrize("path,body", ORCAHUB_NAMED, ids=[f"{i}" for i in range(len(ORCAHUB_NAMED))])
def test_orcahubs_own_named_bodies_are_still_sent(door: Any, path: str, body: Dict[str, Any]) -> None:
    project = None if path.startswith("/1.0/images") else "cust-buyer"
    seen = door.raw(ORCAHUB, "POST", path, body=body, project=project)
    _sent(seen)
    assert seen.cluster.session.calls[0][2]["json"] == body

