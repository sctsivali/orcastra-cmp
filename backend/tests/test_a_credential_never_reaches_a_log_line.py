"""Credentials in a query string never reach the access log, the handshake log or OpenSearch.

The access log masked five exact names, so `access_token`, `client_secret` and `X-Api-Key` went
into the index in clear. uvicorn logs every WebSocket handshake with its query string, and the
browser authenticates a socket with `?token=<JWT>`, so live bearer tokens sat in the container log.
"""

import logging
from typing import Iterator, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import logging_config
from app.core.error_sanitizer import is_sensitive_param
from app.middleware.access_log import AccessLogMiddleware

JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ0ZW5hbnRAZXhhbXBsZS50ZXN0In0.c2lnbmF0dXJlLXZhbHVlLXgxMjM"


@pytest.mark.parametrize(
    "name",
    [
        "token", "access_token", "id_token", "refresh_token", "password", "client_secret",
        "secret", "api_key", "apikey", "X-Api-Key", "authorization", "auth", "credential",
        "signature", "code", "key", "api_key_id",
    ],
)
def test_a_credential_name_is_redacted(name: str) -> None:
    assert is_sensitive_param(name)


@pytest.mark.parametrize(
    "name", ["project", "host", "fresh", "instance", "name", "limit", "offset", "status", "type"]
)
def test_an_ordinary_name_is_kept(name: str) -> None:
    assert not is_sensitive_param(name)


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/v1/nodes")
    async def _nodes() -> dict:
        return {"ok": True}

    @app.get("/api/v1/boom")
    async def _boom() -> dict:
        raise RuntimeError("could not open /var/lib/orcastra/certs/cluster-a/client.key")

    app.add_middleware(AccessLogMiddleware)
    return app


def _access_entries(caplog: pytest.LogCaptureFixture) -> List[dict]:
    return [r.access_log_data for r in caplog.records if hasattr(r, "access_log_data")]


def test_the_access_log_redacts_by_stem(caplog: pytest.LogCaptureFixture) -> None:
    client = TestClient(_app())
    with caplog.at_level(logging.INFO, logger="orcastra.access"):
        client.get("/api/v1/nodes?access_token=a-value&client_secret=b-value&project=p1")
    (entry,) = _access_entries(caplog)
    assert entry["query_params"] == {
        "access_token": "[REDACTED]",
        "client_secret": "[REDACTED]",
        "project": "p1",
    }


def test_the_access_log_error_carries_no_path(caplog: pytest.LogCaptureFixture) -> None:
    client = TestClient(_app(), raise_server_exceptions=False)
    with caplog.at_level(logging.INFO, logger="orcastra.access"):
        client.get("/api/v1/boom")
    (entry,) = _access_entries(caplog)
    assert entry["status_code"] == 500
    assert "/var/lib" not in entry["error"] and "client.key" not in entry["error"]
    assert entry["error"].startswith("could not open")


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@pytest.fixture()
def clean_logging() -> Iterator[None]:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    watched = [logging.getLogger(name) for name in logging_config.QUERY_SECRET_LOGGERS]
    saved_filters = {lg.name: lg.filters[:] for lg in watched}
    for lg in watched:
        for existing in list(lg.filters):
            if isinstance(existing, logging_config.QuerySecretFilter):
                lg.removeFilter(existing)
    try:
        yield
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)
        for lg in watched:
            lg.filters[:] = saved_filters[lg.name]


def test_the_websocket_handshake_line_loses_its_token(clean_logging: None) -> None:
    logging_config.setup_logging(json_logs=False)
    logging_config.setup_logging(json_logs=False)
    handshake = logging.getLogger("uvicorn.error")
    assert sum(isinstance(f, logging_config.QuerySecretFilter) for f in handshake.filters) == 1

    capture = _ListHandler()
    handshake.addHandler(capture)
    try:
        handshake.info(
            '%s - "WebSocket %s" [accepted]',
            "1.2.3.4:5",
            f"/api/v1/terminal/ws/i?host=cluster-a&token={JWT}",
        )
    finally:
        handshake.removeHandler(capture)
    (line,) = capture.lines
    assert JWT not in line
    assert "token=[REDACTED]" in line
    assert "host=cluster-a" in line


def test_the_http_access_line_keeps_its_shape(clean_logging: None) -> None:
    logging_config.setup_logging(json_logs=False)
    access = logging.getLogger("uvicorn.access")
    configured = access.level
    access.setLevel(logging.INFO)
    capture = _ListHandler()
    access.addHandler(capture)
    try:
        access.info(
            '%s - "%s %s HTTP/%s" %d',
            "1.2.3.4:5",
            "GET",
            f"/api/v1/nodes?access_token={JWT}",
            "1.1",
            200,
        )
    finally:
        access.removeHandler(capture)
        access.setLevel(configured)
    (line,) = capture.lines
    assert JWT not in line
    assert line.endswith('HTTP/1.1" 200')
