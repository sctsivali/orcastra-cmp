"""
The proxy envelopes answer HTTP 200, so the application's 5xx sanitiser never sees them, and their
`error` carried the raw exception text: the cluster's address, port, URL and TLS detail. `error`
now goes through sanitize_error. `data` is LXD's own answer and must stay byte-identical, because
the integrator reads a refusal (a secureboot one included) from it.
"""

import json
from typing import Any, Iterator, List

import pytest
import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError

from app.routers.integrations import (
    ProxyRequest,
    RawProxyRequest,
    proxy_operation,
    raw_proxy,
)
from app.services.lxd import health

CLUSTER_ID = "cluster-a"
HOST = "10.20.30.40"
ENDPOINT = f"https://{HOST}:8443/1.0"
API_KEY = {
    "app_name": "qa-envelope",
    "cluster_permissions": [
        {"cluster_id": CLUSTER_ID, "permissions": ["read", "write", "admin"], "projects": ["*"]}
    ],
}


def _connection_error() -> requests.exceptions.ConnectionError:
    reason = NewConnectionError(
        None, f"<urllib3.connection.HTTPSConnection object>: Failed to establish a new connection to {HOST}:8443"
    )
    return requests.exceptions.ConnectionError(
        MaxRetryError(None, f"{ENDPOINT}/instances", reason=reason),
    )


def _assert_clean(text: str) -> None:
    assert text, "the envelope must still say something"
    assert HOST not in text
    assert "https://" not in text
    assert "/1.0/instances" not in text


class RaisingSession:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def _raise(self, url: str, **kwargs: Any) -> requests.Response:
        raise self.error

    get = post = put = patch = delete = _raise


class AnsweringSession:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body

    def _answer(self, url: str, **kwargs: Any) -> requests.Response:
        response = requests.Response()
        response.status_code = self.status
        response._content = self.body
        return response

    get = post = put = patch = delete = _answer


class _Api:
    def __init__(self, session: Any) -> None:
        self._api_endpoint = ENDPOINT
        self._timeout = (3, 8)
        self.session = session


class _BrokenApi:
    """An endpoint lookup that fails outside the inner try, to reach the outer handler."""

    session = None
    _timeout = (3, 8)

    @property
    def _api_endpoint(self) -> str:
        raise RuntimeError(f"cannot resolve endpoint {ENDPOINT} for {HOST}")


class _Client:
    def __init__(self, api: Any) -> None:
        self.api = api


class _Manager:
    def __init__(self, client: Any = None, error: Exception = None) -> None:
        self._client = client
        self._error = error

    def get_client(self, cluster_id: str) -> Any:
        if self._error:
            raise self._error
        return self._client


class FakeService:
    def __init__(self, client: Any = None, error: Exception = None) -> None:
        self.connection_manager = _Manager(client, error)


@pytest.fixture(autouse=True)
def _reset_breaker() -> Iterator[None]:
    health.clear(CLUSTER_ID)
    yield
    health.clear(CLUSTER_ID)


async def _raw(service: FakeService, path: str = "/1.0/instances", method: str = "GET") -> Any:
    return await raw_proxy(
        request=RawProxyRequest(cluster_id=CLUSTER_ID, method=method, path=path),
        api_key=API_KEY,
        lxd_service=service,
        _=None,
    )


async def test_a_transport_error_envelope_names_no_host() -> None:
    response = await _raw(FakeService(_Client(_Api(RaisingSession(_connection_error())))))
    assert response.status_code == 500
    assert response.data is None
    _assert_clean(response.error)


async def test_the_outer_handler_names_no_host() -> None:
    response = await _raw(FakeService(_Client(_BrokenApi())))
    assert response.status_code == 500
    _assert_clean(response.error)


async def test_the_lxd_body_is_passed_through_untouched() -> None:
    lxd_body = {
        "type": "error",
        "error": f"Failed creating instance record: secureboot is not supported on {HOST} ({ENDPOINT})",
        "error_code": 400,
        "metadata": None,
    }
    raw = json.dumps(lxd_body).encode()
    response = await _raw(FakeService(_Client(_Api(AnsweringSession(400, raw)))), method="POST")
    assert response.status_code == 400
    assert response.error is None
    assert response.data == lxd_body
    assert json.dumps(response.data).encode() == raw


async def test_the_proxy_envelope_names_no_host() -> None:
    response = await proxy_operation(
        request=ProxyRequest(cluster_id=CLUSTER_ID, operation="list_instances"),
        api_key=API_KEY,
        lxd_service=FakeService(error=_connection_error()),
        _=None,
    )
    assert response.success is False
    _assert_clean(response.error)


@pytest.mark.parametrize("secret", ["password=hunter2", "token=abc.def.ghi"])
async def test_a_secret_in_the_exception_does_not_reach_the_envelope(secret: str) -> None:
    error = RuntimeError(f"LXD refused: {secret}")
    response = await _raw(FakeService(_Client(_Api(RaisingSession(error)))))
    assert secret.split("=", 1)[1] not in (response.error or "")


def test_the_message_limit_is_generous() -> None:
    from app.routers.integrations import _envelope_error

    long_text = "x " * 900
    assert len(_envelope_error(RuntimeError(long_text))) > 300

    messages: List[str] = [_envelope_error(RuntimeError("Instance not found"))]
    assert messages == ["Instance not found"]
