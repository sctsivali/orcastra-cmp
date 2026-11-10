"""A request to a stored `app_url` connects to the address it judged, under the boundary it passed.

The lifecycle webhook, the member push and the organization refresh must not build a plain httpx
client from the stored URL: the name would be resolved again at every call, and a name that
answered a public address when it was stored could answer this network's Vault later. Each carries
a credential, so a redirect must not take one anywhere either.
"""

import ast
import asyncio
import ipaddress
import socket
from pathlib import Path

import httpx
import pytest

from app.core.safe_http import SafeHTTPError
from app.services import orcahub_callbacks
from app.services.orcahub_callbacks import get_from_app, post_to_app

PUBLIC = "93.184.216.34"


def _resolve_to(address: str):
    def resolve(host, port, *args, **kwargs):
        family = socket.AF_INET6 if ipaddress.ip_address(address).version == 6 else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, port or 443))]

    return resolve


@pytest.fixture
def public_name(monkeypatch):
    monkeypatch.setattr("app.core.url_safety.socket.getaddrinfo", _resolve_to(PUBLIC))


def _post(handler, url="https://hub.example.test/api/webhooks/orcastra"):
    return asyncio.run(post_to_app(url, content=b"{}", headers={"X-API-Key-ID": "oak_1"},
                                   transport=httpx.MockTransport(handler)))


def _get(handler, url="https://hub.example.test/api/external/organizations"):
    return asyncio.run(get_from_app(url, headers={"X-API-Key-Secret-Hash": "h"},
                                    transport=httpx.MockTransport(handler)))


def test_a_post_goes_to_the_judged_address_and_carries_the_name(public_name) -> None:
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers["host"], request.extensions.get("sni_hostname")))
        return httpx.Response(202, json={"ok": True})

    assert _post(handler)[0] == 202
    assert seen == [(PUBLIC, "hub.example.test", "hub.example.test")]


def test_a_get_goes_to_the_judged_address(public_name) -> None:
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(200, json={"organizations": []})

    assert _get(handler) == b'{"organizations":[]}'
    assert seen == [PUBLIC]


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "100.64.0.10"])
def test_a_name_that_answers_a_private_address_is_refused_before_any_request(monkeypatch, address) -> None:
    monkeypatch.setattr("app.core.url_safety.socket.getaddrinfo", _resolve_to(address))
    with pytest.raises(SafeHTTPError):
        _post(lambda request: pytest.fail("no request may be sent"))
    with pytest.raises(SafeHTTPError):
        _get(lambda request: pytest.fail("no request may be sent"))


def test_the_debug_webhook_exemptions_do_not_reach_a_callback(monkeypatch) -> None:
    from app.core import config

    monkeypatch.setattr(config, "webhook_destination_exemptions", lambda: (ipaddress.ip_network("10.0.0.0/8"),))
    monkeypatch.setattr(config, "webhook_requires_https", lambda: False)
    monkeypatch.setattr("app.core.url_safety.socket.getaddrinfo", _resolve_to("10.0.0.5"))
    with pytest.raises(SafeHTTPError):
        _post(lambda request: pytest.fail("no request may be sent"))
    with pytest.raises(SafeHTTPError):
        _post(lambda request: pytest.fail("no request may be sent"), url="http://hub.example.test/x")


def test_a_redirect_is_never_followed_with_the_credential(public_name) -> None:
    sent = []

    def handler(request):
        sent.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://collector.example.test/steal"})

    with pytest.raises(SafeHTTPError):
        _post(handler)
    with pytest.raises(SafeHTTPError):
        _get(handler)
    assert len(sent) == 2, "one request each, none to the redirect target"


def test_an_error_status_is_reported_by_number(public_name) -> None:
    with pytest.raises(SafeHTTPError) as post_refused:
        _post(lambda request: httpx.Response(500))
    with pytest.raises(SafeHTTPError) as get_refused:
        _get(lambda request: httpx.Response(401, json={"error": "Missing API key credentials"}))
    assert post_refused.value.status_code == 500
    assert get_refused.value.status_code == 401


def test_no_callback_builds_its_own_client() -> None:
    app_dir = Path(orcahub_callbacks.__file__).resolve().parents[1]
    offenders = []
    for path in (app_dir / "services" / "orcahub_sync.py", app_dir / "services" / "orcahub_webhook.py"):
        if "AsyncClient" in path.read_text():
            offenders.append(path.name)
    tree = ast.parse((app_dir / "routers" / "integrations.py").read_text())
    refresh = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "refresh_organizations")
    if "AsyncClient" in ast.unparse(refresh):
        offenders.append("integrations.refresh_organizations")
    assert not offenders, offenders
