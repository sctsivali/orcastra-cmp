"""Uvicorn's HTTP and WebSocket request lines must not log credential query values."""

import logging

import pytest

from app.core.error_sanitizer import is_sensitive_param, redact_query_secrets
from app.core.logging_config import QuerySecretFilter, setup_logging

SECRET = "jwt-header.jwt-body.jwt-signature"


@pytest.mark.parametrize(
    "name",
    ["token", "access_token", "client_secret", "X-Api-Key", "api_key_id", "t%6fken"],
)
def test_sensitive_query_names_include_encoded_spelling(name: str) -> None:
    assert is_sensitive_param(name)


def test_query_redaction_keeps_public_parameters_and_separator_shape() -> None:
    line = f'/ws?host=cluster-a&token={SECRET};project=p1&client_secret={SECRET}'
    redacted = redact_query_secrets(line)
    assert SECRET not in redacted
    assert redacted == "/ws?host=cluster-a&token=[REDACTED];project=p1&client_secret=[REDACTED]"


def test_filter_redacts_websocket_tuple_and_http_mapping_arguments() -> None:
    secret_filter = QuerySecretFilter()
    ws_record = logging.LogRecord(
        "uvicorn.error", logging.INFO, "", 0,
        '%s - "WebSocket %s" [accepted]',
        ("1.2.3.4:5", f"/api/v1/terminal/ws?host=a&token={SECRET}"), None,
    )
    assert secret_filter.filter(ws_record)
    assert SECRET not in ws_record.getMessage()
    assert "host=a&token=[REDACTED]" in ws_record.getMessage()

    http_record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0,
        '%(method)s %(path)s %(status)d',
        {"method": "GET", "path": f"/nodes?access_token={SECRET}&project=p1", "status": 200},
        None,
    )
    assert secret_filter.filter(http_record)
    assert http_record.getMessage() == "GET /nodes?access_token=[REDACTED]&project=p1 200"
    assert isinstance(http_record.args, dict)
    assert http_record.args["status"] == 200


def test_filter_redacts_a_message_that_contains_a_literal_query() -> None:
    record = logging.LogRecord(
        "uvicorn.error", logging.INFO, "", 0,
        f"WebSocket /ws?token={SECRET}&host=a accepted", (), None,
    )
    assert QuerySecretFilter().filter(record)
    assert record.getMessage() == "WebSocket /ws?token=[REDACTED]&host=a accepted"


def test_installation_is_idempotent_on_both_uvicorn_loggers() -> None:
    names = ("uvicorn.error", "uvicorn.access")
    loggers = [logging.getLogger(name) for name in names]
    originals = [logger.filters[:] for logger in loggers]
    levels = [logger.level for logger in loggers]
    root = logging.getLogger()
    root_handlers, root_level = root.handlers[:], root.level
    try:
        setup_logging(json_logs=False)
        setup_logging(json_logs=False)
        for logger in loggers:
            assert sum(isinstance(item, QuerySecretFilter) for item in logger.filters) == 1
    finally:
        root.handlers[:] = root_handlers
        root.setLevel(root_level)
        for logger, filters, level in zip(loggers, originals, levels):
            logger.filters[:] = filters
            logger.setLevel(level)
