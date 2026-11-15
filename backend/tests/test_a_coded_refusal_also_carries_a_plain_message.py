"""A 4xx whose detail is an object also carries its message as a top-level string.

Coded refusals (purchase_ended, shared_cluster_project_policy_violation, key_owns_organizations)
answer {"detail": {"code", "message", ...}} so a client can branch on the code. A client that
reads only a string shows such an answer as a bare 403, which is how OrcaHub reported one. The
detail stays as it is for the clients that read the code, and a top-level `message` repeats the
detail's message for those that look for text.
"""

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.error_sanitizer import client_error_body


def test_an_object_detail_gains_a_top_level_message() -> None:
    body = client_error_body({"code": "purchase_ended", "message": "The purchase ended."})
    assert body == {"detail": {"code": "purchase_ended", "message": "The purchase ended."},
                    "message": "The purchase ended."}


def test_a_string_detail_is_left_as_it_was() -> None:
    assert client_error_body("No permission for cluster c") == {"detail": "No permission for cluster c"}


def test_an_object_without_a_message_is_left_as_it_was() -> None:
    assert client_error_body({"code": "x"}) == {"detail": {"code": "x"}}


def test_the_application_answers_a_coded_refusal_with_both() -> None:
    from app.main import create_app

    app: FastAPI = create_app()

    @app.get("/__qa_coded_refusal")
    async def coded():
        raise HTTPException(status_code=403, detail={"code": "c", "message": "Readable."})

    r = TestClient(app).get("/__qa_coded_refusal")
    assert r.status_code == 403
    assert r.json() == {"detail": {"code": "c", "message": "Readable."}, "message": "Readable."}
