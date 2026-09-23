"""A token is accepted only when it was minted for this application, by this issuer.

Signed with a real RSA key generated here, so the checks run through PyJWT exactly as they do in
production. The JWKS cache is seeded directly and nothing reaches the network.
"""

import time
from types import SimpleNamespace
from typing import Any, Dict, Optional

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

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
    client._last_fetched = time.time()
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
