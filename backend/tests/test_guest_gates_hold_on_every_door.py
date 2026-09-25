"""
The guest gates hold whatever spelling, family, method or value a key sends, on every door.

Each case sends one request through the real route (tests/_guest_gate_doors.py) and asserts the
status, the capability the refusal names, the audit record the gate leaves and whether anything
reached the cluster. Covered here:

- V1: a guest path (exec, console, files, backups, exec output, image export and secret) is refused
  for its path before its body is read, so an odd-cased body is 403 plus a record, and an odd-cased
  migration body is 400 plus a record.
- decision 13 and round 1d: public truthy on POST /1.0/images, for every source type, and on PUT or
  PATCH of an image, costs instance.files, while public false, null or absent is sent.
- custom volume backups cost instance.files for every volume type and method (decided with 10).
- decision 17: storage bucket keys are refused for every key and method, like /certificates, with a
  refused_for_every_key record since the security review, and /proxy and MCP have no bucket operation.
"""

from typing import Any, Dict, Optional

import pytest

from app.mcp import tools_guest, tools_write
from app.mcp.contract import TOOL_CONTRACT
from app.services.lxd.proxy_operations import PROXY_OPERATION_LEVELS
from tests._guest_gate_doors import (  # noqa: F401
    BOTH, EXEC, FILES, LEGACY, NARROW, NO_GUEST, ORCAHUB, ORG, SPELLING_IDS, door_fixture, spell,
)
from tests._limited_write_fakes import _no_webhook_events  # noqa: F401
from tests._raw_proxy_fakes import _clean_breaker, fence_calls  # noqa: F401

FP = "0f3c9a1b2c3d4e5f"
FAMILIES = ["instances", "containers", "virtual-machines"]
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
BODY_METHODS = ("POST", "PUT", "PATCH")
ODD_BODY = {"Command": ["id"], "Path": "/etc/shadow", "Name": "b0"}
EXEC_ACTION, FILES_ACTION = "integration.instance.exec", "integration.instance.files"


def _refused(
    seen: Any, status: int, needle: Optional[str], refused: Optional[str], action: Optional[str] = None
) -> None:
    assert seen.status == status, (seen.status, seen.text)
    if needle is not None:
        assert needle in seen.text, seen.text
    assert seen.cluster.untouched, seen.cluster.session.calls
    expected = [] if refused is None else [refused]
    assert [reason for reason, _ in seen.refusals] == expected, seen.refusals
    if action is not None:
        assert [name for _, name in seen.refusals] == [action]


def _sent(seen: Any, method: str) -> None:
    """Forwarded to the cluster as `method`, whatever LXD then answered, and refused by no gate."""
    assert seen.status < 500 and seen.status not in (400, 403), (seen.status, seen.text)
    assert [verb for verb, *_ in seen.cluster.session.calls][-1:] == [method]
    assert seen.refusals == []


# note: V1: the path is judged before the body

# note: each is (action segment, capability, audit action).
GUEST_ACTIONS = [("exec", "instance.exec", EXEC_ACTION), ("console", "instance.exec", EXEC_ACTION),
                 ("files", "instance.files", FILES_ACTION), ("backups", "instance.files", FILES_ACTION)]
GUEST_TEMPLATES = [
    ("POST", "/1.0/{%s}/web-1/{exec}", "instance.exec", EXEC_ACTION),
    ("POST", "/1.0/{%s}/web-1/{console}", "instance.exec", EXEC_ACTION),
    ("GET", "/1.0/{%s}/web-1/{console}", "instance.exec", EXEC_ACTION),
    ("GET", "/1.0/{%s}/web-1/{files}?path=/etc/shadow", "instance.files", FILES_ACTION),
    ("POST", "/1.0/{%s}/web-1/{files}?path=/root/.ssh/authorized_keys", "instance.files", FILES_ACTION),
    ("POST", "/1.0/{%s}/web-1/{backups}", "instance.files", FILES_ACTION),
    ("GET", "/1.0/{%s}/web-1/{backups}/b0/{export}", "instance.files", FILES_ACTION),
    ("POST", "/1.0/{%s}/web-1/{backups}/b0", "instance.files", FILES_ACTION),
    ("GET", "/1.0/{%s}/web-1/{logs}/exec-output", "instance.exec", EXEC_ACTION),
]


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("action,capability,audit", GUEST_ACTIONS, ids=[a for a, *_ in GUEST_ACTIONS])
@pytest.mark.parametrize("method", METHODS)
def test_every_method_on_a_guest_path_is_refused_for_its_path(
    door: Any, family: str, action: str, capability: str, audit: str, method: str
) -> None:
    body = ODD_BODY if method in BODY_METHODS else None
    seen = door.raw(NO_GUEST, method, f"/1.0/{family}/web-1/{action}", body=body)
    _refused(seen, 403, capability, "capability_not_granted", audit)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("how", SPELLING_IDS)
@pytest.mark.parametrize("method,template,capability,audit", GUEST_TEMPLATES,
                         ids=[f"{m}-{t.split('}/web-1/', 1)[1]}" for m, t, *_ in GUEST_TEMPLATES])
def test_every_spelling_of_a_guest_path_is_refused_with_an_odd_body(
    door: Any, family: str, how: str, method: str, template: str, capability: str, audit: str
) -> None:
    path = spell(template.replace("%s", family), how)
    seen = door.raw(NO_GUEST, method, path, body=ODD_BODY if method in BODY_METHODS else None)
    _refused(seen, 403, capability, "capability_not_granted", audit)


OTHER_KEYS = (("legacy", LEGACY), ("orcahub", ORCAHUB), ("files-only", FILES), ("exec-only", EXEC))
KEYS_WITHOUT = [(name, api_key, action, capability, audit)
                for name, api_key in OTHER_KEYS
                for action, capability, audit in GUEST_ACTIONS if capability not in (api_key.get("capabilities") or [])]


@pytest.mark.parametrize("name,api_key,action,capability,audit", KEYS_WITHOUT,
                         ids=[f"{name}-{action}" for name, _k, action, *_ in KEYS_WITHOUT])
def test_a_key_without_that_capability_is_refused(
    door: Any, name: str, api_key: Dict[str, Any], action: str, capability: str, audit: str
) -> None:
    seen = door.raw(api_key, "POST", f"/1.0/instances/web-1/{action}", body=ODD_BODY)
    _refused(seen, 403, capability, "capability_not_granted", audit)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("action,capability,audit", GUEST_ACTIONS, ids=[a for a, *_ in GUEST_ACTIONS])
def test_a_key_holding_the_capability_gets_the_spelling_refusal_and_its_record(
    door: Any, family: str, action: str, capability: str, audit: str
) -> None:
    seen = door.raw(BOTH, "POST", f"/1.0/{family}/web-1/{action}", body=ODD_BODY)
    _refused(seen, 400, "'Command'", "field_name_not_canonical", audit)


@pytest.mark.parametrize("path", [f"/1.0/images/{FP}/export", f"/1.0/images/{FP}/secret",
                                  f"/1.0/IMAGES/{FP}/%65xport", f"/1.0/images/{FP}/secret/"],
                         ids=["export", "secret", "export-spelled", "secret-slash"])
@pytest.mark.parametrize("method", METHODS)
def test_an_image_handout_is_refused_for_its_path(door: Any, path: str, method: str) -> None:
    seen = door.raw(NO_GUEST, method, path, body={"Target": "x"} if method in BODY_METHODS else None)
    _refused(seen, 403, "instance.files", "capability_not_granted")


MIGRATION_SOURCES = [
    *[f"/1.0/{family}/web-1" for family in FAMILIES],
    *[f"/1.0/{family}/web-1/snapshots/s0" for family in FAMILIES],
    *[f"/1.0/storage-pools/default/volumes/{kind}/v1" for kind in ("custom", "container", "virtual-machine", "image")],
    "/1.0/storage-pools/default/volumes/custom/v1/snapshots/s0",
    "/1.0/%69nstances/web-1/",
]


@pytest.mark.parametrize("field", ["Migration", "MIGRATION", "migratioN"])
@pytest.mark.parametrize("api_key", [NO_GUEST, FILES, BOTH, ORCAHUB, LEGACY],
                         ids=["no-guest", "files", "both", "orcahub", "legacy"])
@pytest.mark.parametrize("path", MIGRATION_SOURCES)
def test_an_odd_cased_migration_is_400_and_recorded(door: Any, path: str, api_key: Dict[str, Any], field: str) -> None:
    seen = door.raw(api_key, "POST", path, body={field: True, "name": "web-2"})
    _refused(seen, 400, repr(field), "field_name_not_canonical")


PRICED_VALUES = [True, "true", "false", 1, ["x"], {"x": 1}, ""]
UNPRICED_VALUES = [False, None, 0]


@pytest.mark.parametrize("value", PRICED_VALUES, ids=repr)
@pytest.mark.parametrize("path", MIGRATION_SOURCES)
def test_a_migration_value_other_than_false_costs_files(door: Any, path: str, value: Any) -> None:
    seen = door.raw(NO_GUEST, "POST", path, body={"migration": value})
    _refused(seen, 403, "instance.files", "capability_not_granted")
    _sent(door.raw(FILES, "POST", path, body={"migration": value}), "POST")


@pytest.mark.parametrize("value", UNPRICED_VALUES + ["absent"], ids=repr)
@pytest.mark.parametrize("path", MIGRATION_SOURCES[:3])
def test_a_migration_value_that_is_false_or_missing_is_sent(door: Any, path: str, value: Any) -> None:
    body = {"name": "web-2"} if value == "absent" else {"name": "web-2", "migration": value}
    _sent(door.raw(NO_GUEST, "POST", path, body=body), "POST")


PROXY_ODD = [
    ("exec_instance", {"name": "web-1", "Command": ["id"]}, EXEC_ACTION),
    ("exec_instance", {"name": "web-1", "command": ["id"], "Environment": {}}, EXEC_ACTION),
    ("get_file", {"name": "web-1", "Path": "/etc/shadow"}, FILES_ACTION),
    ("put_file", {"name": "web-1", "path": "/tmp/x", "Content": "x"}, FILES_ACTION),
    ("create_instance", {"name": "c1", "Source": {"type": "image", "alias": "a"}}, "integration.proxy.request"),
    ("create_instance", {"name": "c1", "source": {"type": "copy", "source": "web", "Project": "b"}},
     "integration.proxy.request"),
    ("update_instance", {"name": "web-1", "Config": {"user.user-data": "x"}}, "integration.proxy.request"),
]


@pytest.mark.parametrize("operation,params,audit", PROXY_ODD, ids=[f"{o}-{i}" for i, (o, *_) in enumerate(PROXY_ODD)])
@pytest.mark.parametrize("api_key", [NO_GUEST, BOTH], ids=["no-guest", "both"])
def test_proxy_answers_odd_params_400_with_a_record(
    door: Any, operation: str, params: Dict[str, Any], audit: str, api_key: Dict[str, Any]
) -> None:
    _refused(door.proxy(api_key, operation, params), 400, "lowercase ASCII", "field_name_not_canonical", audit)


@pytest.mark.parametrize("operation,params,capability,audit", [
    ("exec_instance", {"name": "web-1", "command": ["id"]}, "instance.exec", EXEC_ACTION),
    ("get_file", {"name": "web-1", "path": "/etc/shadow"}, "instance.files", FILES_ACTION),
    ("put_file", {"name": "web-1", "path": "/tmp/x", "content": "x"}, "instance.files", FILES_ACTION),
], ids=["exec", "get-file", "put-file"])
def test_proxy_refuses_a_guest_operation_with_an_attributed_record(
    door: Any, operation: str, params: Dict[str, Any], capability: str, audit: str
) -> None:
    seen = door.proxy(NO_GUEST, operation, params)
    _refused(seen, 403, capability, "capability_not_granted", audit)
    assert (seen.records[-1]["target_organization_id"], seen.records[-1]["target_organization_slug"]) == ORG


async def test_mcp_refuses_an_odd_source_and_an_exec_with_records(door: Any) -> None:
    odd = await door.mcp(tools_write, "lxd_create_instance", BOTH, cluster_id="cluster-a", name="c1",
                         source={"Type": "image", "alias": "a"})
    assert odd.status == 0 and "'Type'" in odd.text and odd.cluster.untouched
    exec_refused = await door.mcp(tools_guest, "lxd_exec_instance", NO_GUEST, cluster_id="cluster-a", name="web-1",
                                  command=["id"])
    assert exec_refused.status == 0 and "instance.exec" in exec_refused.text and exec_refused.cluster.untouched
    assert door.trail.refusals == [("field_name_not_canonical", "integration.proxy.request"),
                                   ("capability_not_granted", EXEC_ACTION)]


# note: decision 13 and round 1d: a public image costs files

SOURCES = {
    "instance": {"type": "instance", "name": "web-1"},
    "container": {"type": "container", "name": "web-1"},
    "virtual-machine": {"type": "virtual-machine", "name": "vm-1"},
    "snapshot": {"type": "snapshot", "name": "web-1/s0"},
    "upper-type": {"type": "INSTANCE", "name": "web-1"},
    "local-fingerprint": {"type": "image", "fingerprint": FP},
    "pull": {"type": "image", "mode": "pull", "protocol": "simplestreams", "server": "https://images.example.test",
             "alias": "ubuntu/24.04"},
    "push": {"type": "image", "mode": "push", "fingerprint": FP},
    "url": {"type": "url", "url": "https://images.example.test/x.tar.gz"},
    "none": None,
}
PUBLIC_TRUTHY = [True, "true", "false", 1, "yes", ["x"], {"x": 1}]
PUBLIC_FALSY = [False, None, 0, "absent"]


def _image_body(source: Any, public: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {"aliases": [{"name": "a"}]}
    if source is not None:
        body["source"] = source
    if public != "absent":
        body["public"] = public
    return body


@pytest.mark.parametrize("public", PUBLIC_TRUTHY, ids=repr)
@pytest.mark.parametrize("source", list(SOURCES), ids=list(SOURCES))
def test_a_public_image_create_costs_files_for_every_source(door: Any, source: str, public: Any) -> None:
    seen = door.raw(NO_GUEST, "POST", "/1.0/images", body=_image_body(SOURCES[source], public))
    _refused(seen, 403, "instance.files", "capability_not_granted", "integration.proxy.request")


@pytest.mark.parametrize("public", PUBLIC_FALSY, ids=repr)
@pytest.mark.parametrize("source", list(SOURCES), ids=list(SOURCES))
def test_a_private_image_create_is_sent_for_every_source(door: Any, source: str, public: Any) -> None:
    _sent(door.raw(NO_GUEST, "POST", "/1.0/images", body=_image_body(SOURCES[source], public)), "POST")


@pytest.mark.parametrize("how", SPELLING_IDS)
@pytest.mark.parametrize("method,template", [("POST", "/1.0/{images}"), ("PUT", f"/1.0/{{images}}/{FP}"),
                                             ("PATCH", f"/1.0/{{images}}/{FP}")], ids=["create", "put", "patch"])
def test_every_spelling_of_an_image_write_is_judged(door: Any, how: str, method: str, template: str) -> None:
    path = spell(template, how)
    body = _image_body(SOURCES["snapshot"] if method == "POST" else None, True)
    _refused(door.raw(NO_GUEST, method, path, body=body), 403, "instance.files", "capability_not_granted")
    _sent(door.raw(NO_GUEST, method, path, body=_image_body(SOURCES["snapshot"] if method == "POST" else None, False)),
          method)


@pytest.mark.parametrize("method", ["PUT", "PATCH"])
@pytest.mark.parametrize("public", PUBLIC_TRUTHY + PUBLIC_FALSY, ids=repr)
def test_making_an_existing_image_public_costs_files(door: Any, method: str, public: Any) -> None:
    body = _image_body(None, public)
    body.pop("aliases")
    seen = door.raw(NO_GUEST, method, f"/1.0/images/{FP}", body={**body, "properties": {"os": "ubuntu"}})
    if public in PUBLIC_FALSY:
        _sent(seen, method)
    else:
        _refused(seen, 403, "instance.files", "capability_not_granted")


@pytest.mark.parametrize("api_key,priced",
                         [(LEGACY, True), (ORCAHUB, True), (EXEC, True), (FILES, False), (BOTH, False)],
                         ids=["legacy", "orcahub", "exec-only", "files", "both"])
@pytest.mark.parametrize("source", ["snapshot", "local-fingerprint", "none"])
def test_only_a_key_holding_files_makes_an_image_public(
    door: Any, api_key: Dict[str, Any], priced: bool, source: str
) -> None:
    seen = door.raw(api_key, "POST", "/1.0/images", body=_image_body(SOURCES[source], True))
    if priced:
        _refused(seen, 403, "instance.files", "capability_not_granted")
    else:
        _sent(seen, "POST")


@pytest.mark.parametrize("field", ["Public", "PUBLIC", "puBlic"])
@pytest.mark.parametrize("method,path",
                         [("POST", "/1.0/images"), ("PUT", f"/1.0/images/{FP}"), ("PATCH", f"/1.0/images/{FP}")])
def test_an_odd_cased_public_is_400_and_recorded(door: Any, field: str, method: str, path: str) -> None:
    seen = door.raw(FILES, method, path, body={field: True, "source": SOURCES["snapshot"]})
    _refused(seen, 400, repr(field), "field_name_not_canonical")


@pytest.mark.parametrize("method", ["GET", "DELETE"])
@pytest.mark.parametrize("path", ["/1.0/images", f"/1.0/images/{FP}"])
def test_a_body_on_a_read_or_delete_is_never_judged(door: Any, method: str, path: str) -> None:
    seen = door.raw(NO_GUEST, method, path, body={"public": True})
    _sent(seen, method)
    assert "json" not in seen.cluster.session.calls[-1][2] or seen.cluster.session.calls[-1][2]["json"] is None


# note: OrcaHub v1.32.0 (orcastra.client-image.js), its private publish, its pull, its alias create and its
# image delete, with its preset key.
ORCAHUB_PUBLISH = {"source": {"type": "snapshot", "name": "web-1/snap-0"},
                   "aliases": [{"name": "mkt/acme/appliance-3f2a", "description": "Appliance"}],
                   "public": False, "compression_algorithm": "gzip"}
ORCAHUB_PULL = {"source": {"type": "image", "mode": "pull", "protocol": "simplestreams",
                           "server": "https://images.lxd.canonical.com", "alias": "ubuntu/24.04",
                           "image_type": "container"},
                "aliases": [{"name": "ubuntu/24.04"}], "public": False, "auto_update": True}
ORCAHUB_IMAGE_WRITES = [
    ("POST", "/1.0/images", "cust-buyer", ORCAHUB_PUBLISH),
    ("POST", "/1.0/images", None, ORCAHUB_PULL),
    ("POST", "/1.0/images/aliases", None, {"name": "ubuntu/24.04", "target": FP}),
    ("DELETE", f"/1.0/images/{FP}", "cust-buyer", None),
    ("DELETE", "/1.0/images/aliases/mkt/acme/appliance-3f2a", "cust-buyer", None),
]


@pytest.mark.parametrize("method,path,project,body", ORCAHUB_IMAGE_WRITES,
                         ids=["publish", "pull", "alias-create", "image-delete", "alias-delete"])
def test_orcahubs_own_image_writes_are_still_sent(door: Any, method: str, path: str, project: Any, body: Any) -> None:
    _sent(door.raw(ORCAHUB, method, path, body=body, project=project), method)


@pytest.mark.parametrize("body", [{**ORCAHUB_PUBLISH, "public": True},
                                  {"source": {"type": "image", "fingerprint": FP}, "public": True}],
                         ids=["publish-flipped-public", "copy-of-the-private-publish"])
def test_orcahubs_key_cannot_make_its_published_image_public(door: Any, body: Dict[str, Any]) -> None:
    _refused(door.raw(ORCAHUB, "POST", "/1.0/images", body=body, project="cust-buyer"),
             403, "instance.files", "capability_not_granted")


# note: custom volume backups cost files

VOLUME_TYPES = ["custom", "container", "virtual-machine", "image"]
BACKUP_TAILS = ["backups", "backups/b0", "backups/b0/export"]


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("tail", BACKUP_TAILS)
@pytest.mark.parametrize("kind", VOLUME_TYPES)
def test_a_volume_backup_costs_files_for_every_type_and_method(door: Any, kind: str, tail: str, method: str) -> None:
    body = {"Name": "b1"} if method in BODY_METHODS else None
    seen = door.raw(NO_GUEST, method, f"/1.0/storage-pools/default/volumes/{kind}/v1/{tail}", body=body)
    _refused(seen, 403, "instance.files", "capability_not_granted")
    if method in BODY_METHODS:
        _refused(door.raw(FILES, method, f"/1.0/storage-pools/default/volumes/{kind}/v1/{tail}", body=body),
                 400, "'Name'", "field_name_not_canonical")


@pytest.mark.parametrize("how", SPELLING_IDS)
@pytest.mark.parametrize("kind", VOLUME_TYPES)
def test_every_spelling_of_a_volume_backup_is_priced(door: Any, how: str, kind: str) -> None:
    path = spell(f"/1.0/{{storage-pools}}/default/{{volumes}}/{kind}/v1/{{backups}}/b0/{{export}}", how)
    _refused(door.raw(NO_GUEST, "GET", path), 403, "instance.files", "capability_not_granted")


@pytest.mark.parametrize("api_key,priced,project", [
    (FILES, False, None), (BOTH, False, None), (ORCAHUB, True, None), (LEGACY, True, None), (EXEC, True, None),
    (NARROW, True, "tenant-a"),
], ids=["files", "both", "orcahub", "legacy", "exec-only", "narrow-read-key"])
def test_only_a_key_holding_files_reaches_a_volume_backup(
    door: Any, api_key: Dict[str, Any], priced: bool, project: Any
) -> None:
    seen = door.raw(api_key, "GET", "/1.0/storage-pools/default/volumes/custom/v1/backups/b0/export", project=project)
    if priced:
        _refused(seen, 403, "instance.files", "capability_not_granted")
    else:
        _sent(seen, "GET")


@pytest.mark.parametrize("method,path", [
    ("GET", "/1.0/storage-pools/default/volumes/custom/backups"),
    ("DELETE", "/1.0/storage-pools/default/volumes/custom/backups"),
    ("GET", "/1.0/storage-pools/default/volumes/custom/v1/snapshots?recursion=1"),
    ("POST", "/1.0/storage-pools/default/volumes/custom/v1/snapshots"),
    ("PATCH", "/1.0/storage-pools/default/volumes/custom/data-1"),
], ids=["volume-named-backups", "delete-volume-named-backups", "snapshot-list", "snapshot-create", "orcahub-resize"])
def test_a_volume_path_that_is_not_a_backup_is_sent(door: Any, method: str, path: str) -> None:
    body = {"config": {"size": "20GiB"}} if method in BODY_METHODS else None
    _sent(door.raw(NO_GUEST, method, path, body=body), method)


# note: decision 17: bucket keys are refused for every key

BUCKET_KEY_PATHS = ["keys", "keys/k1", "keys/k1/extra"]


@pytest.mark.parametrize("api_key", [LEGACY, BOTH, NARROW, ORCAHUB], ids=["legacy-star", "both", "narrow", "orcahub"])
@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("tail", BUCKET_KEY_PATHS)
def test_bucket_keys_are_refused_for_every_key_and_method(
    door: Any, api_key: Dict[str, Any], method: str, tail: str
) -> None:
    project = "tenant-a" if api_key is NARROW else None
    body = {"name": "k2", "role": "admin"} if method in BODY_METHODS else None
    seen = door.raw(api_key, method, f"/1.0/storage-pools/s3/buckets/b1/{tail}", body=body, project=project)
    _refused(seen, 403, "storage bucket keys", "refused_for_every_key")


@pytest.mark.parametrize("path", [
    *[spell("/1.0/{storage-pools}/s3/{buckets}/b1/{keys}/k1", how) for how in SPELLING_IDS],
    "/1.0/storage-pools/s3/buckets/b1%2Fkeys", "/1.0/storage-pools/s3/buckets/b1/keys#frag",
    "/1.0/storage-pools/s3/buckets/b1/%256Beys", "/1.0/storage-pools/s3/buckets/b1/keys?recursion=1",
], ids=[*SPELLING_IDS, "encoded-slash-in-bucket", "fragment", "double-encoded-keys", "recursion"])
def test_every_spelling_of_bucket_keys_is_refused(door: Any, path: str) -> None:
    _refused(door.raw(LEGACY, "GET", path), 403, "storage bucket keys", "refused_for_every_key")


@pytest.mark.parametrize("method,path", [
    ("GET", "/1.0/storage-pools/s3/buckets"), ("GET", "/1.0/storage-pools/s3/buckets/b1"),
    ("GET", "/1.0/storage-pools/s3/buckets/keys"), ("DELETE", "/1.0/storage-pools/s3/buckets/keys"),
    ("GET", "/1.0/storage-pools/s3/volumes/custom/keys"),
], ids=["bucket-list", "bucket-read", "bucket-named-keys", "delete-bucket-named-keys", "volume-named-keys"])
def test_other_bucket_paths_keep_their_answer(door: Any, method: str, path: str) -> None:
    _sent(door.raw(LEGACY, method, path), method)


def test_proxy_and_mcp_have_no_bucket_image_or_backup_operation() -> None:
    words = ("bucket", "image", "backup", "publish", "public", "export", "secret")
    assert [op for op in PROXY_OPERATION_LEVELS if any(word in op for word in words)] == ["list_images"]
    assert [tool for tool in TOOL_CONTRACT if any(word in tool for word in words)] == ["lxd_list_images"]
