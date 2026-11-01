"""A backend clock a few seconds off Authentik's does not refuse a token the dashboard was just handed.

jwt.decode ran with no leeway, so a host running ahead refused a fresh token as expired (or as
issued in the future), and the socket ticket minted with it came back 401 "Token has expired".
AUTH_JWT_LEEWAY_SECONDS (default 30, at most 120) now covers exp, iat and a ticket's session end.
"""

import asyncio
import json
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app.core import auth as auth_module
from app.core.auth import AuthenticatedUser
from app.core.config import Settings
from app.core.ws_auth import ws_user
from app.core.ws_ticket import WsTarget, claims_for
from app.core.ws_ticket_store import get_ws_ticket_store

ISSUER = "https://idp.example.test/application/o/orcastra/"
AUDIENCE = "orcastra-client"
PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    public = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps({
        **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(PRIVATE.public_key())), "kid": "k1",
    }))
    monkeypatch.setattr(auth_module._jwks_client, "get_signing_key", lambda token, settings: public)


def _settings(leeway: int = 30) -> SimpleNamespace:
    return SimpleNamespace(
        auth_enabled=True, authentik_issuer=ISSUER, authentik_audience=AUDIENCE, auth_jwt_leeway_seconds=leeway,
    )


def _token(**claims) -> str:
    now = int(time.time())
    body = {"sub": "u1", "email": "qa@example.test", "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 300, **claims}
    return jwt.encode(body, PRIVATE, algorithm="RS256", headers={"kid": "k1"})


def test_a_token_that_expired_twenty_seconds_ago_is_accepted():
    user = auth_module.validate_jwt(_token(exp=int(time.time()) - 20), _settings())

    assert user.email == "qa@example.test"


def test_a_token_that_expired_ninety_seconds_ago_is_refused():
    with pytest.raises(HTTPException) as refused:
        auth_module.validate_jwt(_token(exp=int(time.time()) - 90), _settings())

    assert refused.value.status_code == 401
    assert refused.value.detail == "Token has expired"


def test_a_token_issued_twenty_seconds_in_our_future_is_accepted():
    assert auth_module.validate_jwt(_token(iat=int(time.time()) + 20), _settings()).email == "qa@example.test"


def test_without_leeway_the_same_token_is_refused():
    """The setting is what does it: at 0 the check is to the second, as it was."""
    with pytest.raises(HTTPException):
        auth_module.validate_jwt(_token(exp=int(time.time()) - 20), _settings(leeway=0))


def test_the_leeway_is_bounded_and_documented():
    field = Settings.model_fields["auth_jwt_leeway_seconds"]

    assert field.alias == "AUTH_JWT_LEEWAY_SECONDS" and field.default == 30
    assert Settings(AUTH_JWT_LEEWAY_SECONDS=0).auth_jwt_leeway_seconds == 0
    assert Settings(AUTH_JWT_LEEWAY_SECONDS=120).auth_jwt_leeway_seconds == 120
    for out_of_range in (-1, 121):
        with pytest.raises(ValueError):
            Settings(AUTH_JWT_LEEWAY_SECONDS=out_of_range)


def _ticket_for(lifetime: float) -> str:
    user = AuthenticatedUser(
        user_id="skew@example.test", email="skew@example.test", groups=["role_admin"],
        roles=["role_admin"], session_exp=time.time() + lifetime,
    )
    claims = claims_for("tasks", WsTarget(), user)
    return asyncio.run(get_ws_ticket_store().mint(claims.user_id, claims.to_json()))


def _redeem(ticket: str):
    sock = SimpleNamespace(query_params={"ticket": ticket}, path_params={}, state=SimpleNamespace())
    settings = SimpleNamespace(auth_enabled=True, authentik_issuer=ISSUER, ws_query_token_enabled=False)
    return asyncio.run(ws_user("tasks")(sock, settings)), sock


def test_a_ticket_minted_just_before_its_session_ended_still_opens():
    user, _ = _redeem(_ticket_for(-20))

    assert user is not None and user.user_id == "skew@example.test"


def test_a_ticket_whose_session_ended_long_ago_is_refused_as_session_ended():
    user, sock = _redeem(_ticket_for(-90))

    assert user is None
    assert sock.state.ws_refusal == (4401, "session_ended")
