"""
A raw-proxy refusal on the trust surface leaves an integration refusal record, and its answer is unchanged.

The trust store and /auth (every method), server and cluster writes, and storage bucket keys are refused for
every key and grant. They used to answer 403 without a record while the guest gates beside them wrote one,
so a key probing the trust surface left nothing in the trail an Admin reads. Each case sends one request
through the real route (tests/_guest_gate_doors.py) and asserts the gate's own answer (status and detail,
read by calling the gate directly), nothing sent, and one failure record naming the path, the method and
`refused_for_every_key`. A malformed path is a 400 of the path parser, not a trust-surface refusal, and
stays unrecorded like every traversal 400.
"""

from typing import Any, Callable, Dict, List

import pytest
from fastapi import HTTPException

from app.core.api_key_capabilities import assert_proxy_path_allowed
from app.core.raw_proxy_path import assert_server_state_unchanged
from app.core.storage_bucket_keys import assert_bucket_keys_unreached
from app.routers import integrations
from tests._guest_gate_doors import (  # noqa: F401
    EVERY, LEGACY, NARROW, ORCAHUB, SPELLING_IDS, door_fixture, spell,
)
from tests._limited_write_fakes import _no_webhook_events  # noqa: F401
from tests._raw_proxy_fakes import CLUSTER_ID, _clean_breaker, fence_calls  # noqa: F401

METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
WRITES = ["POST", "PUT", "PATCH", "DELETE"]
KEYS = [(EVERY, None), (LEGACY, None), (NARROW, "tenant-a"), (ORCAHUB, "cust-buyer")]
KEY_IDS = ["every", "legacy", "narrow", "orcahub"]


def _answer(gate: Callable[[], None]) -> HTTPException:
    with pytest.raises(HTTPException) as refused:
        gate()
    return refused.value


def _recorded(seen: Any, expected: HTTPException, path: str, method: str) -> None:
    assert (seen.status, seen.text) == (expected.status_code, str(expected.detail))
    assert seen.cluster.untouched, seen.cluster.session.calls
    records = [record for record in seen.records if record.get("result") == "failure"]
    assert len(records) == 1, seen.records
    record = records[0]
    assert record["details"]["refused"] == "refused_for_every_key" and record["details"]["method"] == method
    assert record["target_id"] == path and record["target_host"] == CLUSTER_ID
    assert record["error_message"] == str(expected.detail) and record["action"].startswith("integration.")


TRUST_PATHS: List[str] = [
    "/1.0/certificates", "/1.0/certificates/0f3c9a1b", "/1.0/certificates?public", "/1.0/auth/groups",
    "/1.0/auth/identities/tls/ops", "/1.0/auth/identity-provider-groups/admins",
]


@pytest.mark.parametrize("api_key,project", KEYS, ids=KEY_IDS)
@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("path", TRUST_PATHS)
def test_the_trust_store_and_auth_are_refused_with_a_record(
    door: Any, path: str, method: str, api_key: Dict[str, Any], project: Any
) -> None:
    body = {"name": "x"} if method in ("POST", "PUT", "PATCH") else None
    seen = door.raw(api_key, method, path, body=body, project=project)
    _recorded(seen, _answer(lambda: assert_proxy_path_allowed(path)), path, method)


@pytest.mark.parametrize("how", SPELLING_IDS)
@pytest.mark.parametrize("template", ["/1.0/{certificates}/0f3c9a1b", "/1.0/{auth}/{groups}"],
                         ids=["certificates", "auth"])
def test_every_spelling_of_the_trust_store_is_recorded(door: Any, how: str, template: str) -> None:
    path = spell(template, how)
    _recorded(door.raw(EVERY, "GET", path), _answer(lambda: assert_proxy_path_allowed(path)), path, "GET")


SERVER_PATHS = ["/1.0", "/1.0/", "/1.0/cluster", "/1.0/cluster/members", "/1.0/cluster/members/nuc-1",
                "/1.0/cluster/groups/default", "/1.0/CLUSTER/certificate"]


@pytest.mark.parametrize("method", WRITES)
@pytest.mark.parametrize("path", SERVER_PATHS)
def test_a_server_or_cluster_write_is_refused_with_a_record(door: Any, path: str, method: str) -> None:
    body = {"config": {"core.https_address": ":8443"}} if method != "DELETE" else None
    seen = door.raw(EVERY, method, path, body=body)
    _recorded(seen, _answer(lambda: assert_server_state_unchanged(path, method)), path, method)


@pytest.mark.parametrize("path", SERVER_PATHS)
def test_a_server_or_cluster_read_is_sent_without_a_record(door: Any, path: str) -> None:
    seen = door.raw(EVERY, "GET", path)
    assert seen.status < 500 and seen.status not in (400, 403) and len(seen.records) == 1, (seen.status, seen.text)
    assert [verb for verb, *_ in seen.cluster.session.calls] == ["GET"] and seen.refusals == []


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("path", ["/1.0/storage-pools/s3/buckets/b1/keys", "/1.0/storage-pools/s3/buckets/b1/keys/k1",
                                  "/1.0/Storage-Pools/s3/BUCKETS/b1/%6Beys/k1"])
def test_bucket_keys_are_refused_with_a_record(door: Any, path: str, method: str) -> None:
    body = {"name": "k2", "role": "admin"} if method in ("POST", "PUT", "PATCH") else None
    seen = door.raw(LEGACY, method, path, body=body)
    _recorded(seen, _answer(lambda: assert_bucket_keys_unreached(path)), path, method)


@pytest.mark.parametrize("namer", ["real", "any-path"])
@pytest.mark.parametrize("path", ["/1.0/./certificates", "/1.0/certificates/../instances", "/1.0/cluster/%2e%2e"])
def test_a_malformed_path_is_a_400_without_a_record(
    door: Any, monkeypatch: pytest.MonkeyPatch, path: str, namer: str
) -> None:
    if namer == "any-path":
        # note: the record's action namer parses the path too, so it is replaced to show the gate itself
        # declines to record the parser's 400.
        monkeypatch.setattr(integrations, "_proxy_audit_action", lambda _method, _path: "integration.proxy.request")
    seen = door.raw(EVERY, "DELETE", path)
    assert seen.status == 400 and "traversal" in seen.text and seen.cluster.untouched
    assert seen.records == []


def test_a_request_outside_the_trust_surface_is_not_recorded_as_one(door: Any) -> None:
    seen = door.raw(EVERY, "POST", "/1.0/instances", body={"name": "c1", "source": {"type": "none"}})
    assert seen.status == 200 and [record["result"] for record in seen.records] == ["success"]
    assert seen.refusals == []
