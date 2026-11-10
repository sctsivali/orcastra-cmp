"""A guarded fetch connects to the address it judged, not to a name resolved again (DNS rebinding).

If `fetch_guarded` resolved the name to judge it and let httpx resolve it again to connect, a name
that answers a public address first and 127.0.0.1 the second time would pass the check and reach
the loopback. Through `POST /user-preferences/proxy-image`, open to every role, the body would come
back for an `image/*` answer and the error text would tell an internal port apart from a closed
one. Every hop is pinned as `post_pinned` pins.
"""

import asyncio
import socket

import httpx
import pytest

from app.core.safe_http import SafeHTTPError, fetch_guarded

PUBLIC = "93.184.216.34"


@pytest.fixture
def rebinding(monkeypatch):
    """The name answers a public address to every lookup the dashboard makes.

    httpx under MockTransport never resolves, so the rebinding itself (a later answer of 127.0.0.1)
    is not reproducible here. The tests assert what closes it: the request names the judged
    literal, never the name a resolver would be asked again at connect time.
    """
    def resolve(host, port, *args, **kwargs):
        if host != "rebind.example":
            raise socket.gaierror("unknown")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port or 443))]

    monkeypatch.setattr("app.core.url_safety.socket.getaddrinfo", resolve)


def _fetch(handler, url="https://rebind.example/logo.png"):
    return asyncio.run(fetch_guarded(url, max_bytes=1024, transport=httpx.MockTransport(handler)))


def test_the_request_goes_to_the_judged_address_and_carries_the_name(rebinding) -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("host"), request.extensions.get("sni_hostname")))
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"png")

    assert _fetch(handler) == ("image/png", b"png")
    assert seen == [(PUBLIC, "rebind.example", "rebind.example")]


def test_a_redirect_is_judged_and_pinned_again(rebinding) -> None:
    hosts = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if len(hosts) == 1:
            return httpx.Response(302, headers={"location": "https://rebind.example/next.png"})
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"ok")

    _fetch(handler)
    assert hosts == [PUBLIC, PUBLIC]


def test_a_name_that_answers_loopback_is_refused_before_any_request(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.core.url_safety.socket.getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port or 443))],
    )
    with pytest.raises(SafeHTTPError):
        _fetch(lambda request: pytest.fail("no request may be sent"))


def test_a_non_default_port_survives_in_the_host_header(rebinding, monkeypatch) -> None:
    seen = []
    monkeypatch.setattr("app.core.url_safety.describe_url_risk", lambda *a, **k: None, raising=False)
    monkeypatch.setattr("app.core.safe_http.describe_url_risk", lambda *a, **k: None)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.url.port, request.headers.get("host")))
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"ok")

    _fetch(handler, url="https://rebind.example:8443/logo.png")
    assert seen == [(PUBLIC, 8443, "rebind.example:8443")]
