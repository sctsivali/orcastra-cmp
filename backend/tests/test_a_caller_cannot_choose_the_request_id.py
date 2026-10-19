"""The request ID is minted by the server; a caller's X-Request-ID is only kept for correlation.

The request ID is the access log's OpenSearch `_id`, written as an upsert. When the caller's
header was used as the ID, sending a request with the ID of an earlier one overwrote that
earlier access record, so a client could erase its own trail, and quotes or long values went
straight into the bulk metadata line.
"""

import logging
import uuid

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.middleware import AccessLogMiddleware, RequestIdMiddleware
from app.middleware.request_id import client_request_id


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/probe")
    async def probe(request: Request):
        return {"request_id": request.state.request_id,
                "client_request_id": request.state.client_request_id}

    app.add_middleware(AccessLogMiddleware, service_name="t", version="0")
    app.add_middleware(RequestIdMiddleware)
    return app


def _is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def test_every_request_gets_a_fresh_server_id_even_when_the_caller_sends_one():
    client = TestClient(_app())
    first = client.get("/probe", headers={"X-Request-ID": "trace-123"})
    second = client.get("/probe", headers={"X-Request-ID": "trace-123"})
    ids = [r.headers["X-Request-ID"] for r in (first, second)]
    assert all(_is_uuid(i) for i in ids) and ids[0] != ids[1]
    assert first.json() == {"request_id": ids[0], "client_request_id": "trace-123"}


@pytest.mark.parametrize("value", ['x","_index":"evil', "a" * 129, "naïve-id", "two words", ""])
def test_a_header_that_is_not_a_plain_token_is_not_kept(value):
    assert client_request_id(value) is None
    response = TestClient(_app()).get("/probe", headers={"X-Request-ID": value.encode()} if value else {})
    assert response.json()["client_request_id"] is None
    assert _is_uuid(response.headers["X-Request-ID"])


@pytest.mark.parametrize("value", ["trace-123", "a" * 128, "0f8fad5b-d9cb-469f-a165-70867728950e", "svc.req_1"])
def test_a_plain_token_is_kept_for_correlation(value):
    assert client_request_id(value) == value


def test_the_access_record_carries_both_ids(caplog):
    caplog.set_level(logging.INFO)
    response = TestClient(_app()).get("/probe", headers={"X-Request-ID": "trace-123"})
    records = [getattr(r, "access_log_data", None) for r in caplog.records]
    records = [r for r in records if r and r.get("path") == "/probe"]
    assert records, "no access record captured"
    assert records[-1]["request_id"] == response.headers["X-Request-ID"]
    assert records[-1]["client_request_id"] == "trace-123"
