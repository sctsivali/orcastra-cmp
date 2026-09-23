"""
A server-side fetch is judged by the address it reaches, on every hop, at the moment it connects.

Hermetic: the resolver is replaced through url_safety._getaddrinfo and HTTP goes to an
httpx.MockTransport, so nothing here touches the network.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import threading
from typing import Any, Dict, List

import httpx
import pytest

from app.core import safe_http, url_safety
from app.core.safe_http import SafeHTTPError, guarded_request
from app.core.url_safety import (
    UnsafeURLError,
    address_risk,
    check_url,
    resolve_public_addresses,
    validate_url_field,
)

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "0.0.0.0",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "100.64.0.1",
        "169.254.169.254",
        "::1",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "2002:7f00:1::",
        "2001:0:4136:e378:8000:63bf:80ff:fffe",
        "fe80::1",
        "224.0.0.1",
        "240.0.0.1",
        "64:ff9b::a00:1",
    ],
)
def test_an_internal_literal_is_refused_whatever_its_spelling(address: str) -> None:
    assert address_risk(address) == url_safety.INTERNAL


@pytest.mark.parametrize("address", ["1.1.1.1", "2606:4700:4700::1111", PUBLIC_V4])
def test_a_public_address_passes(address: str) -> None:
    assert address_risk(address) is None


def test_an_operator_allowed_network_passes_but_never_a_metadata_endpoint() -> None:
    tailnet = [ipaddress.ip_network("100.64.7.7/32")]
    assert address_risk("100.64.7.7", allowed_networks=tailnet) is None
    assert address_risk("100.73.127.76", allowed_networks=tailnet) == url_safety.INTERNAL
    link_local = [ipaddress.ip_network("169.254.0.0/16")]
    assert address_risk("169.254.169.254", allowed_networks=link_local) == url_safety.INTERNAL


@pytest.mark.parametrize(
    "url,reason",
    [
        ("file:///etc/passwd", "scheme"),
        ("gopher://example.test/", "scheme"),
        ("https://user:pw@example.test/", "credentials"),
        ("https://localhost/", "not routable"),
        ("https://metadata.google.internal/", "not routable"),
        ("https://[::ffff:127.0.0.1]/", "internal"),
        ("https://10.0.0.5/", "internal"),
        ("", "malformed"),
        ("https://example.test:99999/", "malformed"),
    ],
)
def test_check_url_refuses_without_any_io(url: str, reason: str) -> None:
    with pytest.raises(UnsafeURLError) as caught:
        check_url(url)
    assert reason in str(caught.value)


def test_check_url_enforces_https_and_ports_only_when_asked() -> None:
    assert check_url("http://example.test/").port == 80
    with pytest.raises(UnsafeURLError):
        check_url("http://example.test/", require_https=True)
    with pytest.raises(UnsafeURLError):
        check_url("https://example.test:8443/", allowed_ports=frozenset({443}))
    assert check_url("https://example.test/", allowed_ports=frozenset({443})).port == 443


def test_a_hostname_is_not_resolved_at_the_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_: Any) -> list:
        raise AssertionError("check_url must not resolve")

    monkeypatch.setattr(url_safety, "_getaddrinfo", explode)
    assert check_url("https://example.test/path?q=1").host == "example.test"


def test_the_pydantic_adapter_names_the_field() -> None:
    with pytest.raises(ValueError) as caught:
        validate_url_field("http://10.0.0.1/", field="app_url")
    assert "app_url is not a URL this server may call" in str(caught.value)
    assert validate_url_field("https://example.test", field="app_url") == "https://example.test"


def _answers(*addresses: str) -> list:
    return [(0, 0, 0, "", (address, 0)) for address in addresses]


async def test_a_mixed_answer_is_refused_outright(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(url_safety, "_getaddrinfo", lambda host, port: _answers(PUBLIC_V4, "10.0.0.9"))
    with pytest.raises(UnsafeURLError):
        await resolve_public_addresses("example.test", 443)


async def test_resolution_runs_off_the_loop_and_puts_ipv4_first(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: Dict[str, int] = {}

    def resolver(host: str, port: int) -> list:
        seen["thread"] = threading.get_ident()
        return _answers(PUBLIC_V6, PUBLIC_V4)

    monkeypatch.setattr(url_safety, "_getaddrinfo", resolver)
    result = await resolve_public_addresses("example.test", 443)
    assert result == (PUBLIC_V4, PUBLIC_V6)
    assert seen["thread"] != threading.get_ident()


async def test_a_name_that_does_not_resolve_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(host: str, port: int) -> list:
        raise OSError("no such host")

    monkeypatch.setattr(url_safety, "_getaddrinfo", fail)
    with pytest.raises(UnsafeURLError, match="does not resolve"):
        await resolve_public_addresses("example.test", 443)


class Recorder:
    def __init__(self, routes: Dict[str, httpx.Response]) -> None:
        self.routes = routes
        self.requests: List[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = f"{request.headers.get('host')}{request.url.path}"
        return self.routes.get(key, httpx.Response(404))


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    lookups: List[str] = []

    def resolver(host: str, port: int) -> list:
        lookups.append(host)
        if host == "evil.example.test":
            return _answers("169.254.169.254")
        return _answers(PUBLIC_V4)

    monkeypatch.setattr(url_safety, "_getaddrinfo", resolver)
    return lookups


async def test_the_connection_is_pinned_to_the_judged_address(public_dns: List[str]) -> None:
    recorder = Recorder({"example.test/data": httpx.Response(200, json={"ok": True})})
    response = await guarded_request(
        "GET", "https://example.test/data", max_bytes=1024, transport=httpx.MockTransport(recorder)
    )
    assert response.status_code == 200 and response.json() == {"ok": True}
    sent = recorder.requests[0]
    assert sent.url.host == PUBLIC_V4
    assert sent.headers["host"] == "example.test"
    assert sent.extensions.get("sni_hostname") == "example.test"
    assert public_dns == ["example.test"]


async def test_a_unicode_host_is_called_by_its_a_label(public_dns: List[str]) -> None:
    assert check_url("https://B\u00dcCHER.example.test/").host == "xn--bcher-kva.example.test"
    recorder = Recorder({"xn--bcher-kva.example.test/logo.png": httpx.Response(200, content=b"ok")})
    response = await guarded_request(
        "GET", "https://b\u00fccher.example.test/logo.png", max_bytes=1024,
        transport=httpx.MockTransport(recorder),
    )
    assert response.body == b"ok"
    sent = recorder.requests[0]
    assert sent.url.host == PUBLIC_V4
    assert sent.headers["host"] == "xn--bcher-kva.example.test"
    assert sent.extensions.get("sni_hostname") == "xn--bcher-kva.example.test"
    assert public_dns == ["xn--bcher-kva.example.test"], "DNS must see the name the header carries"


@pytest.mark.parametrize("url", ["https://\u2603.example.test/x.png", "https://b\u00fccher..example.test/"])
async def test_a_unicode_host_without_an_a_label_is_malformed(public_dns: List[str], url: str) -> None:
    with pytest.raises(UnsafeURLError, match="malformed"):
        check_url(url)
    recorder = Recorder({})
    with pytest.raises(UnsafeURLError, match="malformed"):
        await guarded_request("GET", url, max_bytes=10, transport=httpx.MockTransport(recorder))
    assert recorder.requests == [] and public_dns == []


async def test_a_redirect_to_an_internal_address_is_refused_at_the_hop(public_dns: List[str]) -> None:
    recorder = Recorder({
        "example.test/start": httpx.Response(302, headers={"location": "http://169.254.169.254/latest"}),
    })
    with pytest.raises(UnsafeURLError):
        await guarded_request(
            "GET", "https://example.test/start", max_bytes=1024, max_redirects=3,
            transport=httpx.MockTransport(recorder),
        )
    assert all(r.url.host != "169.254.169.254" for r in recorder.requests)


async def test_a_redirect_to_a_name_that_resolves_inside_is_refused(public_dns: List[str]) -> None:
    recorder = Recorder({
        "example.test/start": httpx.Response(302, headers={"location": "https://evil.example.test/x"}),
    })
    with pytest.raises(UnsafeURLError):
        await guarded_request(
            "GET", "https://example.test/start", max_bytes=1024, max_redirects=3,
            transport=httpx.MockTransport(recorder),
        )
    assert len(recorder.requests) == 1


async def test_a_relative_redirect_is_followed_and_re_resolved(public_dns: List[str]) -> None:
    recorder = Recorder({
        "example.test/a": httpx.Response(301, headers={"location": "/b"}),
        "example.test/b": httpx.Response(200, content=b"done"),
    })
    response = await guarded_request(
        "GET", "https://example.test/a", max_bytes=1024, max_redirects=2,
        transport=httpx.MockTransport(recorder),
    )
    assert response.body == b"done"
    assert public_dns == ["example.test", "example.test"]


async def test_redirects_are_not_followed_without_hops_or_for_a_write(public_dns: List[str]) -> None:
    recorder = Recorder({"example.test/a": httpx.Response(302, headers={"location": "/b"})})
    with pytest.raises(SafeHTTPError, match="redirect not followed"):
        await guarded_request(
            "GET", "https://example.test/a", max_bytes=1024, transport=httpx.MockTransport(recorder)
        )
    with pytest.raises(SafeHTTPError, match="redirect not followed"):
        await guarded_request(
            "POST", "https://example.test/a", max_bytes=1024, max_redirects=3, json={},
            transport=httpx.MockTransport(recorder),
        )


async def test_a_large_body_is_abandoned_while_streaming(public_dns: List[str]) -> None:
    consumed: List[int] = []

    async def stream() -> Any:
        for _ in range(48):
            consumed.append(1)
            yield b"x" * 65536

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=stream())

    with pytest.raises(SafeHTTPError, match="size limit"):
        await guarded_request(
            "GET", "https://example.test/big", max_bytes=2 * 65536,
            transport=httpx.MockTransport(handler),
        )
    assert len(consumed) < 48


async def test_a_declared_length_over_the_limit_is_refused_before_reading(public_dns: List[str]) -> None:
    recorder = Recorder({"example.test/big": httpx.Response(200, content=b"x" * 5000)})
    with pytest.raises(SafeHTTPError, match="size limit"):
        await guarded_request(
            "GET", "https://example.test/big", max_bytes=100, transport=httpx.MockTransport(recorder)
        )


async def test_raw_content_reaches_the_receiver_byte_for_byte(public_dns: List[str]) -> None:
    body = b'{"event":"x","n":1}'
    signature = hmac.new(b"k", b"123." + body, hashlib.sha256).hexdigest()
    recorder = Recorder({"example.test/hook": httpx.Response(204)})
    await guarded_request(
        "POST", "https://example.test/hook", max_bytes=1024, content=body,
        headers={"X-Signature": signature, "Content-Type": "application/json"},
        transport=httpx.MockTransport(recorder),
    )
    sent = recorder.requests[0]
    assert sent.content == body
    assert hmac.compare_digest(
        hmac.new(b"k", b"123." + sent.content, hashlib.sha256).hexdigest(), sent.headers["x-signature"]
    )


async def test_json_and_content_are_exclusive() -> None:
    with pytest.raises(ValueError):
        await guarded_request("POST", "https://example.test/", max_bytes=1, json={}, content=b"x")


async def test_status_and_content_type_policies(public_dns: List[str]) -> None:
    recorder = Recorder({
        "example.test/err": httpx.Response(500, content=b"boom"),
        "example.test/img": httpx.Response(200, content=b"{}", headers={"content-type": "application/json"}),
    })
    transport = httpx.MockTransport(recorder)
    with pytest.raises(SafeHTTPError, match="upstream returned 500"):
        await guarded_request("GET", "https://example.test/err", max_bytes=100, transport=transport)
    response = await guarded_request(
        "GET", "https://example.test/err", max_bytes=100, raise_for_status=False, transport=transport
    )
    assert response.status_code == 500
    with pytest.raises(SafeHTTPError, match="content type"):
        await guarded_request(
            "GET", "https://example.test/img", max_bytes=100, content_type_prefix="image/",
            transport=transport,
        )


async def test_a_transport_failure_carries_no_address(public_dns: List[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connection to {request.url.host} refused")

    with pytest.raises(SafeHTTPError) as caught:
        await guarded_request(
            "GET", "https://example.test/", max_bytes=10, transport=httpx.MockTransport(handler)
        )
    assert PUBLIC_V4 not in str(caught.value)


def test_the_module_exports_the_refusal_type_callers_catch() -> None:
    assert safe_http.UnsafeURLError is UnsafeURLError
    assert issubclass(UnsafeURLError, ValueError)
    assert asyncio.iscoroutinefunction(guarded_request)
