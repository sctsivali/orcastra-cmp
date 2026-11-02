"""Access and audit context names a principal only after token verification."""

import ast
import base64
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core import auth
from app.core.config import get_settings
from app.core.logging_config import StructuredJsonFormatter
from app.core.request_context import get_request_context
from app.middleware.access_log import AccessLogMiddleware
from app.middleware.request_id import RequestIdMiddleware

ISSUER = "https://idp.example.test/application/o/orcastra/"
AUDIENCE = "orcastra-client"


def _segment(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


FORGED = (
    f"{_segment({'alg': 'none', 'typ': 'JWT'})}."
    f"{_segment({'email': 'attacker@example.test', 'groups': ['role_admin']})}.unsigned"
)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        auth_enabled=True,
        authentik_issuer=ISSUER,
        authentik_audience=AUDIENCE,
        auth_jwks_cache_ttl=3600,
    )


def _app(snapshots: list[dict]) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[get_settings] = _settings

    def snapshot() -> None:
        ctx = get_request_context()
        record = logging.LogRecord("app.test", logging.INFO, "", 0, "inside route", (), None)
        snapshots.append({
            "actor": ctx.get_actor() if ctx else None,
            "app_log": json.loads(StructuredJsonFormatter().format(record)),
        })

    @app.get("/open")
    async def open_route() -> dict:
        snapshot()
        return {"ok": True}

    @app.get("/refused")
    async def refused_route() -> dict:
        snapshot()
        raise HTTPException(status_code=401, detail="Authentication required")

    @app.get("/me")
    async def me(user: Any = Depends(auth.get_current_user)) -> dict:
        snapshot()
        return {"user": user.user_id}

    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIdMiddleware)
    return TestClient(app, raise_server_exceptions=False)


def _access_entries(caplog: pytest.LogCaptureFixture) -> list[dict]:
    return [record.access_log_data for record in caplog.records if hasattr(record, "access_log_data")]


@pytest.mark.parametrize("path, expected_status", [("/open", 200), ("/refused", 401), ("/me", 401)])
def test_forged_token_never_names_its_claimed_actor(
    path: str, expected_status: int, caplog: pytest.LogCaptureFixture
) -> None:
    snapshots: list[dict] = []
    with caplog.at_level(logging.INFO, logger="orcastra.access"):
        response = _app(snapshots).get(path, headers={"Authorization": f"Bearer {FORGED}"})
    assert response.status_code == expected_status
    (entry,) = _access_entries(caplog)
    assert entry["user"] is None
    if snapshots:
        (inside,) = snapshots
        assert inside["actor"]["user_id"] is None
        assert inside["actor"]["role"] is None
        assert inside["actor"]["groups"] == []
        assert "user_id" not in inside["app_log"]


def test_verified_token_names_its_actor(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk["kid"] = "key-a"
    client = auth.JWKSClient()
    client._jwks = {"keys": [jwk]}
    client._jwks_uri = "https://idp.example.test/jwks/"
    client._last_fetched = time.time()
    # note: no refresh is due, so ensure_keys makes no network call
    client._next_attempt = float("inf")
    monkeypatch.setattr(auth, "_jwks_client", client)
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "user-1",
            "email": "tenant@example.test",
            "groups": ["role_tenant"],
            "exp": int(time.time()) + 300,
        },
        key,
        algorithm="RS256",
        headers={"kid": "key-a"},
    )

    snapshots: list[dict] = []
    with caplog.at_level(logging.INFO, logger="orcastra.access"):
        response = _app(snapshots).get("/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {"user": "tenant@example.test"}
    (entry,) = _access_entries(caplog)
    assert entry["user"]["id"] == "tenant@example.test"
    assert entry["user"]["groups"] == ["role_tenant"]
    (inside,) = snapshots
    assert inside["actor"]["user_id"] == "tenant@example.test"
    assert inside["app_log"]["user_id"] == "tenant@example.test"


APP = Path(__file__).resolve().parents[1] / "app"
AUTH_MODULE = APP / "core" / "auth.py"
_B64_DECODERS = {"b64decode", "urlsafe_b64decode", "standard_b64decode", "decodebytes", "a2b_base64"}
_IMPORTERS = {"import_module", "__import__"}


def _imports_jwt(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", None)) in _IMPORTERS:
            names = [arg.value for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
        else:
            continue
        if any(name == "jwt" or name.startswith("jwt.") for name in names):
            lines.append(node.lineno)
    return lines


def _splits_on_dot(node: ast.AST) -> bool:
    return any(
        isinstance(item, ast.Call)
        and isinstance(item.func, ast.Attribute)
        and item.func.attr in {"split", "rsplit", "partition", "rpartition"}
        and item.args
        and isinstance(item.args[0], ast.Constant)
        and item.args[0].value == "."
        for item in ast.walk(node)
    )


def _decodes_token_segment(scope: ast.AST) -> list[int]:
    """Find base64 decodes of values derived from dot-separated token segments."""
    bindings = []
    for node in ast.walk(scope):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and node.value:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            bindings.append((targets, node.value))
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            bindings.append(([node.target], node.iter))
    tainted: set[str] = set()

    def reads_taint(expr: ast.AST) -> bool:
        return _splits_on_dot(expr) or any(
            isinstance(item, ast.Name) and item.id in tainted for item in ast.walk(expr)
        )

    changed = True
    while changed:
        changed = False
        for targets, value in bindings:
            if not reads_taint(value):
                continue
            for target in targets:
                for name in (item.id for item in ast.walk(target) if isinstance(item, ast.Name)):
                    if name not in tainted:
                        tainted.add(name)
                        changed = True
    return [
        node.lineno
        for node in ast.walk(scope)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", getattr(node.func, "id", None)) in _B64_DECODERS
        and any(reads_taint(arg) for arg in node.args)
    ]


def _token_readers(source: str) -> list[str]:
    tree = ast.parse(source)
    found = [f"imports jwt at {line}" for line in _imports_jwt(tree)]
    definitions = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    top_level = ast.Module(body=[node for node in tree.body if not isinstance(node, definitions)], type_ignores=[])
    scopes = [top_level] + [node for node in ast.walk(tree) if isinstance(node, definitions[:2])]
    lines = sorted({line for scope in scopes for line in _decodes_token_segment(scope)})
    return found + [f"decodes a token segment at {line}" for line in lines]


_KNOWN_UNVERIFIED_READS = {
    "hand-rolled payload decode": (
        "import base64, json\n"
        "def subject(header):\n"
        "    claims = header[7:].split('.')[1]\n"
        "    return json.loads(base64.urlsafe_b64decode(claims + '==')).get('sub')\n"
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
def test_ratchet_detects_known_unverified_read_shapes(shape: str) -> None:
    assert _token_readers(_KNOWN_UNVERIFIED_READS[shape])


def test_ratchet_ignores_an_ordinary_base64_decode() -> None:
    assert _token_readers(
        "import base64\ndef decode(data):\n    return base64.urlsafe_b64decode(data.encode())\n"
    ) == []


# note: the verifier itself, and a token this backend minted and checks by HMAC before it reads it.
# Neither attributes a bearer token to a user.
_NOT_A_BEARER_READ = {
    APP / "core" / "jwks.py": "the JWKS client that verifies bearer tokens for core/auth.py",
    APP / "services" / "instance_files" / "uploads.py": "an upload session token, HMAC-checked before it is decoded",
}


def test_only_the_auth_module_reads_token_claims() -> None:
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        if path == AUTH_MODULE or path in _NOT_A_BEARER_READ:
            continue
        offenders.extend(f"{path.relative_to(APP)} {why}" for why in _token_readers(path.read_text()))
    assert not offenders, (
        "claims read outside core/auth.py could attribute a forged token to a user: "
        + ", ".join(offenders)
    )
