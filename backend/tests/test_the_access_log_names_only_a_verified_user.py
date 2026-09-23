"""The access log, app log and audit actor name a user only after the token verified.

RequestIdMiddleware runs before routing and seeded the request context from claims decoded without
verifying the signature. A forged token therefore wrote any email, group and role it liked into the
access index, including on requests that were answered 401.
"""

import ast
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


# ---------------------------------------------------------------------------
# Ratchet: nothing outside core/auth.py reads a token's claims
# ---------------------------------------------------------------------------

APP = Path(__file__).resolve().parents[1] / "app"
AUTH_MODULE = APP / "core" / "auth.py"
_B64_DECODERS = {"b64decode", "urlsafe_b64decode", "standard_b64decode", "decodebytes", "a2b_base64"}
_IMPORTERS = {"import_module", "__import__"}


def _imports_jwt(tree: ast.AST) -> List[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", None)) in _IMPORTERS:
            names = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        else:
            continue
        if any(name == "jwt" or name.startswith("jwt.") for name in names):
            lines.append(node.lineno)
    return lines


def _splits_on_a_dot(node: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"split", "rsplit", "partition", "rpartition"}
        and n.args
        and isinstance(n.args[0], ast.Constant)
        and n.args[0].value == "."
        for n in ast.walk(node)
    )


def _decodes_a_token_segment(scope: ast.AST) -> List[int]:
    """Lines where a base64 decoder reads a value that came from splitting a string on '.'.

    A small def-use pass: a name assigned from a split on '.', or from an already tainted name,
    is tainted, and a decoder call over a tainted name or a split is a hand-rolled JWT decode.
    """
    bindings = []
    for node in ast.walk(scope):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and node.value:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            bindings.append((targets, node.value))
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            bindings.append(([node.target], node.iter))
    tainted: set = set()

    def _reads_taint(expr: ast.AST) -> bool:
        return _splits_on_a_dot(expr) or any(
            isinstance(n, ast.Name) and n.id in tainted for n in ast.walk(expr)
        )

    changed = True
    while changed:
        changed = False
        for targets, value in bindings:
            if not _reads_taint(value):
                continue
            for target in targets:
                for name in (n.id for n in ast.walk(target) if isinstance(n, ast.Name)):
                    if name not in tainted:
                        tainted.add(name)
                        changed = True
    return [
        node.lineno
        for node in ast.walk(scope)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", getattr(node.func, "id", None)) in _B64_DECODERS
        and any(_reads_taint(arg) for arg in node.args)
    ]


def _token_readers(source: str) -> List[str]:
    tree = ast.parse(source)
    found = [f"imports jwt at {line}" for line in _imports_jwt(tree)]
    defs = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    top_level = ast.Module(body=[n for n in tree.body if not isinstance(n, defs)], type_ignores=[])
    scopes = [top_level] + [n for n in ast.walk(tree) if isinstance(n, defs[:2])]
    lines = sorted({line for scope in scopes for line in _decodes_a_token_segment(scope)})
    return found + [f"decodes a token segment at {line}" for line in lines]


# note: the shapes this ratchet exists for, each one a way a request's claims were read without
# a signature check before. The scanner must see every one, or a green run proves nothing.
_KNOWN_UNVERIFIED_READS = {
    "hand-rolled payload decode": (
        "import base64, json\n"
        "def subject(header):\n"
        "    claims = header[7:].split('.')[1]\n"
        "    padded = claims + '=' * (-len(claims) % 4)\n"
        "    return json.loads(base64.urlsafe_b64decode(padded)).get('sub')\n"
    ),
    "inline segment decode": (
        "from base64 import urlsafe_b64decode\n"
        "def subject(token):\n"
        "    return urlsafe_b64decode(token.split('.')[1] + '==')\n"
    ),
    "function-local jwt import": (
        "def peek(token):\n"
        "    import jwt as pyjwt\n"
        "    return pyjwt.decode(token, options={'verify_signature': False})\n"
    ),
    "from-import": "from jwt import decode\n",
    "dynamic import": "import importlib\nlib = importlib.import_module('jwt')\n",
}


@pytest.mark.parametrize("shape", sorted(_KNOWN_UNVERIFIED_READS))
def test_the_scanner_sees_every_known_unverified_read(shape: str) -> None:
    assert _token_readers(_KNOWN_UNVERIFIED_READS[shape]), f"the scanner misses a {shape}"


def test_an_ordinary_base64_decode_is_not_a_token_read() -> None:
    source = "import base64\ndef f(data):\n    return base64.urlsafe_b64decode(data.encode())\n"
    assert _token_readers(source) == []


def test_only_the_auth_module_reads_a_token() -> None:
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        if path == AUTH_MODULE:
            continue
        offenders += [f"{path.relative_to(APP)} {why}" for why in _token_readers(path.read_text())]
    assert not offenders, (
        "claims read outside core/auth.py are unverified, and a forged token then names any user "
        "it likes in the access log and the audit trail: " + ", ".join(offenders)
    )
