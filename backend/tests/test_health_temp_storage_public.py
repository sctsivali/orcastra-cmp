"""Unauthenticated health reports certificate storage condition without filesystem details."""

import logging
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.services.lxd import cert_files

SCRATCH = "/var/lib/example-private-scratch"
PUBLIC_FIELDS = {"available", "used_mib", "total_mib", "percent_used", "degraded"}


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    from app.main import create_app

    try:
        yield TestClient(create_app())
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


def test_health_publishes_only_allowlisted_storage_fields(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cert_files,
        "describe_usage",
        lambda: {
            "path": SCRATCH,
            "available": True,
            "used_mib": 12.5,
            "total_mib": 256.0,
            "percent_used": 4.9,
            "degraded": False,
            "future_internal_detail": "private",
        },
    )
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"status", "service", "version", "temp_storage", "vault", "identity_provider"}
    assert set(body["temp_storage"]) == PUBLIC_FIELDS
    assert SCRATCH not in response.text and "private" not in response.text


def test_unreadable_storage_keeps_error_only_in_server_log(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        cert_files,
        "describe_usage",
        lambda: {"path": SCRATCH, "available": False, "error": f"cannot stat {SCRATCH}"},
    )
    with caplog.at_level(logging.WARNING):
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["temp_storage"] == {"available": False}
    assert SCRATCH not in response.text and "cannot stat" not in response.text
    assert any(SCRATCH in record.getMessage() for record in caplog.records)
