"""A raw-proxy call LXD refused names LXD's error and a code, beside the unchanged envelope.

The raw proxy answers HTTP 200 with LXD's own status in `status_code` and LXD's body in `data`;
`error` is reserved for a failure to reach the cluster, the contract existing integrations rely
on. A client that read `error` saw nothing when LXD itself refused (a 403 for an untrusted or
restricted certificate, a 404, a 409), so it showed a bare status. Two optional fields now carry
LXD's message and a code from the /proxy vocabulary, and only when LXD answered 4xx or 5xx.
"""

import pytest
import requests

from app.services.lxd.proxy_operations import PROXY_ERROR_CODES
from tests._raw_proxy_fakes import WILDCARD, RecordingSession, call_raw_proxy


class Answering(RecordingSession):
    def __init__(self, status: int, body: bytes) -> None:
        super().__init__()
        self.status, self.body = status, body

    def _answer(self, verb, url, **kwargs):
        self.calls.append((verb, url, kwargs))
        response = requests.Response()
        response.status_code = self.status
        response._content = self.body
        return response


@pytest.mark.parametrize("status,code", [(404, "not_found"), (409, "conflict"), (403, "forbidden"), (400, "invalid_request"), (500, "operation_failed")])
async def test_an_lxd_refusal_names_its_message_and_code(status: int, code: str) -> None:
    body = b'{"type": "error", "error": "Project not found", "error_code": %d}' % status
    response, _session = await call_raw_proxy(WILDCARD, "GET", "/1.0/projects/missing", session=Answering(status, body))
    assert response.status_code == status
    assert response.error is None, "error still means the cluster was not reached"
    assert response.lxd_error == "Project not found"
    assert response.error_code == code
    assert code in PROXY_ERROR_CODES


async def test_a_success_carries_neither_field() -> None:
    response, _session = await call_raw_proxy(WILDCARD, "GET", "/1.0/projects")
    assert response.status_code == 200
    assert response.lxd_error is None and response.error_code is None


async def test_an_lxd_refusal_without_a_body_still_gets_a_code() -> None:
    response, _session = await call_raw_proxy(WILDCARD, "GET", "/1.0/projects/missing", session=Answering(403, b""))
    assert response.status_code == 403
    assert response.error_code == "forbidden"
    assert response.lxd_error
