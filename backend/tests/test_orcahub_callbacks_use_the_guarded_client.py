"""Every OrcaHub callback goes out through guarded_request.

The member push carries the key's raw secret, the webhook is signed with it and the refresh GET
carries its hash, and all three fetch an app_url whoever holds the key chose. Raw httpx followed
no URL policy at all. Constructing an httpx.AsyncClient directly now fails these tests, so a
call site reverted to raw httpx is caught.

The webhook contract is pinned too: OrcaHub checks HMAC(secret, ts + "." + raw body) over the
exact bytes it receives, so the body travels as content= bytes and the header must verify.
"""

import hashlib
import hmac
import logging
from types import SimpleNamespace
from typing import Any, Dict, List

import httpx
import pytest

from app.core.safe_http import GuardedResponse
from app.core.url_safety import UnsafeURLError
from app.services import orcahub_sync, orcahub_webhook

CREDS = {
    "app_url": "https://example.test",
    "api_key_id": "oak_test",
    "api_key_secret": "oas_unit_test_secret",
}


class _NoRawHttpx:
    def __init__(self, *_a: Any, **_k: Any) -> None:
        raise AssertionError("an OrcaHub callback must not construct a raw httpx client")


class Recorder:
    def __init__(self, raises: Exception = None, status: int = 200) -> None:
        self.raises = raises
        self.status = status
        self.calls: List[Dict[str, Any]] = []

    async def __call__(self, method: str, url: str, **kwargs: Any) -> GuardedResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if method == "GET" and url.endswith("/api/external/organizations"):
            # note: the member push first asks OrcaHub which org the connection belongs to.
            return GuardedResponse(
                200, "application/json", b'{"data":{"organizations":[{"id":"ext-home"}]}}'
            )
        if self.raises is not None:
            raise self.raises
        return GuardedResponse(self.status, "application/json", b"{}")

    @property
    def posts(self) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c["method"] == "POST"]


@pytest.fixture
def no_raw_httpx(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _NoRawHttpx)


class _OrgResult:
    def __init__(self, org: Any) -> None:
        self.org = org

    def scalar_one_or_none(self) -> Any:
        return self.org


class _OrgSession:
    def __init__(self, org: Any) -> None:
        self.org = org

    async def execute(self, *_a: Any, **_k: Any) -> _OrgResult:
        return _OrgResult(self.org)


def _org() -> SimpleNamespace:
    return SimpleNamespace(
        id=7, slug="org-a", source="orcahub", api_key_id="oak_test", external_id="ext-home"
    )


@pytest.fixture
def push(monkeypatch, no_raw_httpx):
    recorder = Recorder()
    monkeypatch.setattr(orcahub_sync, "_home_org_cache", {})

    async def _creds(_org: Any) -> Dict[str, str]:
        return dict(CREDS)

    monkeypatch.setattr(orcahub_sync, "guarded_request", recorder)
    monkeypatch.setattr(orcahub_sync, "_get_orcahub_credentials", _creds)
    return recorder


async def test_the_member_push_goes_through_the_guarded_client(push):
    ok = await orcahub_sync.push_members_to_orcahub(
        _OrgSession(_org()), 7, [{"email": "tenant@example.test", "role": "tenant", "action": "sync"}]
    )
    assert ok is True
    assert len(push.posts) == 1
    assert all(c["max_redirects"] == 0 and c["require_https"] is True for c in push.calls)
    call = push.posts[0]
    assert call["url"] == "https://example.test/api/external/members/sync"
    assert call["max_redirects"] == 0
    assert call["require_https"] is True
    assert call["headers"]["X-API-Key-Secret"] == CREDS["api_key_secret"]
    assert call["max_bytes"] == orcahub_sync.CALLBACK_RESPONSE_MAX_BYTES


async def test_a_refused_member_push_url_returns_false(push, caplog):
    push.raises = UnsafeURLError("host resolves to an internal address")
    caplog.set_level(logging.INFO, logger=orcahub_sync.logger.name)
    ok = await orcahub_sync.push_members_to_orcahub(
        _OrgSession(_org()), 7, [{"email": "tenant@example.test", "role": "tenant", "action": "sync"}]
    )
    assert ok is False
    assert len(push.posts) == 1
    refused = [r for r in caplog.records if "refused" in r.getMessage()]
    assert refused and all(r.levelno == logging.WARNING for r in refused)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], (
        "a URL the policy refuses is a configuration fact, not a delivery failure"
    )


async def test_an_error_status_from_the_member_push_returns_false(push):
    push.status = 400
    ok = await orcahub_sync.push_members_to_orcahub(
        _OrgSession(_org()), 7, [{"email": "tenant@example.test", "role": "tenant", "action": "sync"}]
    )
    assert ok is False


class _SessionMaker:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_a: Any) -> None:
        return None


@pytest.fixture
def webhook(monkeypatch, no_raw_httpx):
    recorder = Recorder()

    async def _orgs(_db: Any, _cluster: str) -> List[Any]:
        return [_org()]

    async def _creds(_org: Any) -> Dict[str, str]:
        return dict(CREDS)

    async def _no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(orcahub_webhook, "guarded_request", recorder)
    monkeypatch.setattr(orcahub_webhook, "_orcahub_orgs_for_cluster", _orgs)
    monkeypatch.setattr(orcahub_webhook, "_get_orcahub_credentials", _creds)
    monkeypatch.setattr(orcahub_webhook, "async_session_maker", _SessionMaker)
    monkeypatch.setattr(orcahub_webhook.asyncio, "sleep", _no_sleep)
    return recorder


async def test_the_webhook_signs_the_exact_bytes_it_sends(webhook):
    await orcahub_webhook._emit("INSTANCE_CREATED", "cluster-a", "proj-a", "vm-1", "Running", "INSTANCE")
    assert len(webhook.calls) == 1
    call = webhook.calls[0]
    body = call["content"]
    assert isinstance(body, bytes), "the webhook must send raw bytes, never json="
    assert call.get("json") is None
    headers = call["headers"]
    expected = hmac.new(
        CREDS["api_key_secret"].encode(),
        headers["X-Orcastra-Timestamp"].encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    assert headers["X-Orcastra-Signature"] == expected
    assert headers["Content-Type"] == "application/json"
    assert call["url"] == "https://example.test/api/webhooks/orcastra"
    assert call["max_redirects"] == 0
    assert call["require_https"] is True


async def test_a_refused_webhook_url_is_not_retried(webhook):
    webhook.raises = UnsafeURLError("host resolves to an internal address")
    await orcahub_webhook._emit("INSTANCE_CREATED", "cluster-a", None, "vm-1", None, "INSTANCE")
    assert len(webhook.calls) == 1


async def test_a_transport_failure_is_still_retried(webhook):
    webhook.raises = RuntimeError("could not reach the URL")
    await orcahub_webhook._emit("INSTANCE_CREATED", "cluster-a", None, "vm-1", None, "INSTANCE")
    assert len(webhook.calls) == 3


async def test_guarded_request_sends_content_bytes_unchanged():
    """The contract the webhook relies on, through the real guarded_request and a mock transport."""
    from app.core import safe_http

    seen: Dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        seen["content_type"] = request.headers.get("content-type", "")
        return httpx.Response(200, json={})

    payload = b'{"b":1,"a":"x y"}'
    response = await safe_http.guarded_request(
        "POST", "https://93.184.216.34/api/webhooks/orcastra",
        content=payload, headers={"Content-Type": "application/json"},
        max_bytes=1024, transport=httpx.MockTransport(handler),
    )
    assert response.status_code == 200
    assert seen["body"] == payload
    assert seen["content_type"] == "application/json"
