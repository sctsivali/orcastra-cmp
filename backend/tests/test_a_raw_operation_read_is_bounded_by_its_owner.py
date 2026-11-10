"""The raw proxy reaches one LXD operation only for a key that holds the operation's project.

raw_proxy bounds a request by forcing `?project=`, but LXD ignores `?project=` on
`/1.0/operations/{id}`. Without an ownership read, a key granted only `tenant-a` that held another
project's operation id could read it, wait on it and, holding `admin`, cancel it. REST, the proxy
get_operation and both MCP poll tools apply the same visibility rule.
"""

import asyncio
from typing import Any, Dict, List, Tuple

import pytest
import requests

from app.routers import integrations
from app.services.lxd import raw_proxy_operations
from app.services.lxd.operation_ownership import visible_to
from tests._raw_proxy_fakes import NARROW, WILDCARD, call_raw_proxy, fence_calls  # noqa: F401

LXD_MISSING = {"type": "error", "status": "", "status_code": 0, "operation": "", "error_code": 404,
               "error": "Operation not found", "metadata": None}
OPERATIONS: Dict[str, Any] = {
    "op-a": {"id": "op-a", "status": "Running", "resources": {"instances": ["/1.0/instances/web?project=tenant-a"]}},
    "op-b": {"id": "op-b", "status": "Running", "err": "tenant-b detail",
             "resources": {"instances": ["/1.0/instances/db?project=tenant-b"]}},
    "op-none": {"id": "op-none", "status": "Running", "resources": {}},
}


def _response(status: int, body: Any) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = requests.compat.json.dumps(body).encode()
    return response


class OperationSession:
    """Answers GET /operations/<id> from OPERATIONS (404 when absent, 500 for op-500)."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []

    def _answer(self, method: str, url: str, **_: Any) -> requests.Response:
        self.calls.append((method, url))
        operation_id = url.split("/operations/", 1)[1].split("/", 1)[0].split("?", 1)[0] if "/operations/" in url else ""
        if operation_id == "op-500":
            return _response(500, {"type": "error", "error": "boom"})
        if operation_id and operation_id not in OPERATIONS:
            return _response(404, LXD_MISSING)
        if operation_id:
            return _response(200, {"type": "sync", "metadata": OPERATIONS[operation_id]})
        return _response(200, {"type": "sync", "metadata": []})

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("GET", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("DELETE", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("POST", url, **kwargs)


@pytest.fixture
def refusals(monkeypatch) -> List[str]:
    seen: List[str] = []
    monkeypatch.setattr(integrations, "_audit_raw_refusal", lambda *args: seen.append(args[-1]))
    return seen


def _call(api_key: Dict[str, Any], method: str, path: str) -> Tuple[Any, OperationSession]:
    session = OperationSession()
    answer, _ = asyncio.run(call_raw_proxy(api_key, method, path, project="tenant-a", session=session))
    return answer, session


def test_the_owner_reads_its_own_operation(fence_calls) -> None:
    answer, session = _call(NARROW, "GET", "/1.0/operations/op-a")
    assert answer.status_code == 200 and answer.data["metadata"]["id"] == "op-a"
    assert [method for method, _ in session.calls] == ["GET", "GET"]


@pytest.mark.parametrize("method, path", [
    ("GET", "/1.0/operations/op-b"),
    ("GET", "/1.0/operations/op-b/wait?timeout=5"),
    ("DELETE", "/1.0/operations/op-b"),
    ("GET", "/1.0/Operations/op-b"),
    ("GET", "/1.0/operations/op-none"),
])
def test_another_projects_operation_is_not_found_and_nothing_is_sent(fence_calls, refusals, method, path) -> None:
    answer, session = _call(NARROW, method, path)
    assert answer.status_code == 404
    assert answer.data == LXD_MISSING
    assert "tenant-b" not in str(answer.model_dump())
    # Only the ownership read reached the cluster: no wait, no cancel.
    assert session.calls == [("GET", session.calls[0][1])] and "/operations/op-" in session.calls[0][1]
    assert refusals == ["operation_not_visible"]


def test_not_yours_reads_exactly_like_gone(fence_calls) -> None:
    refused, _ = _call(NARROW, "GET", "/1.0/operations/op-b")
    gone, _ = _call(NARROW, "GET", "/1.0/operations/op-gone")
    wildcard_gone, _ = _call(WILDCARD, "GET", "/1.0/operations/op-gone")
    assert refused.model_dump() == gone.model_dump() == wildcard_gone.model_dump()


def test_a_wildcard_key_is_forwarded_without_an_ownership_read(fence_calls) -> None:
    answer, session = _call(WILDCARD, "GET", "/1.0/operations/op-b")
    assert answer.status_code == 200 and len(session.calls) == 1


def test_the_listing_is_left_to_the_forced_project(fence_calls) -> None:
    answer, session = _call(NARROW, "GET", "/1.0/operations")
    assert answer.status_code == 200 and len(session.calls) == 1
    assert "project=tenant-a" in session.calls[0][1]


def test_an_owner_lxd_will_not_state_sends_nothing(fence_calls, refusals) -> None:
    answer, session = _call(NARROW, "DELETE", "/1.0/operations/op-500")
    assert answer.status_code == 502 and answer.data is None
    assert [method for method, _ in session.calls] == ["GET"]
    assert refusals == ["operation_owner_unknown"]


def test_the_ownership_read_does_not_double_the_api_version(fence_calls) -> None:
    _, session = _call(NARROW, "GET", "/1.0/operations/op-a")
    assert "/1.0/1.0/" not in session.calls[0][1] and session.calls[0][1].endswith("/1.0/operations/op-a")


def test_every_surface_reads_one_visibility_rule() -> None:
    assert raw_proxy_operations.visible_to is visible_to
