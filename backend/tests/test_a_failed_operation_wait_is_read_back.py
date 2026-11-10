"""`wait_for_operation` must report a failed LXD operation as failed, not as still running.

LXD answers `/1.0/operations/<id>/wait` for a failed or cancelled operation with an error-typed
body, and pylxd raises it as LXDAPIException. Callers (`_settle` for snapshot create, delete,
restore and the safety copy, the settle-time cache clearance, the schedule runner) would take that
raise as "could not wait" and answer a failed write as accepted, so the operation is read back.
"""

import pytest
from pylxd.exceptions import LXDAPIException

from app.services.lxd import snapshots as svc


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"{}"

    def json(self):
        return self._payload


class _Op:
    def __init__(self, wait_error=None, get_result=None, get_error=None):
        self.calls = []
        self._wait_error = wait_error
        self._get_result = get_result
        self._get_error = get_error
        self.wait = self

    def get(self, params=None, timeout=None):
        kind = "wait" if "timeout" in (params or {}) else "get"
        self.calls.append((kind, dict(params or {}), timeout))
        if kind == "wait" and self._wait_error is not None:
            raise self._wait_error
        if kind == "get" and self._get_error is not None:
            raise self._get_error
        return self._get_result


class _Client:
    def __init__(self, op):
        self.api = type("Api", (), {"operations": {"op-1": op}})()


def _refused_wait(err="Failed to restore snapshot rootfs: cannot be restored due to subsequent snapshot(s)"):
    return LXDAPIException(_Resp({"type": "error", "error": err, "error_code": 500, "metadata": None}))


def test_a_refused_wait_is_read_back_as_the_failure_it_was():
    op = _Op(
        wait_error=_refused_wait(),
        get_result=_Resp({"type": "sync", "metadata": {"status": "Failure", "err": "cannot be restored due to subsequent snapshot(s)"}}),
    )
    outcome = svc.wait_for_operation(_Client(op), "op-1", "default")
    assert outcome == {
        "status": "Failure",
        "err": "cannot be restored due to subsequent snapshot(s)",
        "settled": True,
        "failed": True,
    }
    assert [c[0] for c in op.calls] == ["wait", "get"]
    _kind, params, socket_timeout = op.calls[1]
    assert params == {"project": "default"}, "the read-back stays in the operation's project"
    assert socket_timeout is not None, "the read-back has its own bounded socket window"


def test_a_cancelled_operation_read_back_is_settled_and_failed():
    op = _Op(wait_error=_refused_wait("operation cancelled"), get_result=_Resp({"type": "sync", "metadata": {"status": "Cancelled", "err": ""}}))
    outcome = svc.wait_for_operation(_Client(op), "op-1", "default")
    assert outcome["settled"] is True and outcome["failed"] is True


def test_a_wait_that_times_out_still_propagates():
    """Only a daemon answer is read back. A socket timeout says nothing about the outcome."""
    op = _Op(wait_error=TimeoutError("read timed out"))
    with pytest.raises(TimeoutError):
        svc.wait_for_operation(_Client(op), "op-1", "default")
    assert [c[0] for c in op.calls] == ["wait"]


def test_a_read_back_that_fails_too_propagates_its_own_error():
    """A reaped operation answers 404 to both; callers that map 404 (the migration executor) see it."""
    gone = LXDAPIException(_Resp({"type": "error", "error": "not found", "error_code": 404}, status_code=404))
    op = _Op(wait_error=gone, get_error=gone)
    with pytest.raises(LXDAPIException) as raised:
        svc.wait_for_operation(_Client(op), "op-1", "default")
    assert raised.value.response.status_code == 404
