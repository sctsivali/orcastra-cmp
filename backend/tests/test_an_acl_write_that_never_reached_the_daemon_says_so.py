"""An ACL write that never reached the daemon, or timed out waiting for it, is 503 or 504.

The write handlers keep the daemon's own 4xx (a bad rule is 400, a stale ETag is 412). Any
other failure as a bare 500 would be replaced by main.py with an opaque reference, and a host
that is down would look the same as one that is merely slow. The mapping every read path uses
answers 503 for a connection that never came up and 504 for a read that ran past its budget.
"""

import requests.exceptions as requests_exc
import urllib3.exceptions as urllib3_exc
from urllib3.exceptions import MaxRetryError

from tests.test_an_acl_edit_refuses_a_stale_write import _app


def _pooled_read_timeout() -> Exception:
    """The shape the pooled adapter (Retry(read=0)) produces for a read that ran past 8 s."""
    return requests_exc.ConnectionError(
        MaxRetryError(None, "/1.0/network-acls/qaacl",
                      reason=urllib3_exc.ReadTimeoutError(None, "u", "Read timed out.")),
    )


def _connect_refused() -> Exception:
    return requests_exc.ConnectionError(
        MaxRetryError(None, "/1.0/network-acls/qaacl",
                      reason=urllib3_exc.NewConnectionError(None, "Connection refused")),
    )


def _raising(exc: Exception):
    def _answer():
        raise exc
    return _answer


def test_a_read_timeout_on_the_write_is_a_504_naming_the_host(monkeypatch) -> None:
    tc, _, _ = _app(monkeypatch, put_answer=_raising(_pooled_read_timeout()))
    r = tc.put("/network-acls/qaacl?host=nuc1", json={"description": "after"},
               headers={"If-Match": '"abc"'})
    assert r.status_code == 504, r.text
    assert "nuc1" in r.json()["detail"]
    assert "Read timed out" not in r.text, "the transport text is not the operator's diagnosis"


def test_a_refused_connection_on_the_write_is_a_503(monkeypatch) -> None:
    tc, _, _ = _app(monkeypatch, put_answer=_raising(_connect_refused()))
    r = tc.put("/network-acls/qaacl?host=nuc1", json={"description": "after"},
               headers={"If-Match": '"abc"'})
    assert r.status_code == 503, r.text
    assert "nuc1" in r.json()["detail"]


def test_the_daemons_own_status_still_wins_over_the_blanket_500(monkeypatch) -> None:
    """The transport branch must not swallow the 412 the stale-write contract depends on."""
    from types import SimpleNamespace
    from pylxd import exceptions as pylxd_exceptions

    def _stale():
        raise pylxd_exceptions.LXDAPIException(SimpleNamespace(status_code=412, json=lambda: {"error": "ETag mismatch"}))

    tc, _, _ = _app(monkeypatch, put_answer=_stale)
    r = tc.put("/network-acls/qaacl?host=nuc1", json={"description": "after"},
               headers={"If-Match": '"abc"'})
    assert r.status_code == 412, r.text
