"""An identity provider outage does not sign users out, block a worker, or answer with a 500.

The JWKS refresh is async and never raises. While the identity provider is down, keys fetched
earlier keep verifying tokens up to auth_jwks_max_stale, further fetch attempts are spaced out,
and past that the answer is a structured 503 with Retry-After.
"""

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException, WebSocketException
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from app.core import auth as auth_module
from app.core import jwks as jwks_module
from app.core.error_sanitizer import public_server_detail
from app.core.jwks import IDENTITY_PROVIDER_UNAVAILABLE, JWKSClient

ISSUER = "https://idp.example.test/application/o/orcastra/"
AUDIENCE = "orcastra-client"


def _settings(**over: Any) -> SimpleNamespace:
    base = dict(
        auth_enabled=True, authentik_issuer=ISSUER, authentik_audience=AUDIENCE,
        auth_jwks_cache_ttl=3600, auth_jwks_max_stale=86400,
    )
    base.update(over)
    return SimpleNamespace(**base)


class _Key:
    def __init__(self, kid: str) -> None:
        self.kid = kid
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private.public_key()))
        self.jwk = {**jwk, "kid": kid, "use": "sig", "alg": "RS256"}

    def token(self, **claims: Any) -> str:
        now = int(time.time())
        body = {"sub": "user-1", "email": "qa@example.test", "iss": ISSUER, "aud": AUDIENCE,
                "iat": now, "exp": now + 300, "groups": ["role_admin"], **claims}
        return jwt.encode(body, self.private, algorithm="RS256", headers={"kid": self.kid})


KEY_A = _Key("kid-a")


class _Clock:
    def __init__(self) -> None:
        self.wall = 1_000_000.0
        self.mono = 5_000.0

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(jwks_module, "_wall", lambda: c.wall)
    monkeypatch.setattr(jwks_module, "_mono", lambda: c.mono)
    return c


class _IdP:
    """The JWKS endpoint, answering what a test sets and counting what it was asked."""

    def __init__(self, keys: Optional[List[Dict[str, Any]]] = None) -> None:
        self.keys = keys if keys is not None else [KEY_A.jwk]
        self.down: Optional[Exception] = None
        self.reply: Optional[Dict[str, Any]] = None
        self.fetches = 0
        self.gate: Optional[asyncio.Event] = None

    def install(self, client: JWKSClient) -> JWKSClient:
        async def _discover(issuer: str) -> str:
            if self.down is not None:
                raise self.down
            return issuer + "jwks/"

        async def _fetch(uri: str) -> Dict[str, Any]:
            self.fetches += 1
            if self.gate is not None:
                await self.gate.wait()
            if self.down is not None:
                raise self.down
            return self.reply if self.reply is not None else {"keys": list(self.keys)}

        client._discover_jwks_uri_async = _discover  # type: ignore[method-assign]
        client._fetch_jwks_async = _fetch  # type: ignore[method-assign]
        return client


def _warm(clock: _Clock, idp: _IdP) -> JWKSClient:
    client = idp.install(JWKSClient())
    asyncio.run(client.warm_up(_settings()))
    assert idp.fetches == 1 and client._find(KEY_A.kid)
    return client


def _unavailable(exc: HTTPException) -> bool:
    return (exc.status_code == 503 and isinstance(exc.detail, dict)
            and exc.detail.get("error") == IDENTITY_PROVIDER_UNAVAILABLE
            and int((exc.headers or {}).get("Retry-After", "0")) >= 1)


# ── the outage itself ──────────────────────────────────────────────────────────────────────────

def test_a_failed_refresh_keeps_serving_the_last_good_keys(clock) -> None:
    idp = _IdP()
    client = _warm(clock, idp)
    clock.advance(3601)  # past the TTL
    idp.down = httpx.ConnectError("502 Bad Gateway")

    async def _go() -> Any:
        await client.ensure_keys(KEY_A.token(), _settings())
        key = client.get_signing_key(KEY_A.token(), _settings())
        await asyncio.sleep(0)  # let the background refresh run and fail
        await asyncio.sleep(0)
        return key

    assert asyncio.run(_go()) is not None
    assert client._find(KEY_A.kid), "the keys held must survive a failed refresh"
    assert client._last_error == "ConnectError"
    assert client.snapshot(_settings())["serving_stale"] is True


def test_a_known_kid_past_ttl_is_answered_without_waiting_for_the_fetch(clock) -> None:
    idp = _IdP()
    client = _warm(clock, idp)
    clock.advance(3601)

    async def _go() -> None:
        idp.gate = asyncio.Event()
        await asyncio.wait_for(client.ensure_keys(KEY_A.token(), _settings()), timeout=0.5)
        assert client.get_signing_key(KEY_A.token(), _settings()) is not None
        idp.gate.set()
        await client._refresh_task

    asyncio.run(_go())
    assert idp.fetches == 2


def test_keys_past_max_stale_answer_503_with_retry_after(clock) -> None:
    idp = _IdP()
    client = _warm(clock, idp)
    clock.advance(86401)
    idp.down = httpx.ConnectError("down")
    asyncio.run(client.ensure_keys(KEY_A.token(), _settings()))
    with pytest.raises(HTTPException) as refused:
        client.get_signing_key(KEY_A.token(), _settings())
    assert _unavailable(refused.value)


def test_max_stale_is_never_shorter_than_the_ttl(clock) -> None:
    idp = _IdP()
    client = _warm(clock, idp)
    settings = _settings(auth_jwks_cache_ttl=7200, auth_jwks_max_stale=60)
    clock.advance(3600)
    assert client.get_signing_key(KEY_A.token(), settings) is not None


def test_no_keys_and_no_idp_is_a_503_not_a_500(clock) -> None:
    idp = _IdP()
    idp.down = httpx.ConnectError("down")
    client = idp.install(JWKSClient())
    asyncio.run(client.ensure_keys(KEY_A.token(), _settings()))
    with pytest.raises(HTTPException) as refused:
        client.get_signing_key(KEY_A.token(), _settings())
    assert _unavailable(refused.value)


def test_the_identity_503_survives_the_production_handler() -> None:
    """The client keys on the error code, so the app's handler must keep the 503 dict intact."""
    from app.main import app

    request = Request({
        "type": "http", "method": "GET", "scheme": "https", "path": "/api/v1/integrations/api-keys",
        "raw_path": b"/api/v1/integrations/api-keys", "query_string": b"", "headers": [],
        "client": ("127.0.0.1", 12345), "server": ("testserver", 443), "root_path": "",
    })
    handler = app.exception_handlers[StarletteHTTPException]
    exc = HTTPException(503, detail={"error": IDENTITY_PROVIDER_UNAVAILABLE,
                                     "message": "unreachable at https://idp.example.test/x"},
                        headers={"Retry-After": "17"})
    response = asyncio.run(handler(request, exc))
    body = json.loads(response.body)
    assert response.status_code == 503
    assert body["detail"]["error"] == IDENTITY_PROVIDER_UNAVAILABLE
    assert "idp.example.test" not in body["detail"]["message"]
    assert response.headers["retry-after"] == "17"


@pytest.mark.parametrize("code", ["authority_unavailable", IDENTITY_PROVIDER_UNAVAILABLE])
def test_the_structured_503s_keep_their_shape(code: str) -> None:
    shown = public_server_detail(503, {"error": code, "message": "see /var/lib/x/y", "extra": 1}, "r")
    assert shown == {"error": code, "message": shown["message"]} and "/var/lib" not in shown["message"]


def test_an_unknown_structured_code_is_flattened() -> None:
    assert isinstance(public_server_detail(503, {"error": "made_up", "message": "m"}, "r"), str)


# ── unknown kids ───────────────────────────────────────────────────────────────────────────────

def test_an_unknown_kid_while_the_idp_is_down_is_503_not_401(clock) -> None:
    idp = _IdP()
    client = _warm(clock, idp)
    clock.advance(31)
    idp.down = httpx.ConnectError("down")
    stranger = _Key("kid-rotated")
    asyncio.run(client.ensure_keys(stranger.token(), _settings()))
    with pytest.raises(HTTPException) as refused:
        client.get_signing_key(stranger.token(), _settings())
    assert _unavailable(refused.value), "a rotated key cannot be told from a forged kid while down"


def test_an_unknown_kid_after_a_fresh_fetch_is_401_and_remembered(clock) -> None:
    idp = _IdP()
    client = _warm(clock, idp)
    clock.advance(31)
    stranger = _Key("kid-forged")
    asyncio.run(client.ensure_keys(stranger.token(), _settings()))
    with pytest.raises(HTTPException) as refused:
        client.get_signing_key(stranger.token(), _settings())
    assert refused.value.status_code == 401
    clock.advance(31)
    asyncio.run(client.ensure_keys(stranger.token(), _settings()))
    assert idp.fetches == 2, "a remembered absent kid must not refetch"


def test_a_rotated_in_key_is_picked_up(clock) -> None:
    idp = _IdP()
    client = _warm(clock, idp)
    clock.advance(31)
    rotated = _Key("kid-b")
    idp.keys = [KEY_A.jwk, rotated.jwk]
    asyncio.run(client.ensure_keys(rotated.token(), _settings()))
    assert client.get_signing_key(rotated.token(), _settings()) is not None


def test_random_kids_cost_at_most_one_fetch_per_spacing_window(clock) -> None:
    idp = _IdP()
    client = _warm(clock, idp)
    clock.advance(31)

    async def _go() -> None:
        for i in range(25):
            await client.ensure_keys(_Key(f"kid-random-{i}").token(), _settings())

    asyncio.run(_go())
    assert idp.fetches == 2


def test_concurrent_arrivals_share_one_refresh(clock) -> None:
    idp = _IdP()
    client = idp.install(JWKSClient())

    async def _go() -> None:
        idp.gate = asyncio.Event()
        waiters = [asyncio.create_task(client.ensure_keys(KEY_A.token(), _settings())) for _ in range(20)]
        await asyncio.sleep(0)
        idp.gate.set()
        await asyncio.gather(*waiters)

    asyncio.run(_go())
    assert idp.fetches == 1
    assert client.get_signing_key(KEY_A.token(), _settings()) is not None


def test_a_failed_refresh_holds_further_attempts(clock) -> None:
    idp = _IdP()
    idp.down = httpx.ConnectError("down")
    client = idp.install(JWKSClient())
    for _ in range(5):
        asyncio.run(client.ensure_keys(KEY_A.token(), _settings()))
    assert idp.fetches == 0 or idp.fetches == 1
    discovered_or_fetched = idp.fetches
    clock.advance(31)
    idp.down = None
    asyncio.run(client.ensure_keys(KEY_A.token(), _settings()))
    assert idp.fetches == discovered_or_fetched + 1
    assert client.get_signing_key(KEY_A.token(), _settings()) is not None


def test_a_jwks_without_keys_does_not_replace_the_good_set(clock) -> None:
    idp = _IdP()
    client = _warm(clock, idp)
    clock.advance(86401)
    idp.reply = {}
    asyncio.run(client.ensure_keys(KEY_A.token(), _settings()))
    assert client._find(KEY_A.kid) is not None
    assert client._last_error == "EmptyKeySet"


@pytest.mark.parametrize("failure", [
    ValueError("jwks_uri not found"),
    httpx.HTTPStatusError("502", request=httpx.Request("GET", "https://x"), response=httpx.Response(502)),
    json.JSONDecodeError("bad", "x", 0),
])
def test_ensure_keys_never_raises(clock, failure: Exception) -> None:
    idp = _IdP()
    idp.down = failure
    client = idp.install(JWKSClient())
    asyncio.run(client.ensure_keys(KEY_A.token(), _settings()))


def test_a_malformed_token_is_401_even_with_no_keys(clock) -> None:
    client = JWKSClient()
    with pytest.raises(HTTPException) as refused:
        client.get_signing_key("not-a-jwt", _settings())
    assert refused.value.status_code == 401


def test_an_unparsable_token_skips_the_refresh() -> None:
    """Nothing is fetched for garbage, and no settings attribute is read before the kid parses."""
    idp = _IdP()
    client = idp.install(JWKSClient())
    asyncio.run(client.ensure_keys("jwt", SimpleNamespace()))
    assert idp.fetches == 0


def test_get_signing_key_makes_no_network_call() -> None:
    import inspect

    for module in (auth_module, jwks_module):
        source = inspect.getsource(module)
        assert "import requests" not in source and "_sync(" not in source, module.__name__


# ── through the real dependencies ──────────────────────────────────────────────────────────────

class _Req:
    def __init__(self) -> None:
        self.state = SimpleNamespace()


def test_a_signed_in_request_is_verified_from_stale_keys_while_the_idp_is_down(clock, monkeypatch) -> None:
    idp = _IdP()
    client = _warm(clock, idp)
    clock.advance(7200)
    idp.down = httpx.ConnectError("down")
    monkeypatch.setattr(auth_module, "_jwks_client", client)
    credentials = SimpleNamespace(credentials=KEY_A.token(), scheme="Bearer")
    user = asyncio.run(auth_module.get_current_user(_Req(), credentials, _settings()))
    assert user.email == "qa@example.test"


def test_an_unreachable_idp_closes_a_legacy_token_socket_with_1011(clock, monkeypatch) -> None:
    idp = _IdP()
    idp.down = httpx.ConnectError("down")
    monkeypatch.setattr(auth_module, "_jwks_client", idp.install(JWKSClient()))
    socket = SimpleNamespace(query_params={"token": KEY_A.token()})
    with pytest.raises(WebSocketException) as closed:
        asyncio.run(auth_module.get_ws_user(socket, _settings()))
    assert closed.value.code == 1011
    assert len(closed.value.reason.encode()) < 123


def test_a_bad_token_on_a_socket_is_still_none(clock, monkeypatch) -> None:
    idp = _IdP()
    client = _warm(clock, idp)
    monkeypatch.setattr(auth_module, "_jwks_client", client)
    socket = SimpleNamespace(query_params={"token": KEY_A.token(aud="someone-else")})
    assert asyncio.run(auth_module.get_ws_user(socket, _settings())) is None
