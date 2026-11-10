"""A structured 5xx detail reaches the client as written, never as a Python dict repr.

The handler in main.py passes every 5xx detail through public_server_detail. A detail keyed on
`code` must keep its shape: `isProjectDeleteUnknown` in the frontend reads `detail.code`, and
`str(detail)` would put a dict repr in the toast. Router tests do not install that handler, so
these go through it.
"""

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exception_handlers import http_exception_handler
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.error_sanitizer import _STRUCTURED_5XX, public_server_detail, reference_for

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"


def _through_the_handler(status: int, detail: object) -> dict:
    app = FastAPI()

    @app.exception_handler(StarletteHTTPException)
    async def handler(request, exc):
        if exc.status_code < 500:
            return await http_exception_handler(request, exc)
        from fastapi.responses import JSONResponse

        reference, _ = reference_for(exc)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": public_server_detail(exc.status_code, exc.detail, reference)},
        )

    @app.get("/x")
    async def route():
        raise HTTPException(status_code=status, detail=detail)

    return TestClient(app).get("/x").json()


@pytest.mark.parametrize(
    "code",
    [
        "project_write_unknown",
        "CREATE_PREFLIGHT_UNAVAILABLE",
        "LXD_UNAVAILABLE",
        "operation_cancel_temporarily_unavailable",
        "operation_cancel_outcome_unknown",
    ],
)
def test_a_coded_answer_keeps_the_code_the_client_branches_on(code):
    body = _through_the_handler(503, {"code": code, "message": "Refresh before retrying."})
    assert body["detail"] == {"code": code, "message": "Refresh before retrying."}


def test_an_extra_field_does_not_ride_along_on_a_coded_answer():
    shown = public_server_detail(503, {"code": "LXD_UNAVAILABLE", "message": "m", "internal": "x"}, "r")
    assert set(shown) == {"code", "message"}


def test_a_coded_message_is_still_sanitized():
    shown = public_server_detail(
        503, {"code": "project_write_unknown", "message": "see https://10.0.0.1:8443/1.0 now"}, "r"
    )
    assert "10.0.0.1" not in shown["message"]


def test_a_dict_with_no_allowed_code_shows_its_sentence_and_never_a_repr():
    shown = public_server_detail(503, {"message": "Cache clearing was incomplete.", "namespaces": ["a"]}, "r")
    assert shown == "Cache clearing was incomplete."


def test_a_dict_with_no_sentence_gets_the_reference():
    shown = public_server_detail(503, {"thing": 1}, "abc123")
    assert "abc123" in shown and "{" not in shown


def test_an_unhashable_code_cannot_turn_the_answer_into_a_500():
    assert public_server_detail(503, {"code": ["x"], "message": "fine"}, "r") == "fine"


def _literal_5xx_codes():
    """Every `HTTPException(status_code=50x, detail={"code"|"error": "..."})` written in a router."""
    found = []
    for path in sorted(ROUTERS.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "HTTPException"):
                continue
            kwargs = {k.arg: k.value for k in node.keywords}
            status, detail = kwargs.get("status_code"), kwargs.get("detail")
            if not (isinstance(status, ast.Constant) and isinstance(detail, ast.Dict)):
                continue
            for key, value in zip(detail.keys, detail.values):
                if isinstance(key, ast.Constant) and key.value in ("code", "error") and isinstance(value, ast.Constant):
                    found.append((path.name, status.value, value.value))
    return found


def test_every_code_a_router_sends_is_allowed_through():
    found = [row for row in _literal_5xx_codes() if row[1] in (502, 503, 504, 507)]
    assert len(found) >= 12, found
    missing = [row for row in found if row[2] not in _STRUCTURED_5XX.get(row[1], {})]
    assert missing == []
