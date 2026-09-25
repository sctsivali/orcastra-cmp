"""
cloud-init.ssh-keys.<name> on every door, judged by what changes, and the trust-surface refusal records
(certificates, auth, server and cluster writes, bucket keys) with unchanged answers.

Each case sends one request through the real route (tests/_guest_gate_doors.py), with the key lookup and the
cluster connection replaced by recording fakes, and asserts the status, what was read, what was written and the
audit records.
"""

from typing import Any, Callable, Dict, List

import pytest
import requests
from fastapi import HTTPException

from app.core.api_key_capabilities import assert_proxy_path_allowed
from app.core.raw_proxy_path import assert_server_state_unchanged
from app.core.storage_bucket_keys import assert_bucket_keys_unreached
from app.mcp import tools_write
from app.routers import integrations
from tests._cloud_init_fakes import ConfigSession
from tests._guest_gate_doors import (  # noqa: F401
    BOTH, EVERY, EXEC, FILES, LEGACY, NARROW, NO_GUEST, ORCAHUB, SPELLING_IDS, Cluster, door_fixture,
    proxy_cluster, spell,
)
from tests._limited_write_fakes import _no_webhook_events  # noqa: F401
from tests._raw_proxy_fakes import CLUSTER_ID, ENDPOINT, RecordingSession, _clean_breaker, fence_calls  # noqa: F401

KEY_A = "root:ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA ops@example.test"
KEY_B = "ubuntu:ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB intruder@example.test"
ROOT, DEPLOY = "cloud-init.ssh-keys.root", "cloud-init.ssh-keys.deploy"
PRICED_NAMES = [ROOT, "cloud-init.ssh-keys.Root", "CLOUD-INIT.SSH-KEYS.root", "Cloud-Init.Ssh-Keys.deploy",
                "cloud-init.ssh-keys.a.b", "cloud-init.ssh-keys.x-y_z", "cloud-init.ssh-keys.1", "cloud-init.ssh-keys."]
UNPRICED_NAMES = ["cloud-init.ssh-keys", "cloud-init.ssh-keysroot", "user.ssh-keys.root", "cloud-init.ssh_keys.root",
                  "cloud-init.sshkeys.root", "cloud-init.ssh-key.root", " cloud-init.ssh-keys.root",
                  "cloud-init.ssh-keys-root"]
DENIED = [(NO_GUEST, None), (FILES, None), (ORCAHUB, "cust-buyer"), (LEGACY, None), (NARROW, "tenant-a")]
DENIED_IDS = ["no-guest", "files", "orcahub", "legacy", "narrow"]
GRANTED = [EXEC, BOTH, EVERY]
RAW_CREATES = [
    ("/1.0/instances", {"name": "c1", "source": {"type": "image", "alias": "ubuntu/24.04"}}),
    ("/1.0/containers", {"name": "c1", "source": {"type": "none"}}),
    ("/1.0/virtual-machines", {"name": "c1", "source": {"type": "image", "alias": "ubuntu/24.04"}}),
    ("/1.0/instances", {"name": "c1", "source": {"type": "copy", "source": "web-1"}}),
    ("/1.0/profiles", {"name": "p9"}),
]
RAW_CREATE_IDS = ["instances", "containers", "virtual-machines", "copy", "profile"]


def _priced(seen: Any, reads: int = 0) -> None:
    assert seen.status in (0, 403) and "instance.exec" in seen.text, (seen.status, seen.text)
    assert len(seen.cluster.session.reads) == reads and seen.cluster.writes == []
    assert [reason for reason, _ in seen.refusals] == ["capability_not_granted"]


def _passed(seen: Any) -> None:
    assert seen.status == 200 and seen.refusals == [], (seen.status, seen.text)
    assert len(seen.cluster.writes) == 1


# note: creates, on every door, priced without a read


@pytest.mark.parametrize("value", [KEY_A, "", None, 1, True, [KEY_A]], ids=repr)
@pytest.mark.parametrize("name", PRICED_NAMES)
@pytest.mark.parametrize("api_key,project", DENIED, ids=DENIED_IDS)
@pytest.mark.parametrize("path,body", RAW_CREATES, ids=RAW_CREATE_IDS)
def test_raw_create_with_an_ssh_key_costs_exec(door: Any, path: str, body: Dict[str, Any], api_key: Dict[str, Any],
                                                  project: Any, name: str, value: Any) -> None:
    seen = door.raw(api_key, "POST", path, body={**body, "config": {name: value}}, project=project)
    _priced(seen)
    assert seen.cluster.untouched


@pytest.mark.parametrize("api_key", GRANTED, ids=["exec", "both", "every"])
@pytest.mark.parametrize("path,body", RAW_CREATES, ids=RAW_CREATE_IDS)
def test_raw_create_with_an_ssh_key_is_sent_for_a_key_holding_exec(door: Any, path: str, body: Dict[str, Any],
                                                                      api_key: Dict[str, Any]) -> None:
    sent = {**body, "config": {ROOT: KEY_A}}
    seen = door.raw(api_key, "POST", path, body=sent)
    _passed(seen)
    assert seen.cluster.writes == [("POST", sent)] and seen.cluster.session.reads == []


@pytest.mark.parametrize("name", UNPRICED_NAMES, ids=repr)
@pytest.mark.parametrize("path,body", RAW_CREATES, ids=RAW_CREATE_IDS)
def test_a_near_miss_key_is_not_priced(door: Any, path: str, body: Dict[str, Any], name: str) -> None:
    _passed(door.raw(NO_GUEST, "POST", path, body={**body, "config": {name: KEY_A}}))


@pytest.mark.parametrize("name", PRICED_NAMES)
@pytest.mark.parametrize("source", [{"type": "image", "alias": "a"}, {"type": "copy", "source": "web"},
                                    {"type": "none"}], ids=["image", "copy", "none"])
@pytest.mark.parametrize("api_key,project", DENIED[:4] + [(NARROW, "tenant-a")], ids=DENIED_IDS)
def test_proxy_create_with_an_ssh_key_costs_exec(door: Any, api_key: Dict[str, Any], project: Any,
                                                    source: Dict[str, Any], name: str) -> None:
    seen = door.proxy(api_key, "create_instance", {"name": "c1", "source": source, "config": {name: KEY_A}},
                      project=project or "default")
    _priced(seen)
    assert seen.cluster.untouched


@pytest.mark.parametrize("wait", [True, False], ids=["wait", "no-wait"])
@pytest.mark.parametrize("name", [ROOT, "Cloud-Init.Ssh-Keys.deploy"])
async def test_mcp_create_with_an_ssh_key_costs_exec(door: Any, name: str, wait: bool) -> None:
    seen = await door.mcp(tools_write, "lxd_create_instance", NO_GUEST, cluster_id=CLUSTER_ID, name="c1",
                          source={"type": "image", "alias": "a"}, config={name: KEY_A}, wait=wait)
    _priced(seen)
    assert seen.cluster.untouched


# note: updates, judged against the object's own config (decision 18)

UPDATE_TARGETS = [
    *[(method, f"/1.0/{family}/web-1", f"/{family}/web-1") for family in ("instances", "containers", "virtual-machines")
      for method in ("PUT", "PATCH", "POST")],
    ("PUT", "/1.0/profiles/p1", "/profiles/p1"), ("PATCH", "/1.0/profiles/p1", "/profiles/p1"),
]
UPDATE_IDS = [f"{m}-{t.strip('/')}" for m, _p, t in UPDATE_TARGETS]
# note: (id, the object's own config, the config sent, priced)
SSH_COMBOS = [
    ("second-key-added", {ROOT: KEY_A}, {ROOT: KEY_A, DEPLOY: KEY_B}, True),
    ("one-of-two-left-out", {ROOT: KEY_A, DEPLOY: KEY_B}, {ROOT: KEY_A}, False),
    ("both-unchanged", {ROOT: KEY_A, DEPLOY: KEY_B}, {DEPLOY: KEY_B, ROOT: KEY_A}, False),
    ("value-swapped", {ROOT: KEY_A, DEPLOY: KEY_B}, {ROOT: KEY_B, DEPLOY: KEY_A}, True),
    ("other-case-of-the-name", {"cloud-init.ssh-keys.Root": KEY_A}, {ROOT: KEY_A}, True),
    ("same-odd-case-name", {"cloud-init.ssh-keys.Root": KEY_A}, {"cloud-init.ssh-keys.Root": KEY_A}, False),
    ("trailing-newline", {ROOT: KEY_A}, {ROOT: KEY_A + "\n"}, True),
    ("upper-spelling", {ROOT: KEY_A}, {"CLOUD-INIT.SSH-KEYS.ROOT": KEY_A}, True),
    ("removed-by-empty", {ROOT: KEY_A}, {ROOT: ""}, True),
    ("empty-on-absent", {}, {ROOT: ""}, False),
    ("null-on-absent", {}, {ROOT: None}, False),
    ("beside-unchanged-user-data", {ROOT: KEY_A, "user.user-data": "#cloud-config\n"},
     {ROOT: KEY_A, "user.user-data": "#cloud-config\n", "limits.cpu": "4"}, False),
    ("unchanged-user-data-new-key", {"user.user-data": "#cloud-config\n"},
     {"user.user-data": "#cloud-config\n", ROOT: KEY_A}, True),
]


# note: a project-limited key is refused any profile, container or vm write (pinned in round 1d), so it is
# judged on /instances only.
KEYED_TARGETS = [(api_key, project, method, path, target) for api_key, project in DENIED
                 for method, path, target in UPDATE_TARGETS if api_key is not NARROW or target.startswith("/instances")]
KEYED_IDS = [f"{DENIED_IDS[DENIED.index((k, p))]}-{m}-{t.strip('/')}" for k, p, m, _path, t in KEYED_TARGETS]


@pytest.mark.parametrize("combo,held,config,priced", SSH_COMBOS, ids=[c[0] for c in SSH_COMBOS])
@pytest.mark.parametrize("api_key,project,method,path,target", KEYED_TARGETS, ids=KEYED_IDS)
def test_raw_update_prices_an_ssh_key_by_what_it_changes(
    door: Any, api_key: Dict[str, Any], project: Any, method: str, path: str, target: str, combo: str,
    held: Dict[str, Any], config: Dict[str, Any], priced: bool
) -> None:
    where = project or "default"
    body = {"config": config, **({"name": "web-2"} if method == "POST" else {})}
    cluster = Cluster(ConfigSession({(where, target): {"name": target.rsplit("/", 1)[-1], "config": dict(held)}}))
    seen = door.raw(api_key, method, path, body=body, project=project, cluster=cluster)
    assert seen.cluster.session.reads == [(ENDPOINT + target, where)], seen.cluster.session.calls
    if priced:
        _priced(seen, reads=1)
    else:
        _passed(seen)
        assert seen.cluster.writes == [(method, body)]


@pytest.mark.parametrize("method,path,target", UPDATE_TARGETS, ids=UPDATE_IDS)
def test_raw_update_prices_a_key_only_a_profile_gives(door: Any, method: str, path: str, target: str) -> None:
    session = ConfigSession({("default", target): {"config": {}, "expanded_config": {ROOT: KEY_A}, "profiles": ["k"]}})
    body = {"config": {ROOT: KEY_A}, **({"name": "web-2"} if method == "POST" else {})}
    _priced(door.raw(NO_GUEST, method, path, body=body, cluster=Cluster(session)), reads=1)


@pytest.mark.parametrize("fail,status", [(None, 404), (500, 502),
                                         (requests.exceptions.ReadTimeout("host 10.0.0.9"), 504),
                                         (requests.exceptions.ConnectionError("host 10.0.0.9"), 502)],
                         ids=["missing", "500", "read-timeout", "unreachable"])
@pytest.mark.parametrize("method,path,target", UPDATE_TARGETS[:3] + UPDATE_TARGETS[-1:],
                         ids=UPDATE_IDS[:3] + UPDATE_IDS[-1:])
def test_raw_ssh_key_update_whose_read_fails_sends_nothing(door: Any, method: str, path: str, target: str,
                                                              fail: Any, status: int) -> None:
    held = {} if fail is None else {("default", target): {"config": {ROOT: KEY_A}}}
    seen = door.raw(NO_GUEST, method, path, body={"config": {ROOT: KEY_A}},
                    cluster=Cluster(ConfigSession(held, fail=fail)))
    assert seen.status == status and "The write was not sent" in seen.text and "10.0.0.9" not in seen.text
    assert seen.cluster.writes == [] and [reason for reason, _ in seen.refusals] == ["cloud_init_not_compared"]


@pytest.mark.parametrize("api_key", GRANTED, ids=["exec", "both", "every"])
@pytest.mark.parametrize("method,path,target", UPDATE_TARGETS, ids=UPDATE_IDS)
def test_raw_update_never_reads_for_a_key_holding_exec(door: Any, api_key: Dict[str, Any], method: str, path: str,
                                                          target: str) -> None:
    body = {"config": {ROOT: KEY_B}}
    seen = door.raw(api_key, method, path, body=body,
                    cluster=Cluster(ConfigSession({("default", target): {"config": {ROOT: KEY_A}}})))
    _passed(seen)
    assert seen.cluster.session.reads == []


@pytest.mark.parametrize("api_key,project", DENIED[:4], ids=DENIED_IDS[:4])
@pytest.mark.parametrize("combo,held,config,priced", SSH_COMBOS, ids=[c[0] for c in SSH_COMBOS])
def test_proxy_update_prices_an_ssh_key_by_what_it_changes(door: Any, combo: str, held: Dict[str, Any],
                                                              config: Dict[str, Any], priced: bool,
                                                              api_key: Dict[str, Any], project: Any) -> None:
    seen = door.proxy(api_key, "update_instance", {"name": "web", "config": config}, cluster=proxy_cluster(held))
    assert seen.cluster.session.reads == [(ENDPOINT + "/instances/web", "default")]
    if priced:
        _priced(seen, reads=1)
    else:
        assert seen.status == 200 and seen.code is None and seen.refusals == [], seen.text
        assert [(verb, name) for verb, name, _body in seen.cluster.writes] == [("save", "web")]


@pytest.mark.parametrize("combo,held,config,priced", SSH_COMBOS, ids=[c[0] for c in SSH_COMBOS])
async def test_mcp_update_prices_an_ssh_key_by_what_it_changes(door: Any, combo: str, held: Dict[str, Any],
                                                                  config: Dict[str, Any], priced: bool) -> None:
    seen = await door.mcp(tools_write, "lxd_update_instance", NO_GUEST, proxy_cluster(held), cluster_id=CLUSTER_ID,
                          name="web", config=config)
    if priced:
        _priced(seen, reads=1)
    else:
        assert seen.status == 200 and [verb for verb, *_ in seen.cluster.writes] == ["save"], seen.text


# note: OrcaHub v1.32.0's read-modify-write on an instance the dashboard gave a login key

EDITABLE = {"architecture": "x86_64", "config": {"limits.cpu": "2", "limits.memory": "2GiB", ROOT: KEY_A,
                                                 "volatile.cloud-init.instance-id": "5b1e0a4c"},
            "devices": {"root": {"type": "disk", "path": "/", "pool": "default", "size": "20GiB"}},
            "profiles": ["default"], "description": "Plan - Order 1", "ephemeral": False, "stateful": False}
ORCAHUB_ON_A_KEYED_INSTANCE = [
    ("PUT", {**EDITABLE, "config": {**EDITABLE["config"], "limits.cpu": "4", "limits.memory": "4GiB"}}),
    ("PUT", {**EDITABLE, "devices": {**EDITABLE["devices"], "data": {"type": "disk", "pool": "default",
                                                                    "source": "vol-1", "path": "/data"}}}),
    ("PATCH", {"config": {"snapshots.schedule": "@daily", "snapshots.expiry": "7d"}}),
    ("PATCH", {"config": {"security.secureboot": "false"}}),
]


@pytest.mark.parametrize("method,body", ORCAHUB_ON_A_KEYED_INSTANCE, ids=["resize", "attach", "schedule", "secureboot"])
def test_orcahubs_round_trip_on_a_keyed_instance_is_still_sent(door: Any, method: str, body: Dict[str, Any]) -> None:
    held = {("cust-buyer", "/instances/vm-7"): {"name": "vm-7", "config": EDITABLE["config"]}}
    cluster = Cluster(ConfigSession(held))
    seen = door.raw(ORCAHUB, method, "/1.0/instances/vm-7", body=body, project="cust-buyer", cluster=cluster)
    _passed(seen)
    assert seen.cluster.writes == [(method, body)]


def test_user_meta_data_is_sent_unpriced(door: Any) -> None:
    """Pinned as observed: no decision prices user.meta-data (LXD appends it to cloud-init's meta-data)."""
    meta = "public-keys:\n  - ssh-ed25519 AAAA"
    body = {"name": "c1", "source": {"type": "none"}, "config": {"user.meta-data": meta}}
    _passed(door.raw(NO_GUEST, "POST", "/1.0/instances", body=body))


# note: trust surface refusal records, each answer compared with the gate's own


def _answer(gate: Callable[[], None]) -> HTTPException:
    with pytest.raises(HTTPException) as refused:
        gate()
    return refused.value


def _recorded(seen: Any, expected: HTTPException, path: str, method: str, project: str, email: Any = None) -> None:
    assert (seen.status, seen.text) == (expected.status_code, str(expected.detail))
    assert seen.cluster.untouched, seen.cluster.session.calls
    assert len(seen.records) == 1, seen.records
    record = seen.records[0]
    assert record["result"] == "failure" and record["error_message"] == str(expected.detail)
    assert record["details"]["refused"] == "refused_for_every_key" and record["details"]["method"] == method
    assert record["details"]["claimed_user_email"] == email and record["details"]["api_key_id"] == "oak_test"
    assert record["target_id"] == path[:512] and record["target_host"] == CLUSTER_ID
    assert record["target_project"] == project and record["action"].startswith("integration.")


TRUST_TEMPLATES = ["/1.0/{certificates}", "/1.0/{certificates}/0f3c9a1b", "/1.0/{auth}", "/1.0/{auth}/{groups}/g1",
                   "/1.0/{auth}/identities/oidc/ops@example.test", "/1.0/{auth}/identity-provider-groups/admins"]
KEYS = [(EVERY, None), (LEGACY, None), (NARROW, "tenant-a"), (ORCAHUB, "cust-buyer"), (NO_GUEST, None)]
KEY_IDS = ["every", "legacy", "narrow", "orcahub", "no-guest"]


TRUST_CASES = [(how, method) for how in SPELLING_IDS
               for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "get", "Delete")
               if how != "query" or method in ("GET", "DELETE")]


@pytest.mark.parametrize("api_key,project", KEYS, ids=KEY_IDS)
@pytest.mark.parametrize("how,method", TRUST_CASES, ids=[f"{h}-{m}" for h, m in TRUST_CASES])
@pytest.mark.parametrize("template", TRUST_TEMPLATES)
def test_trust_store_and_auth_refusal_is_recorded(door: Any, template: str, how: str, method: str,
                                                     api_key: Dict[str, Any], project: Any) -> None:
    path = spell(template, how)
    body = {"name": "x", "type": "client"} if method.upper() in ("POST", "PUT", "PATCH") else None
    seen = door.raw(api_key, method, path, body=body, project=project)
    _recorded(seen, _answer(lambda: assert_proxy_path_allowed(path)), path, method.upper(), project or "default")


@pytest.mark.parametrize("path", ["/1.0/certificates#frag", "/1.0/%2563ertificates", "/1.0/certificates?x=1&project=o",
                                  "/1.0/Auth/Identities", "/1.0/%61uth", "/1.0/auth/instances/web/exec",
                                  "/1.0/certificates/" + "a" * 600])
def test_odd_trust_paths_are_recorded_once(door: Any, path: str) -> None:
    seen = door.raw(EVERY, "POST", path, body={"name": "x"}, project="default")
    _recorded(seen, _answer(lambda: assert_proxy_path_allowed(path)), path, "POST", "default")


SERVER_WRITES = ["", "/", "1.0", "/1.0", "/1.0/", "/1.0?target=nuc-1", "/1.0/cluster", "/1.0/cluster/members",
                 "/1.0/cluster/members/nuc-1/state", "/1.0/cluster/certificate", "/1.0/cluster/groups",
                 "/1.0/CLUSTER/members", "/1.0/%63luster/members", "/1.0/cluster/members?x=1"]


@pytest.mark.parametrize("api_key,project", KEYS, ids=KEY_IDS)
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", SERVER_WRITES, ids=repr)
def test_server_and_cluster_writes_are_recorded(door: Any, path: str, method: str, api_key: Dict[str, Any],
                                                   project: Any) -> None:
    body = None if method == "DELETE" else {"config": {"core.https_address": ":8443"}}
    seen = door.raw(api_key, method, path, body=body, project=project)
    _recorded(seen, _answer(lambda: assert_server_state_unchanged(path, method)), path, method, project or "default")


@pytest.mark.parametrize("path", ["/1.0", "/1.0/cluster", "/1.0/cluster/members", "/1.0/cluster/members?recursion=1",
                                  "/1.0/resources"])
def test_orcahubs_server_reads_are_sent_with_only_a_success_record(door: Any, path: str) -> None:
    seen = door.raw(ORCAHUB, "GET", path, project="cust-buyer", cluster=Cluster(RecordingSession()))
    assert seen.status < 500 and seen.status not in (400, 403) and seen.refusals == [], (seen.status, seen.text)
    assert [record["result"] for record in seen.records] == ["success"]


BUCKET_KEYS = ["/1.0/storage-pools/s3/buckets/b1/keys", "/1.0/storage-pools/s3/buckets/b1/keys/k1",
               "/1.0/storage-pools/s3/buckets/b1/keys/", "/1.0/Storage-Pools/s3/BUCKETS/b1/%6Beys/k1",
               "/1.0/storage-pools/s3/buckets/b1/keys?recursion=1", "/1.0/storage-pools/s3/buckets/b1/%256Beys",
               "/1.0/storage-pools/s3/buckets/keys/keys/k1"]


@pytest.mark.parametrize("api_key,project", KEYS, ids=KEY_IDS)
@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", BUCKET_KEYS)
def test_bucket_key_refusal_is_recorded(door: Any, path: str, method: str, api_key: Dict[str, Any],
                                           project: Any) -> None:
    body = {"name": "k2", "role": "admin"} if method in ("POST", "PUT", "PATCH") else None
    seen = door.raw(api_key, method, path, body=body, project=project)
    _recorded(seen, _answer(lambda: assert_bucket_keys_unreached(path)), path, method, project or "default")


def test_the_claimed_email_is_kept_as_an_annotation(door: Any) -> None:
    payload = {"cluster_id": CLUSTER_ID, "method": "GET", "path": "/1.0/certificates",
               "external_user_email": "someone@example.test", "external_user_id": "u-1"}
    seen = door._send(EVERY, Cluster(), "POST", "/api/v1/integrations/raw-proxy", json=payload)
    _recorded(seen, _answer(lambda: assert_proxy_path_allowed("/1.0/certificates")), "/1.0/certificates", "GET",
              "default", email="someone@example.test")


def test_a_failing_audit_writer_leaves_the_answer_unchanged(door: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    class Down:
        def log_activity(self, **_: Any) -> None:
            raise RuntimeError("audit index down")

    monkeypatch.setattr(integrations, "activity_logger", Down())
    for method, path, gate in (("GET", "/1.0/certificates", lambda: assert_proxy_path_allowed("/1.0/certificates")),
                               ("PUT", "/1.0", lambda: assert_server_state_unchanged("/1.0", "PUT")),
                               ("GET", BUCKET_KEYS[0], lambda: assert_bucket_keys_unreached(BUCKET_KEYS[0]))):
        seen = door.raw(EVERY, method, path, body={"config": {}} if method == "PUT" else None)
        expected = _answer(gate)
        assert (seen.status, seen.text) == (expected.status_code, str(expected.detail)) and seen.cluster.untouched


# note: refusals that come before the trust surface stay unrecorded, as before


def test_refusals_before_the_trust_surface_are_not_recorded(door: Any) -> None:
    other = {**EVERY, "cluster_permissions": [{"cluster_id": "cluster-b", "permissions": ["read"], "projects": ["*"]}]}
    for api_key, method, project, status in ((other, "GET", None, 403), (NARROW, "GET", "tenant-b", 403),
                                             (EVERY, "HEAD", None, 400), (EVERY, "OPTIONS", None, 400)):
        seen = door.raw(api_key, method, "/1.0/certificates", project=project)
        assert seen.status == status and seen.records == [] and seen.cluster.untouched, (seen.status, seen.text)


@pytest.mark.parametrize("path", ["/1.0/./certificates", "/1.0/certificates/..", "/1.0/auth/%2e%2e/instances",
                                  "/1.0/cluster/%252e%252e", "/1.0/storage-pools/s3/buckets/b1/keys/..",
                                  "/1.0/%" + "25" * 9 + "63ertificates"])
@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_a_malformed_trust_path_is_a_400_without_a_record(door: Any, path: str, method: str) -> None:
    seen = door.raw(EVERY, method, path, body={"config": {}} if method == "PUT" else None)
    assert seen.status == 400 and seen.cluster.untouched and seen.records == [], (seen.status, seen.text)


def test_one_record_per_refusal_whichever_gate_answers(door: Any) -> None:
    records: List[Any] = []
    for method, path in (("PUT", "/1.0/cluster/certificate"), ("DELETE", "/1.0/certificates/x"),
                         ("DELETE", BUCKET_KEYS[1])):
        seen = door.raw(EVERY, method, path, body={"config": {}} if method == "PUT" else None)
        records.append([record["action"] for record in seen.records])
    assert records == [["integration.proxy.request"], ["integration.certificate.delete"],
                       ["integration.storage.pool.delete"]]
