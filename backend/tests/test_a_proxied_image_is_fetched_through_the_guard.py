"""The image proxy fetches through guarded_request, and nothing else under app/ judges addresses.

proxy-image checked a URL with its own private-range table and a synchronous getaddrinfo on the
event loop, then fetched with follow_redirects=True. The table missed 0.0.0.0/8, link-local IPv6,
mapped and tunnelled literals, a redirect was never re-checked, the name was resolved again by the
client, and the whole body was read before its size was checked.

Hermetic: the resolver is url_safety._getaddrinfo and HTTP goes to an httpx.MockTransport.
"""

import ast
import ipaddress
import socket
from pathlib import Path
from typing import Any, List

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import rbac, url_safety
from app.core.auth import AuthenticatedUser
from app.routers import user_preferences

APP = Path(__file__).resolve().parents[1] / "app"
PUBLIC_V4 = "93.184.215.14"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class Recorder:
    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.requests: List[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


@pytest.fixture()
def resolved(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    lookups: List[str] = []

    def _resolver(host: str, port: int) -> list:
        lookups.append(host)
        if host == "inside.example.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", port))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_V4, port))]

    monkeypatch.setattr(url_safety, "_getaddrinfo", _resolver)
    return lookups


def _client(monkeypatch: pytest.MonkeyPatch, handler: Any) -> "tuple[TestClient, Recorder]":
    recorder = Recorder(handler)
    monkeypatch.setattr(user_preferences, "_IMAGE_TRANSPORT", httpx.MockTransport(recorder))
    app = FastAPI()
    app.include_router(user_preferences.router, prefix="/api/v1")
    app.dependency_overrides[rbac.get_current_user] = lambda: AuthenticatedUser(
        user_id="tenant@example.test", email="tenant@example.test", groups=["role_tenant"]
    )
    return TestClient(app), recorder


def _post(client: TestClient, url: str) -> httpx.Response:
    return client.post("/api/v1/user-preferences/proxy-image", json={"url": url})


def _png(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)


def test_a_public_image_comes_back_as_a_data_uri(
    monkeypatch: pytest.MonkeyPatch, resolved: List[str]
) -> None:
    client, recorder = _client(monkeypatch, _png)
    response = _post(client, "https://cdn.example.test/logo.png")
    assert response.status_code == 200
    assert response.json()["data"].startswith("data:image/png;base64,")
    (sent,) = recorder.requests
    assert sent.url.host == PUBLIC_V4, "the connection must go to the address that was judged"
    assert sent.headers["host"] == "cdn.example.test"
    assert sent.extensions.get("sni_hostname") == "cdn.example.test"
    assert resolved == ["cdn.example.test"]


@pytest.mark.parametrize(
    "url",
    [
        "http://[::ffff:127.0.0.1]/x.png",
        "http://0.0.0.0/x.png",
        "http://169.254.169.254/latest/meta-data/",
        "http://[fe80::1]/x.png",
        "http://10.0.0.5/x.png",
        "http://inside.example.test/x.png",
        "http://localhost/x.png",
        "https://cdn.example.test:4050/x.png",
        "file:///etc/passwd",
        "gopher://cdn.example.test/x",
        "https://user:pass@cdn.example.test/x.png",
        "   ",
    ],
)
def test_a_refused_url_never_reaches_the_transport(
    monkeypatch: pytest.MonkeyPatch, resolved: List[str], url: str
) -> None:
    client, recorder = _client(monkeypatch, _png)
    response = _post(client, url)
    assert response.status_code == 400, response.text
    assert recorder.requests == []
    assert "10.1.2.3" not in response.text


def test_a_redirect_inside_is_refused_at_the_hop(
    monkeypatch: pytest.MonkeyPatch, resolved: List[str]
) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    client, recorder = _client(monkeypatch, _handler)
    response = _post(client, "https://cdn.example.test/logo.png")
    assert response.status_code == 400
    assert [r.url.host for r in recorder.requests] == [PUBLIC_V4]


def test_a_relative_redirect_is_followed_and_a_loop_ends(
    monkeypatch: pytest.MonkeyPatch, resolved: List[str]
) -> None:
    def _hop(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/moved.png":
            return _png(request)
        return httpx.Response(302, headers={"location": "/moved.png"})

    client, _ = _client(monkeypatch, _hop)
    assert _post(client, "https://cdn.example.test/logo.png").status_code == 200

    def _loop(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/again.png"})

    client, recorder = _client(monkeypatch, _loop)
    assert _post(client, "https://cdn.example.test/logo.png").status_code == 400
    assert len(recorder.requests) == user_preferences.PROXY_IMAGE_MAX_REDIRECTS + 1


def test_a_body_over_the_limit_is_abandoned_while_streaming(
    monkeypatch: pytest.MonkeyPatch, resolved: List[str]
) -> None:
    produced: List[int] = []
    chunk = b"\x00" * (256 * 1024)

    class _Stream(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[override]
            for _ in range(12):
                produced.append(len(chunk))
                yield chunk

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/png"}, stream=_Stream())

    client, _ = _client(monkeypatch, _handler)
    response = _post(client, "https://cdn.example.test/huge.png")
    assert response.status_code == 400
    assert sum(produced) < 12 * len(chunk), "the whole 3 MiB body was read before the refusal"


def test_a_non_image_is_refused(monkeypatch: pytest.MonkeyPatch, resolved: List[str]) -> None:
    def _json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}")

    client, _ = _client(monkeypatch, _json)
    response = _post(client, "https://cdn.example.test/health")
    assert response.status_code == 400
    assert "application/json" not in response.text


def test_an_overlong_url_is_refused_at_the_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    client, recorder = _client(monkeypatch, _png)
    response = _post(client, "https://cdn.example.test/" + "a" * 2100)
    assert response.status_code == 422
    assert recorder.requests == []


# ---------------------------------------------------------------------------
# Ratchets: one table of internal ranges, one resolver
# ---------------------------------------------------------------------------


def _cidr_literals(tree: ast.AST) -> List[str]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "/" in node.value:
            try:
                ipaddress.ip_network(node.value)
            except ValueError:
                continue
            found.append(node.value)
    return found


def test_there_is_one_table_of_internal_ranges() -> None:
    tables = {}
    for path in APP.rglob("*.py"):
        literals = _cidr_literals(ast.parse(path.read_text()))
        if len(literals) >= 2:
            tables[str(path.relative_to(APP))] = len(literals)
    assert set(tables) == {"core/url_safety.py"}, (
        f"a second range table drifts from url_safety and misses what it learns: {tables}"
    )


def test_nothing_resolves_a_name_outside_url_safety() -> None:
    offenders = []
    for path in APP.rglob("*.py"):
        if path.name == "url_safety.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            name = getattr(node, "attr", None) or getattr(node, "id", None)
            if name in {"getaddrinfo", "gethostbyname", "gethostbyname_ex"}:
                offenders.append(f"{path.relative_to(APP)}:{node.lineno}")
    assert not offenders, (
        "a resolver outside url_safety decides on an address the connection may never use: "
        + ", ".join(offenders)
    )
