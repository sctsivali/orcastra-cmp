"""An intentional 502/503/504 is logged as a warning; a genuine 500 is still an error.

A caller polling a cluster that is down would otherwise fill the log with "Server fault"
ERROR lines and bury the faults worth paging on. The answer to the caller is the same
either way.
"""

import logging

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client() -> TestClient:
    from app.main import create_app

    app = create_app()

    @app.get("/api/v1/__probe/{status}")
    async def probe(status: int):
        raise HTTPException(status_code=status, detail=f"cluster c1 recently unreachable ({status})")

    return TestClient(app, raise_server_exceptions=False)


def _main_records(caplog):
    return [r for r in caplog.records if r.name == "app.main" and "/__probe/" in r.getMessage()]


@pytest.mark.parametrize("status", [502, 503, 504])
def test_an_availability_status_is_a_warning(client, caplog, status):
    caplog.set_level(logging.INFO, logger="app.main")
    response = client.get(f"/api/v1/__probe/{status}")
    assert response.status_code == status
    assert "recently unreachable" in response.json()["detail"]
    records = _main_records(caplog)
    assert [r.levelno for r in records] == [logging.WARNING]
    assert records[0].getMessage().startswith("Unavailable ")


def test_a_genuine_fault_is_still_an_error(client, caplog):
    caplog.set_level(logging.INFO, logger="app.main")
    response = client.get("/api/v1/__probe/500")
    assert response.status_code == 500
    assert "recently unreachable" not in response.text
    records = _main_records(caplog)
    assert [r.levelno for r in records] == [logging.ERROR]
    assert records[0].getMessage().startswith("Server fault ")
