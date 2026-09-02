"""A cluster that is up but slow must not be reported as a broken dashboard.

The member resource endpoint answered 500 for every transport failure, and main.py replaces a
500's body with an opaque reference id, so the operator got no reason at all. Worse, the
obvious fix does not work: `requests.exceptions.ReadTimeout` never reaches this handler.
connection.py mounts `Retry(read=0)`, urllib3 treats that 0 as a counter rather than as False
and raises MaxRetryError, and requests maps that to a plain ConnectionError. The fakes below
raise the real production shapes rather than the ones the type names suggest.

Hermetic in the same way as test_unreachable_cluster_is_503.py: LXDService.__new__ skips
__init__, so there is no Vault, no pylxd and no network.
"""

import uuid

import pytest
import requests.exceptions as requests_exc
import urllib3.exceptions as urllib3_exc
from fastapi import FastAPI
from fastapi.testclient import TestClient
from urllib3.exceptions import MaxRetryError

from app.services.lxd.lxd_service import LXDService


HOST = "central2-qa"


def _pooled_read_timeout() -> Exception:
    """What a read timeout actually looks like after Retry(read=0) and requests' mapping."""
    return requests_exc.ConnectionError(
        MaxRetryError(
            None,
            "https://central2:8443/1.0/resources",
            reason=urllib3_exc.ReadTimeoutError(None, "u", "Read timed out."),
        )
    )


def _connect_refused() -> Exception:
    return requests_exc.ConnectionError(
        MaxRetryError(
            None,
            "https://central2:8443/1.0",
            reason=urllib3_exc.NewConnectionError(None, "Connection refused"),
        )
    )


class _RaisingApiNode:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __getitem__(self, item):
        return self

    def __getattr__(self, item):
        return self

    def get(self, params=None):
        raise self._exc


class _Client:
    def __init__(self, exc: Exception) -> None:
        self.api = _RaisingApiNode(exc)
        self.instances = None


class _ConnectionManager:
    def __init__(self, client) -> None:
        self._client = client

    def get_client(self, host, *args, **kwargs):
        return self._client

    def get_node_names(self):
        return [HOST]


def _service(client):
    def _factory() -> LXDService:
        svc = LXDService.__new__(LXDService)
        svc.connection_manager = _ConnectionManager(client)
        svc.cert_manager = None
        return svc

    return _factory


def _app(client) -> TestClient:
    from app.routers import cluster_members
    from app.services.lxd_service import get_lxd_service
    from app.core.rbac import get_current_user
    from app.core.org_scope import org_scope_dependency

    app = FastAPI()
    app.include_router(cluster_members.router)
    app.dependency_overrides[get_lxd_service] = _service(client)
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def _read(client) -> "object":
    """A uniquely named member, so the 60s response cache cannot answer for a sibling test."""
    member = f"m-{uuid.uuid4().hex[:8]}"
    return _app(client).get(f"/cluster-members/{member}/resources", params={"node": HOST})


def test_a_read_timeout_answers_504_not_500():
    r = _read(_Client(_pooled_read_timeout()))
    assert r.status_code == 504, (
        f"a cluster that answered and then ran long is a gateway timeout, got "
        f"{r.status_code}: {r.text[:300]}"
    )
    detail = str(r.json().get("detail", ""))
    assert HOST in detail, f"the operator must be told which cluster, got {detail!r}"
    assert "Internal server error" not in detail


def test_a_connect_failure_answers_503():
    r = _read(_Client(_connect_refused()))
    assert r.status_code == 503, (
        f"never getting a usable connection is what 503 already means here, got "
        f"{r.status_code}: {r.text[:300]}"
    )


def test_a_bare_read_timeout_is_also_a_504():
    """A caller that builds a session without a Retry raises the type the name suggests."""
    r = _read(_Client(requests_exc.ReadTimeout("Read timed out.")))
    assert r.status_code == 504, r.text[:300]


def test_a_tripped_breaker_still_answers_503():
    """require_client must keep working now that get_client runs off the loop."""
    r = _read(None)
    assert r.status_code == 503, (
        f"a None client is the steady state for a tripped breaker, got {r.status_code}: "
        f"{r.text[:300]}"
    )
    assert HOST in str(r.json().get("detail", ""))


def test_a_404_from_target_is_still_a_404():
    """LXD answers 404 for ?target= on a host that is not clustered. Not a server fault."""
    from pylxd.exceptions import LXDAPIException

    class _Resp:
        status_code = 404

        def json(self):
            return {"error": "not found"}

    r = _read(_Client(LXDAPIException(_Resp())))
    assert r.status_code == 404, r.text[:300]
    assert "is not a cluster member" in str(r.json().get("detail", ""))


@pytest.mark.parametrize(
    "exc,expected",
    [(_pooled_read_timeout(), 504), (_connect_refused(), 503)],
)
def test_no_transport_failure_reaches_the_blanket_handler(exc, expected):
    """The `except Exception` below the mapper must never be what answers a transport error.

    500 is the status this whole file exists to remove, and main.py strips a 500's body, so a
    regression here is silent from the outside.
    """
    r = _read(_Client(exc))
    assert r.status_code == expected
    assert r.status_code != 500
