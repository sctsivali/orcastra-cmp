"""
A create or rename body that names an LXD object `.`, `..`, empty or null is refused with 400 before it is
sent, on every key door.

LXD addresses a snapshot, a backup, a volume, a network, a zone record or an image alias by the name its
create or rename body carried, and several of its own name checks allow a dot name (core/lxd_body_names.py
cites each). requests folds such a segment out of a path and the raw proxy refuses one, so the object could
never be read, renamed or deleted through a key door once it existed. Each case sends one request through
the real route (tests/_guest_gate_doors.py) and asserts a 400 naming the object, nothing sent to the
cluster and no failure record (a dot-name 400 is not audited, like the raw proxy's traversal 400). Bodies
whose name LXD refuses itself (an instance create, a profile, a project, a network ACL or peer, a bucket)
are left to LXD and pinned as sent.
"""

from typing import Any, Dict, List, Tuple

import pytest

from app.mcp import tools_write
from tests._guest_gate_doors import (  # noqa: F401
    EVERY, FILES, ORCAHUB, SPELLING_IDS, Cluster, SnapshotClient, door_fixture, spell,
)
from tests._limited_write_fakes import _no_webhook_events  # noqa: F401
from tests._raw_proxy_fakes import CLUSTER_ID, _clean_breaker, fence_calls  # noqa: F401

FP = "0f3c9a1b2c3d4e5f"
FAMILIES = ["instances", "containers", "virtual-machines"]
VOLUME = "/1.0/storage-pools/default/volumes/custom/v1"
# note: each value no key door could address once LXD named an object with it, the encoded ones as the raw
# proxy decodes a path.
UNADDRESSABLE = [".", "..", "", None, "%2e%2e", "%2E", "%252e%252e"]
# note: (id, path, the rest of the body). The name goes in `name`, or in the one alias of an image create.
NAMED = [
    *[(f"{family}-snapshot-create", f"/1.0/{family}/web-1/snapshots", {"stateful": False}) for family in FAMILIES],
    *[(f"{family}-snapshot-rename", f"/1.0/{family}/web-1/snapshots/s0", {}) for family in FAMILIES],
    *[(f"{family}-rename", f"/1.0/{family}/web-1", {}) for family in FAMILIES],
    ("instance-backup-create", "/1.0/instances/web-1/backups", {"compression_algorithm": "gzip"}),
    ("instance-backup-rename", "/1.0/instances/web-1/backups/b0", {}),
    ("volume-create", "/1.0/storage-pools/default/volumes/custom", {"config": {"size": "10GiB"}}),
    ("volume-create-typeless", "/1.0/storage-pools/default/volumes", {"type": "custom"}),
    ("volume-rename", VOLUME, {}),
    ("volume-snapshot-create", f"{VOLUME}/snapshots", {}),
    ("volume-snapshot-rename", f"{VOLUME}/snapshots/s0", {}),
    ("volume-backup-create", f"{VOLUME}/backups", {}),
    ("volume-backup-rename", f"{VOLUME}/backups/b0", {}),
    ("pool-create", "/1.0/storage-pools", {"driver": "dir"}),
    ("network-create", "/1.0/networks", {"type": "ovn"}),
    ("network-rename", "/1.0/networks/net-1", {}),
    ("zone-create", "/1.0/network-zones", {}),
    ("zone-record-create", "/1.0/network-zones/example.test/records", {"entries": []}),
    ("alias-create", "/1.0/images/aliases", {"target": FP}),
    ("alias-rename", "/1.0/images/aliases/mkt/acme/appliance", {}),
    ("image-create-alias", "/1.0/images", {"source": {"type": "snapshot", "name": "web-1/s0"}}),
]
NAMED_IDS = [case_id for case_id, _path, _rest in NAMED]


def _body(path: str, rest: Dict[str, Any], name: Any) -> Dict[str, Any]:
    if path == "/1.0/images":
        return {**rest, "aliases": [{"name": name}]}
    return {**rest, "name": name}


def _refused(seen: Any) -> None:
    assert seen.status in (400, 0), (seen.status, seen.text)
    assert "cannot name an LXD object" in seen.text, seen.text
    assert seen.cluster.untouched, seen.cluster.session.calls
    assert seen.refusals == []


def _sent(seen: Any) -> None:
    assert seen.status < 500 and seen.status not in (400, 403), (seen.status, seen.text)
    assert [verb for verb, *_ in seen.cluster.session.calls][-1:] == ["POST"]
    assert seen.refusals == []


@pytest.mark.parametrize("value", UNADDRESSABLE, ids=repr)
@pytest.mark.parametrize("case_id,path,rest", NAMED, ids=NAMED_IDS)
def test_a_body_naming_an_object_unaddressably_is_refused(
    door: Any, case_id: str, path: str, rest: Dict[str, Any], value: Any
) -> None:
    _refused(door.raw(EVERY, "POST", path, body=_body(path, rest, value)))


@pytest.mark.parametrize("value", ["snap-1", "v1.2", ".hidden"])
@pytest.mark.parametrize("case_id,path,rest", NAMED, ids=NAMED_IDS)
def test_a_body_naming_an_object_addressably_is_sent(
    door: Any, case_id: str, path: str, rest: Dict[str, Any], value: str
) -> None:
    _sent(door.raw(EVERY, "POST", path, body=_body(path, rest, value)))


@pytest.mark.parametrize("how", SPELLING_IDS)
@pytest.mark.parametrize("template", ["/1.0/{instances}/web-1/{snapshots}", "/1.0/{instances}/web-1/{snapshots}/s0",
                                      "/1.0/{storage-pools}/default/{volumes}/custom/v1/{snapshots}"],
                         ids=["snapshot-create", "snapshot-rename", "volume-snapshot-create"])
def test_every_spelling_of_a_snapshot_path_is_judged(door: Any, how: str, template: str) -> None:
    path = spell(template, how)
    _refused(door.raw(EVERY, "POST", path, body={"name": ".."}))
    _sent(door.raw(EVERY, "POST", path, body={"name": "snap-1"}))


# note: LXD reads an absent name as an empty one and keeps it for these, so an absent name is refused too.
ABSENT_IS_EMPTY = ["instance-backup-rename", "volume-backup-rename", "zone-record-create", "alias-rename",
                   "image-create-alias"]


@pytest.mark.parametrize("case_id,path,rest", NAMED, ids=NAMED_IDS)
def test_an_absent_name_is_refused_only_where_lxd_would_keep_it_empty(
    door: Any, case_id: str, path: str, rest: Dict[str, Any]
) -> None:
    body = {**rest, "aliases": [{"description": "no name"}]} if path == "/1.0/images" else dict(rest)
    seen = door.raw(EVERY, "POST", path, body=body)
    if case_id in ABSENT_IS_EMPTY:
        _refused(seen)
    else:
        _sent(seen)


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", ["/1.0/instances/web-1/snapshots/s0", f"{VOLUME}/snapshots/s0", VOLUME])
def test_a_name_in_a_body_that_is_not_a_create_or_rename_is_never_judged(door: Any, method: str, path: str) -> None:
    seen = door.raw(EVERY, method, path, body={"name": "..", "description": "x"})
    assert seen.status < 500 and seen.status not in (400, 403) and seen.refusals == [], (seen.status, seen.text)
    assert [verb for verb, *_ in seen.cluster.session.calls] == [method]


# note: LXD refuses a dot name for these itself (core/lxd_body_names.py cites where), so they are sent.
LEFT_TO_LXD: List[Tuple[str, str, Dict[str, Any]]] = [
    ("instance-create", "/1.0/instances", {"source": {"type": "none"}}),
    ("profile-create", "/1.0/profiles", {}),
    ("profile-rename", "/1.0/profiles/p1", {}),
    ("project-create", "/1.0/projects", {"config": {}}),
    ("project-rename", "/1.0/projects/p1", {}),
    ("acl-create", "/1.0/network-acls", {}),
    ("acl-rename", "/1.0/network-acls/acl-1", {}),
    ("peer-create", "/1.0/networks/net-1/peers", {"target_project": "p1", "target_network": "net-2"}),
    ("bucket-create", "/1.0/storage-pools/s3/buckets", {}),
    ("forward-create", "/1.0/networks/net-1/forwards", {"listen_address": ".."}),
]


@pytest.mark.parametrize("case_id,path,rest", LEFT_TO_LXD, ids=[case_id for case_id, *_ in LEFT_TO_LXD])
def test_a_body_whose_name_lxd_refuses_itself_is_left_to_lxd(
    door: Any, case_id: str, path: str, rest: Dict[str, Any]
) -> None:
    _sent(door.raw(EVERY, "POST", path, body={**rest, "name": ".."}))


def test_a_key_without_files_is_still_priced_before_its_backup_name_is_read(door: Any) -> None:
    seen = door.raw(ORCAHUB, "POST", "/1.0/instances/web-1/backups", body={"name": ".."})
    assert seen.status == 403 and "instance.files" in seen.text and seen.cluster.untouched
    assert [reason for reason, _ in seen.refusals] == ["capability_not_granted"]
    _refused(door.raw(FILES, "POST", "/1.0/instances/web-1/backups", body={"name": ".."}))


def test_orcahubs_own_snapshot_and_volume_creates_are_still_sent(door: Any) -> None:
    for path, body in (("/1.0/instances/vm-7/snapshots", {"name": "pre-restore-1790000000000", "stateful": False,
                                                          "expires_at": "2026-10-01T00:00:00Z"}),
                       (f"{VOLUME}/snapshots", {"name": "snap-1", "expires_at": "2026-10-01T00:00:00Z"}),
                       ("/1.0/storage-pools/default/volumes/custom", {"name": "vol-1", "content_type": "filesystem",
                                                                      "config": {"size": "10GiB"}})):
        _sent(door.raw(ORCAHUB, "POST", path, body=body, project="cust-buyer"))


@pytest.mark.parametrize("value", [".", ".."])
async def test_mcp_refuses_a_dot_snapshot_name_on_create(door: Any, value: str) -> None:
    seen = await door.mcp(tools_write, "lxd_create_snapshot", EVERY, Cluster(client=SnapshotClient()),
                          cluster_id=CLUSTER_ID, name="web", snapshot_name=value)
    assert seen.status == 0 and "cannot name an LXD object" in seen.text, seen.text
    assert seen.cluster.untouched and seen.cluster.client.sent == []


async def test_mcp_still_creates_a_named_snapshot(door: Any) -> None:
    seen = await door.mcp(tools_write, "lxd_create_snapshot", EVERY, Cluster(client=SnapshotClient()),
                          cluster_id=CLUSTER_ID, name="web", snapshot_name="snap-1")
    assert seen.status == 200 and seen.cluster.client.sent == [("snapshot", "web", "snap-1")], seen.text
