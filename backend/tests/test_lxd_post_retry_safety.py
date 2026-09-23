"""Mutation submission must not be replayed or redirected by the LXD HTTP pool."""

import requests
import pytest
from requests import Response
from requests.adapters import BaseAdapter

from app.services.lxd.connection import FingerprintHTTPAdapter, LXDConnectionManager
from app.services.lxd.exceptions import LXDConnectionError


class _API:
    def __init__(self) -> None:
        self.session = requests.Session()


class _Client:
    def __init__(self) -> None:
        self.api = _API()


def _assert_mutations_are_not_retryable(session: requests.Session) -> None:
    for prefix in ("http://", "https://"):
        adapter = session.get_adapter(prefix + "cluster.invalid")
        allowed = {method.upper() for method in (adapter.max_retries.allowed_methods or set())}
        assert not {"POST", "PUT", "PATCH", "DELETE"}.intersection(allowed)
        assert "GET" in allowed


def test_pooled_pylxd_clients_do_not_retry_post() -> None:
    manager = object.__new__(LXDConnectionManager)
    client = _Client()

    manager._configure_client_session(client)

    _assert_mutations_are_not_retryable(client.api.session)


def test_direct_connection_sessions_do_not_retry_post() -> None:
    manager = object.__new__(LXDConnectionManager)

    _assert_mutations_are_not_retryable(manager._create_http_session())


def test_registered_fingerprint_is_enforced_by_the_real_connection_pool() -> None:
    fingerprint = "ab" * 32
    adapter = FingerprintHTTPAdapter(server_fingerprint=fingerprint)

    assert adapter.poolmanager.connection_pool_kw["assert_fingerprint"] == fingerprint


class _RedirectAdapter(BaseAdapter):
    def __init__(self) -> None:
        self.urls: list[str] = []

    def send(self, request, **kwargs):
        del kwargs
        self.urls.append(request.url)
        response = Response()
        response.status_code = 307
        response.url = request.url
        response.request = request
        response.headers["Location"] = "https://attacker.invalid/collect"
        response._content = b""
        response._content_consumed = True
        return response

    def close(self) -> None:
        return None


def test_redirect_is_rejected_before_a_mutation_body_can_reach_a_second_origin() -> None:
    manager = object.__new__(LXDConnectionManager)
    session = manager._create_http_session("cd" * 32)
    adapter = _RedirectAdapter()
    session.mount("https://", adapter)

    with pytest.raises(LXDConnectionError, match="redirect"):
        session.post(
            "https://cluster.invalid/1.0/instances",
            json={"source": {"secrets": {"control": "must-not-leak"}}},
        )

    assert adapter.urls == ["https://cluster.invalid/1.0/instances"]
