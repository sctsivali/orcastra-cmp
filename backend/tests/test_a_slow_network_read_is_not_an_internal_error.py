"""A network state read against a slow cluster is a 504, not a broken dashboard.

Found live: `GET /networks/lxdbr0/state?host=nuc2` answered 500 while nuc2 was merely slow, and
`main.py` replaces a 500's body with an opaque reference, so the operator got an id and no
reason. The same taxonomy the member resource read already uses applies here.

The fakes raise the shape production actually produces. `connection.py` mounts `Retry(read=0)`,
urllib3 treats that 0 as a counter rather than as False, and requests maps the result to a
plain ConnectionError, so `isinstance(exc, ReadTimeout)` never fires on the pooled path.
"""

import pytest
import requests.exceptions as requests_exc
import urllib3.exceptions as urllib3_exc
from fastapi import FastAPI
from fastapi.testclient import TestClient
from urllib3.exceptions import MaxRetryError

from app.services.lxd.lxd_service import LXDService


HOST = "nuc2-qa"


def _pooled_read_timeout() -> Exception:
    return requests_exc.ConnectionError(
        MaxRetryError(
            None,
            f"https://{HOST}:8443/1.0/networks/lxdbr0",
            reason=urllib3_exc.ReadTimeoutError(None, "u", "Read timed out."),
        )
    )


def _connect_refused() -> Exception:
    return requests_exc.ConnectionError(
        MaxRetryError(
            None,
            f"https://{HOST}:8443/1.0",
            reason=urllib3_exc.NewConnectionError(None, "Connection refused"),
        )
    )


class _Networks:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def get(self, name):
        raise self._exc


class _Client:
    def __init__(self, exc: Exception) -> None:
        self.networks = _Networks(exc)


def _service(client):
    def _factory() -> LXDService:
        svc = LXDService.__new__(LXDService)
        svc.connection_manager = None
        svc.cert_manager = None
        svc.get_client_with_reconnect = lambda host: client
        return svc

    return _factory


def _read(exc: Exception):
    from app.routers import networks
    from app.services.lxd_service import get_lxd_service
    from app.core.rbac import get_current_user
    from app.core.org_scope import org_scope_dependency

    app = FastAPI()
    app.include_router(networks.router)
    app.dependency_overrides[get_lxd_service] = _service(_Client(exc))
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    client = TestClient(app, raise_server_exceptions=False)
    return client.get("/networks/lxdbr0/state", params={"host": HOST})


def test_a_slow_cluster_is_a_504_not_a_500():
    r = _read(_pooled_read_timeout())
    assert r.status_code == 504, r.text
    assert HOST in r.json()["detail"], "the operator has to be told which host ran out of time"


def test_a_refused_connection_is_a_503():
    r = _read(_connect_refused())
    assert r.status_code == 503, r.text


def test_a_bare_read_timeout_is_also_a_504():
    """A caller that builds its own session without a Retry produces this shape instead."""
    r = _read(requests_exc.ReadTimeout("Read timed out."))
    assert r.status_code == 504, r.text


@pytest.mark.parametrize(
    "exc",
    [_pooled_read_timeout(), _connect_refused(), requests_exc.ReadTimeout("t")],
    ids=["pooled-read-timeout", "connect-refused", "bare-read-timeout"],
)
def test_no_transport_failure_reaches_the_blanket_handler(exc):
    """500 is the one answer this endpoint must never give for a peer that is merely slow:
    main.py replaces its body, so the reason never reaches the interface."""
    assert _read(exc).status_code != 500
