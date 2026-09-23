"""/health and / answer anyone, so they carry the version and health signals and nothing else.

/health published the backend's scratch directory and, when it could not be read, the OSError
text naming it. / published the build commit and date, which tell a caller exactly which fixes a
deployment lacks. The authenticated /system/version keeps the commit.
"""

import logging
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from app.services.lxd import cert_files

SCRATCH = "/var/lib/orcastra-scratch-example"
HEALTH_KEYS = {"status", "service", "temp_storage", "vault"}


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    # note: importing app.main configures logging for the process, so the root logger is put back
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    from app.main import create_app

    try:
        yield TestClient(create_app())
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


def test_health_keeps_its_top_level_keys(client: TestClient) -> None:
    body = client.get("/health").json()
    assert set(body) == HEALTH_KEYS, "integrations fingerprint these keys, so they must not drift"
    assert set(body["temp_storage"]) <= set(cert_files.PUBLIC_USAGE_FIELDS)


def test_health_does_not_name_the_scratch_directory(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scratch = tmp_path / "orcastra-scratch"
    scratch.mkdir()
    monkeypatch.setattr(cert_files, "cert_base", lambda: str(scratch))
    response = client.get("/health")
    body = response.json()
    assert response.status_code == 200
    assert body["temp_storage"]["available"] is True
    assert "path" not in body["temp_storage"]
    assert str(scratch) not in response.text


def test_an_unreadable_scratch_directory_is_reported_without_its_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _refuse(path: Any) -> None:
        raise OSError(2, "No such file or directory", SCRATCH)

    monkeypatch.setattr(cert_files, "cert_base", lambda: SCRATCH)
    monkeypatch.setattr(cert_files.shutil, "disk_usage", _refuse)
    response = client.get("/health")
    body = response.json()
    assert response.status_code == 200
    assert set(body) == HEALTH_KEYS
    assert body["temp_storage"] == {"available": False}
    assert SCRATCH not in response.text and "No such file" not in response.text
    assert any(SCRATCH in record.getMessage() for record in caplog.records), (
        "the operator still needs the path and the error, in the server log"
    )


def test_root_names_the_version_and_no_build(client: TestClient) -> None:
    body = client.get("/").json()
    assert "version" in body
    assert "build_commit" not in body and "build_date" not in body
