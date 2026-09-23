"""
A server fault answers with a reference, not with the exception text.

The text of a 5xx is written by pylxd, requests, asyncpg and hvac and carries control-plane URLs,
addresses, backend paths and Vault paths, and most routers are reachable by a tenant. A 4xx is the
caller's own request described back to them and must stay byte-identical, dict details included.
"""

import ast
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.error_handlers import register_error_handlers
from app.core.error_sanitizer import (
    client_detail,
    is_sensitive_param,
    redact_query_secrets,
    sanitize_error,
)
from app.services.lxd.exceptions import LXDConnectionError, LXDLocalStorageError

APP_DIR = Path(__file__).resolve().parents[1] / "app"


def _build(with_handlers: bool) -> FastAPI:
    app = FastAPI()
    if with_handlers:
        register_error_handlers(app)

    @app.get("/vault")
    async def vault() -> None:
        raise HTTPException(
            status_code=500,
            detail="Failed to read http://10.0.1.4:8200/v1/secret/data/clusters/cluster-a/certs",
        )

    @app.get("/unreachable")
    async def unreachable() -> None:
        raise HTTPException(
            status_code=503,
            detail="Cluster 'cluster-a' recently unreachable: HTTPSConnectionPool(host='10.20.30.40', "
            "port=8443): Read timed out.",
            headers={"Retry-After": "30"},
        )

    @app.get("/bad")
    async def bad() -> None:
        raise HTTPException(status_code=400, detail="Name must be lowercase: /tmp/x/y is not allowed")

    @app.get("/limit")
    async def limit() -> None:
        raise HTTPException(status_code=400, detail={"code": "PROJECT_LIMIT_REACHED", "limit": "instances"})

    @app.get("/missing")
    async def missing() -> None:
        raise HTTPException(status_code=404, detail="Instance web-1 not found on cluster-a")

    @app.get("/slow")
    async def slow() -> None:
        raise HTTPException(status_code=429, detail="Too many requests", headers={"Retry-After": "7"})

    @app.get("/lxd-down")
    async def lxd_down() -> None:
        raise LXDConnectionError("Could not connect to https://192.0.2.10:8443 (cert /tmp/orcastra/a.crt)")

    @app.get("/disk-full")
    async def disk_full() -> None:
        raise LXDLocalStorageError("No space left writing /tmp/orcastra-certs/cluster-a.key")

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_build(True))


def test_an_unexpected_fault_answers_with_a_reference(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger="app.core.error_handlers"):
        response = client.get("/vault")
    body = response.json()
    assert response.status_code == 500
    assert body["detail"] == client_detail(body["reference"])
    assert "10.0.1.4" not in response.text and "secret/data" not in response.text
    assert any(body["reference"] in r.getMessage() and "10.0.1.4" in r.getMessage() for r in caplog.records)


def test_an_availability_answer_is_logged_as_a_warning_with_its_reference(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.core.error_handlers"):
        response = client.get("/unreachable")
    reference = response.json()["reference"]
    records = [r for r in caplog.records if reference in r.getMessage()]
    assert [r.levelno for r in records] == [logging.WARNING]


def test_an_unexpected_fault_is_logged_as_an_error(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.core.error_handlers"):
        response = client.get("/vault")
    reference = response.json()["reference"]
    records = [r for r in caplog.records if reference in r.getMessage()]
    assert [r.levelno for r in records] == [logging.ERROR]


def test_an_availability_answer_keeps_its_sentence_without_the_address(client: TestClient) -> None:
    response = client.get("/unreachable")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    detail = response.json()["detail"]
    assert "recently unreachable" in detail and "cluster-a" in detail
    assert "10.20.30.40" not in detail


@pytest.mark.parametrize("path", ["/bad", "/limit", "/missing", "/slow"])
def test_a_client_error_is_byte_identical(client: TestClient, path: str) -> None:
    plain = TestClient(_build(False)).get(path)
    handled = client.get(path)
    assert handled.status_code == plain.status_code
    assert handled.content == plain.content
    assert handled.headers.get("retry-after") == plain.headers.get("retry-after")


def test_the_lxd_handlers_keep_their_shape_and_drop_the_details(client: TestClient) -> None:
    down = client.get("/lxd-down")
    assert down.status_code == 503
    assert down.json()["code"] == "LXD_CONNECTION_ERROR"
    assert "192.0.2.10" not in down.text and "/tmp/orcastra" not in down.text
    full = client.get("/disk-full")
    assert full.status_code == 503
    assert full.json()["code"] == "BACKEND_TEMP_STORAGE_FULL"
    assert "/tmp/orcastra-certs" not in full.text


def test_main_registers_the_handlers() -> None:
    tree = ast.parse((APP_DIR / "main.py").read_text())
    create_app = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "create_app"
    )
    calls = [
        n for n in ast.walk(create_app)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "register_error_handlers"
    ]
    assert calls, "create_app must call register_error_handlers(app)"


def test_no_router_raises_a_server_fault_with_a_structured_detail() -> None:
    """A dict detail on a 5xx would be flattened into text by the sanitiser."""
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "HTTPException"):
                continue
            kwargs = {k.arg: k.value for k in node.keywords}
            status = kwargs.get("status_code")
            code = status.value if isinstance(status, ast.Constant) else getattr(status, "attr", "")
            is_5xx = (isinstance(code, int) and code >= 500) or (isinstance(code, str) and "_5" in code)
            if is_5xx and isinstance(kwargs.get("detail"), ast.Dict):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


@pytest.mark.parametrize(
    "raw,gone,kept",
    [
        ("certificate not trusted by the cluster", [], ["certificate not trusted"]),
        ("password=hunter2 rejected", ["hunter2"], ["password="]),
        ("key oas_abcdefghijklmnop leaked", ["oas_abcdefghijklmnop"], ["leaked"]),
        ("dial [fd7a:115c::1]:8443 failed", ["fd7a:115c::1"], ["failed"]),
        ("GET /1.0/instances/web answered 500", ["/1.0/instances/web"], ["answered 500"]),
    ],
)
def test_sanitize_error_removes_only_what_it_must(raw: str, gone: list, kept: list) -> None:
    out = sanitize_error(raw)
    for fragment in gone:
        assert fragment not in out
    for fragment in kept:
        assert fragment in out


def test_query_secrets_are_redacted_by_stem() -> None:
    for name in ("token", "access_token", "client_secret", "X-Api-Key", "signature", "code"):
        assert is_sensitive_param(name)
    for name in ("project", "host", "name"):
        assert not is_sensitive_param(name)
    line = '1.2.3.4 - "WebSocket /api/v1/terminal/ws/i?host=h&token=eyJabc.def.ghi" [accepted]'
    out = redact_query_secrets(line)
    assert "eyJabc" not in out and "host=h" in out and "token=[REDACTED]" in out
