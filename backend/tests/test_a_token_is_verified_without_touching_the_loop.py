"""A token is accepted only when it was minted for this application, by this issuer.

Signed with a real RSA key generated here, so the checks run through PyJWT exactly as they do in
production. The JWKS cache is seeded directly and nothing reaches the network.
"""

import asyncio
import inspect
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.core import auth

ISSUER = "https://idp.example.test/application/o/orcastra/"
AUDIENCE = "orcastra-client"
KID = "kid-cluster-a"


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(private_key: rsa.RSAPrivateKey, kid: str = KID) -> Dict[str, Any]:
    key = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    key["kid"] = kid
    return key


def _settings(**overrides: Any) -> SimpleNamespace:
    values = {
        "auth_enabled": True,
        "authentik_issuer": ISSUER,
        "authentik_audience": AUDIENCE,
        "auth_jwks_cache_ttl": 3600,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _token(
    private_key: rsa.RSAPrivateKey,
    *,
    kid: str = KID,
    audience: Optional[str] = AUDIENCE,
    **claims: Any,
) -> str:
    payload: Dict[str, Any] = {
        "iss": ISSUER,
        "sub": "user-1",
        "email": "tenant@example.test",
        "groups": ["role_tenant"],
        "exp": int(time.time()) + 300,
    }
    if audience is not None:
        payload["aud"] = audience
    payload.update(claims)
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture()
def jwks(monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey) -> auth.JWKSClient:
    client = auth.JWKSClient()
    client._jwks = {"keys": [_jwk(signing_key)]}
    client._jwks_uri = "https://idp.example.test/jwks/"
    client._last_fetched = client._clock()
    monkeypatch.setattr(auth, "_jwks_client", client)
    return client


def test_a_token_for_this_audience_is_accepted(jwks: auth.JWKSClient, signing_key: rsa.RSAPrivateKey) -> None:
    user = auth.validate_jwt(_token(signing_key), _settings())
    assert user.user_id == "tenant@example.test"
    assert user.groups == ["role_tenant"]


def test_a_token_for_another_audience_is_refused(
    jwks: auth.JWKSClient, signing_key: rsa.RSAPrivateKey
) -> None:
    with pytest.raises(HTTPException) as exc:
        auth.validate_jwt(_token(signing_key, audience="another-app"), _settings())
    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid token audience"


def test_a_token_without_an_audience_is_refused(
    jwks: auth.JWKSClient, signing_key: rsa.RSAPrivateKey
) -> None:
    with pytest.raises(HTTPException) as exc:
        auth.validate_jwt(_token(signing_key, audience=None), _settings())
    assert exc.value.status_code == 401


def test_a_server_without_an_audience_refuses_as_its_own_fault(
    jwks: auth.JWKSClient, signing_key: rsa.RSAPrivateKey
) -> None:
    for token in (_token(signing_key), _token(signing_key, audience="another-app")):
        with pytest.raises(HTTPException) as exc:
            auth.validate_jwt(token, _settings(authentik_audience=""))
        assert exc.value.status_code == 503, (
            "with no audience configured any token the issuer signed for any application "
            f"would verify, got {exc.value.status_code}"
        )


@pytest.mark.asyncio
async def test_get_current_user_refuses_a_server_without_an_audience() -> None:
    class _Req:
        class state:  # noqa: D106
            pass

    with pytest.raises(HTTPException) as exc:
        await auth.get_current_user(_Req(), None, _settings(authentik_audience=""))
    assert exc.value.status_code == 503


# ---------------------------------------------------------------------------
# Key refresh: async, bounded, and never on the request's blocking path
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class _FetchSpy:
    def __init__(self, keys: list, *, fail: bool = False, delay: float = 0.0) -> None:
        self.keys = keys
        self.fail = fail
        self.delay = delay
        self.calls = 0

    async def __call__(self, jwks_uri: str) -> dict:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("identity provider unreachable")
        return {"keys": list(self.keys)}


@pytest.fixture()
def clock(jwks: auth.JWKSClient) -> _Clock:
    fake = _Clock()
    jwks._clock = fake
    jwks._last_fetched = fake()
    return fake


@pytest.fixture()
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    def _refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("key lookup reached the network")

    monkeypatch.setattr(requests, "get", _refuse)
    monkeypatch.setattr(httpx, "AsyncClient", _refuse)


async def test_random_kids_cost_one_fetch_per_interval(
    jwks: auth.JWKSClient, clock: _Clock, signing_key: rsa.RSAPrivateKey
) -> None:
    spy = _FetchSpy([_jwk(signing_key)])
    jwks._fetch_jwks_async = spy
    for _ in range(50):
        token = _token(signing_key, kid=f"kid-{uuid.uuid4().hex}")
        with pytest.raises(HTTPException) as exc:
            await auth.verify_token(token, _settings())
        assert exc.value.status_code == 401
    assert spy.calls == 1, f"50 unknown kids made {spy.calls} fetches to the identity provider"


async def test_a_rotated_key_is_found_once_the_interval_elapses(
    jwks: auth.JWKSClient, clock: _Clock, signing_key: rsa.RSAPrivateKey
) -> None:
    rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    spy = _FetchSpy([_jwk(signing_key)])
    jwks._fetch_jwks_async = spy
    with pytest.raises(HTTPException):
        await auth.verify_token(_token(rotated, kid="kid-rotated"), _settings())
    assert spy.calls == 1

    spy.keys = [_jwk(signing_key), _jwk(rotated, kid="kid-rotated")]
    clock.now += 10
    with pytest.raises(HTTPException):
        await auth.verify_token(_token(rotated, kid="kid-rotated"), _settings())
    assert spy.calls == 1, "a second forced refetch inside the interval"

    clock.now += auth.JWKSClient.FORCED_REFETCH_INTERVAL
    user = await auth.verify_token(_token(rotated, kid="kid-rotated"), _settings())
    assert user.email == "tenant@example.test"
    assert spy.calls == 2


async def test_concurrent_requests_share_one_fetch(
    jwks: auth.JWKSClient, clock: _Clock, signing_key: rsa.RSAPrivateKey
) -> None:
    jwks._jwks = {}
    jwks._jwks_uri = ""
    discovered = []

    async def _discover(issuer: str) -> str:
        discovered.append(issuer)
        return "https://idp.example.test/jwks/"

    spy = _FetchSpy([_jwk(signing_key)], delay=0.01)
    jwks._discover_jwks_uri_async = _discover
    jwks._fetch_jwks_async = spy
    token = _token(signing_key)
    await asyncio.gather(*(jwks.ensure_key_for(token, _settings()) for _ in range(20)))
    assert spy.calls == 1, f"20 concurrent requests made {spy.calls} fetches"
    assert len(discovered) == 1
    assert (await auth.verify_token(token, _settings())).email == "tenant@example.test"


async def test_a_failed_refresh_is_not_retried_within_the_backoff(
    jwks: auth.JWKSClient, clock: _Clock, signing_key: rsa.RSAPrivateKey
) -> None:
    jwks._jwks = {}
    spy = _FetchSpy([], fail=True)
    jwks._fetch_jwks_async = spy
    token = _token(signing_key)
    for _ in range(5):
        await jwks.ensure_key_for(token, _settings())
    assert spy.calls == 1

    clock.now += auth.JWKSClient.FAILED_REFRESH_BACKOFF + 0.1
    await jwks.ensure_key_for(token, _settings())
    assert spy.calls == 2


async def test_an_empty_cache_answers_503_not_a_crash(
    jwks: auth.JWKSClient, clock: _Clock, signing_key: rsa.RSAPrivateKey
) -> None:
    jwks._jwks = {}
    jwks._fetch_jwks_async = _FetchSpy([], fail=True)
    with pytest.raises(HTTPException) as exc:
        await auth.verify_token(_token(signing_key), _settings())
    assert exc.value.status_code == 503


async def test_stale_keys_stay_in_use_while_a_refresh_fails(
    jwks: auth.JWKSClient, clock: _Clock, signing_key: rsa.RSAPrivateKey
) -> None:
    clock.now += 7200
    spy = _FetchSpy([], fail=True)
    jwks._fetch_jwks_async = spy
    user = await auth.verify_token(_token(signing_key), _settings())
    assert user.email == "tenant@example.test"
    assert spy.calls == 1


class _BodySpy:
    """A fetch that succeeds with whatever body the identity provider sends."""

    def __init__(self, body: Any) -> None:
        self.body = body
        self.calls = 0

    async def __call__(self, jwks_uri: str) -> Any:
        self.calls += 1
        return self.body


@pytest.mark.parametrize("body", [{"keys": []}, {}, {"keys": "not-a-list"}, {"keys": ["x"]}])
async def test_an_answer_without_keys_counts_as_a_failed_refresh(
    jwks: auth.JWKSClient, clock: _Clock, signing_key: rsa.RSAPrivateKey, body: Any
) -> None:
    jwks._jwks = {}
    spy = _BodySpy(body)
    jwks._fetch_jwks_async = spy
    for _ in range(50):
        with pytest.raises(HTTPException) as exc:
            await auth.verify_token(_token(signing_key, kid=f"kid-{uuid.uuid4().hex}"), _settings())
        assert exc.value.status_code == 503
    assert spy.calls == 1, f"50 random kids made {spy.calls} fetches against a keyless answer"

    clock.now += auth.JWKSClient.FAILED_REFRESH_BACKOFF + 0.1
    with pytest.raises(HTTPException):
        await auth.verify_token(_token(signing_key), _settings())
    assert spy.calls == 2


async def test_a_keyless_answer_does_not_wipe_the_keys_in_use(
    jwks: auth.JWKSClient, clock: _Clock, signing_key: rsa.RSAPrivateKey
) -> None:
    clock.now += 7200
    spy = _BodySpy({"keys": []})
    jwks._fetch_jwks_async = spy
    for _ in range(20):
        user = await auth.verify_token(_token(signing_key), _settings())
        assert user.email == "tenant@example.test"
    assert spy.calls == 1
    assert jwks._find_key(KID) is not None


async def test_a_keyless_warm_up_leaves_the_cache_empty(
    jwks: auth.JWKSClient, clock: _Clock, signing_key: rsa.RSAPrivateKey
) -> None:
    jwks._jwks = {}

    async def _discover(issuer: str) -> str:
        return "https://idp.example.test/jwks/"

    jwks._discover_jwks_uri_async = _discover
    jwks._fetch_jwks_async = _BodySpy({"keys": "not-a-list"})
    await jwks.warm_up(_settings())
    assert jwks._jwks == {} and not jwks._warmed_up
    with pytest.raises(HTTPException) as exc:
        await auth.verify_token(_token(signing_key), _settings())
    assert exc.value.status_code == 503


def test_an_unknown_kid_is_refused_without_touching_the_network(
    jwks: auth.JWKSClient, no_network: None, signing_key: rsa.RSAPrivateKey
) -> None:
    kid = "kid-" + uuid.uuid4().hex
    with pytest.raises(HTTPException) as exc:
        auth.validate_jwt(_token(signing_key, kid=kid), _settings())
    assert exc.value.status_code == 401
    assert kid not in str(exc.value.detail), "the 401 echoed the caller's kid back"


def test_no_synchronous_network_path_remains() -> None:
    assert not hasattr(auth.JWKSClient, "_fetch_jwks_sync")
    assert not hasattr(auth.JWKSClient, "_discover_jwks_uri_sync")
    source = inspect.getsource(auth)
    assert "import requests" not in source


async def test_get_current_user_refreshes_before_it_validates(
    monkeypatch: pytest.MonkeyPatch, jwks: auth.JWKSClient, signing_key: rsa.RSAPrivateKey
) -> None:
    seen = []

    async def _spy(token: str, settings: Any) -> None:
        seen.append(token)

    monkeypatch.setattr(jwks, "ensure_key_for", _spy)
    token = _token(signing_key)

    class _Req:
        class state:  # noqa: D106
            pass

    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    user = await auth.get_current_user(_Req(), credentials, _settings())
    assert user is not None and user.email == "tenant@example.test"
    assert seen == [token], "get_current_user validated without giving the key cache a chance"

    seen.clear()
    socket = SimpleNamespace(query_params={"token": token})
    assert (await auth.get_ws_user(socket, _settings())).email == "tenant@example.test"
    assert seen == [token], "get_ws_user validated without giving the key cache a chance"


def test_only_the_auth_module_calls_validate_jwt() -> None:
    app_dir = Path(auth.__file__).resolve().parents[1]
    callers = []
    for path in app_dir.rglob("*.py"):
        if path.name == "auth.py" and path.parent.name == "core":
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if "validate_jwt(" in line.split("#", 1)[0]:
                callers.append(f"{path.relative_to(app_dir)}:{number}")
    assert not callers, (
        "validate_jwt only reads the key cache. Call verify_token so an empty or rotated cache "
        "is refreshed first: " + ", ".join(callers)
    )
