"""The access log, app log and audit actor name a user only after the token verified.

RequestIdMiddleware runs before routing and seeded the request context from claims decoded without
verifying the signature. A forged token therefore wrote any email, group and role it liked into the
access index, including on requests that were answered 401.
"""

import base64
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.core import auth
from app.core.auth import AuthenticatedUser
from app.core.config import get_settings
from app.core.logging_config import StructuredJsonFormatter
from app.core.rbac import _populate_request_state
from app.core.request_context import get_request_context
from app.middleware.access_log import AccessLogMiddleware
from app.middleware.request_id import RequestIdMiddleware

ISSUER = "https://idp.example.test/application/o/orcastra/"
AUDIENCE = "orcastra-client"


def _segment(data: Dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


FORGED = (
    f"{_segment({'alg': 'none', 'typ': 'JWT'})}."
    f"{_segment({'email': 'attacker@example.test', 'sub': 'x', 'groups': ['orcastra-admins']})}."
    "unsigned"
)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        auth_enabled=True,
        authentik_issuer=ISSUER,
        authentik_audience=AUDIENCE,
        auth_jwks_cache_ttl=3600,
    )


def _app(seen: List[Dict[str, Any]]) -> FastAPI:
    app = FastAPI()

    def _snapshot() -> None:
        ctx = get_request_context()
        record = logging.LogRecord("app.test", logging.INFO, "", 0, "inside the route", (), None)
        seen.append(
            {
                "actor": ctx.get_actor() if ctx else None,
                "app_log": json.loads(StructuredJsonFormatter().format(record)),
            }
        )

    @app.get("/api/v1/open")
    async def _open() -> dict:
        _snapshot()
        return {"ok": True}

    @app.get("/api/v1/refused")
    async def _refused() -> dict:
        _snapshot()
        raise HTTPException(status_code=401, detail="Authentication required")

    @app.get("/api/v1/rbac")
    async def _rbac(request: Request) -> dict:
        _populate_request_state(
            request,
            AuthenticatedUser(user_id="tenant@example.test", groups=["role_tenant"]),
        )
        return {"ok": True}

    @app.get("/api/v1/me")
    async def _me(user: Optional[AuthenticatedUser] = Depends(auth.get_current_user)) -> dict:
        return {"user": user.user_id if user else None}

    app.dependency_overrides[get_settings] = _settings
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIdMiddleware)
    return app


def _entries(caplog: pytest.LogCaptureFixture) -> List[dict]:
    return [r.access_log_data for r in caplog.records if hasattr(r, "access_log_data")]


def _call(path: str, token: str, caplog: pytest.LogCaptureFixture, seen: List[dict]) -> dict:
    client = TestClient(_app(seen), raise_server_exceptions=False)
    with caplog.at_level(logging.INFO, logger="orcastra.access"):
        client.get(path, headers={"Authorization": f"Bearer {token}"})
    (entry,) = _entries(caplog)
    return entry


@pytest.mark.parametrize("path", ["/api/v1/open", "/api/v1/refused"])
def test_a_forged_token_names_nobody(path: str, caplog: pytest.LogCaptureFixture) -> None:
    seen: List[dict] = []
    entry = _call(path, FORGED, caplog, seen)
    assert entry["user"] is None, f"the access log named an unverified user: {entry['user']}"
    (inside,) = seen
    assert inside["actor"]["user_id"] is None and inside["actor"]["role"] is None
    assert inside["actor"]["groups"] == []
    assert "user_id" not in inside["app_log"]


def test_the_rbac_path_still_names_the_verified_user(caplog: pytest.LogCaptureFixture) -> None:
    entry = _call("/api/v1/rbac", FORGED, caplog, [])
    assert entry["user"]["id"] == "tenant@example.test"
    assert entry["user"]["role"] == "tenant"


@pytest.fixture()
def signing_key(monkeypatch: pytest.MonkeyPatch) -> rsa.RSAPrivateKey:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk["kid"] = "kid-cluster-a"
    client = auth.JWKSClient()
    client._jwks = {"keys": [jwk]}
    client._jwks_uri = "https://idp.example.test/jwks/"
    client._last_fetched = client._clock()
    monkeypatch.setattr(auth, "_jwks_client", client)
    return key


def test_get_current_user_names_the_verified_user(
    signing_key: rsa.RSAPrivateKey, caplog: pytest.LogCaptureFixture
) -> None:
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "user-1",
            "email": "tenant@example.test",
            "groups": ["role_tenant"],
            "exp": int(time.time()) + 300,
        },
        signing_key,
        algorithm="RS256",
        headers={"kid": "kid-cluster-a"},
    )
    entry = _call("/api/v1/me", token, caplog, [])
    assert entry["status_code"] == 200
    assert entry["user"]["id"] == "tenant@example.test"
    assert entry["user"]["groups"] == ["role_tenant"]


def test_a_token_that_fails_verification_names_nobody(
    signing_key: rsa.RSAPrivateKey, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _call("/api/v1/me", FORGED, caplog, [])
    assert entry["status_code"] == 401
    assert entry["user"] is None


def test_no_middleware_decodes_a_token_without_verifying_it() -> None:
    middleware = Path(__file__).resolve().parents[1] / "app" / "middleware"
    offenders = [
        path.name
        for path in middleware.glob("*.py")
        if "verify_signature" in path.read_text() or "import jwt" in path.read_text()
    ]
    assert not offenders, f"middleware decoding JWTs for attribution: {offenders}"
