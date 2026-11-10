"""The exec that carries a password: nothing leaves before the pin is proved, and an unanswered send
is reported as unknown, never as a failure that would make someone rely on the password it may have
replaced."""

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from app.services.guest_credentials import errors, pinned_exec, runtime
from app.services.lxd_ws import CERTIFICATE_MISMATCH, SessionSetupError

SECRET_LINE = b"ubuntu:Zq8vKcanaryMx3pLwR7t\n"


class _Socket:
    def __init__(self, key: str, output: List[bytes], hang: bool = False) -> None:
        self.key, self.output, self.hang = key, list(output), hang
        self.sent: List[Any] = []
        self.closed = False

    async def send(self, message: Any) -> None:
        self.sent.append(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.hang:
            await asyncio.sleep(3600)
        if self.output:
            return self.output.pop(0)
        raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True


class _Lxd:
    def __init__(self, exit_code=0, wait_fails=False) -> None:
        self.posts: List[Dict[str, Any]] = []
        self.exit_code, self.wait_fails = exit_code, wait_fails
        outer = self

        class _Exec:
            def post(self, json, timeout):
                outer.posts.append(json)
                return SimpleNamespace(json=lambda: {"metadata": {"id": "op-1", "metadata": {"fds": {
                    "0": "s0", "1": "s1", "2": "s2", "control": "sc"}}}})

        class _Wait:
            def get(self, params, timeout):
                if outer.wait_fails:
                    raise TimeoutError("read timed out")
                return SimpleNamespace(json=lambda: {"metadata": {"metadata": {"return": outer.exit_code}}})

        self.instance = SimpleNamespace(
            api=SimpleNamespace(exec=_Exec()),
            client=SimpleNamespace(api=SimpleNamespace(operations={"op-1": SimpleNamespace(wait=_Wait())})),
        )


def _guest(lxd: _Lxd):
    return SimpleNamespace(host="lab", project="default", name="vm", instance=lxd.instance)


def _patch(monkeypatch, *, pin="ab" * 32, sockets=None, refuse=False):
    monkeypatch.setattr(pinned_exec, "socket_material", lambda host: ("wss://lab:8443", object(), pin))
    opened: List[_Socket] = []

    async def open_socket(url, context, *, pin, timeout, max_size):
        assert pin, "a pinned exec never opens an unpinned socket"
        if refuse:
            raise SessionSetupError(CERTIFICATE_MISMATCH, retryable=False)
        key = url.rsplit("secret=", 1)[1]
        socket = (sockets or {}).get(key) or _Socket(key, [])
        opened.append(socket)
        return socket

    monkeypatch.setattr(pinned_exec, "open_lxd_socket", open_socket)
    return opened


def _run(guest, stdin=SECRET_LINE):
    return asyncio.run(pinned_exec.run_with_stdin(guest, ["chpasswd"], stdin, user=None, group=None, label="t"))


def test_no_pin_means_no_exec_is_even_posted(monkeypatch):
    lxd = _Lxd()
    _patch(monkeypatch, pin="")
    with pytest.raises(errors.CredentialError) as refused:
        _run(_guest(lxd))
    assert refused.value.code == "cluster_not_pinned"
    assert lxd.posts == []


def test_a_stranger_certificate_gets_no_stdin(monkeypatch):
    lxd = _Lxd()
    opened = _patch(monkeypatch, refuse=True)
    with pytest.raises(errors.CredentialError) as refused:
        _run(_guest(lxd))
    assert refused.value.code == "cluster_not_pinned"
    assert opened == []


def test_stdin_is_one_binary_frame_then_the_end_of_input(monkeypatch):
    lxd = _Lxd()
    stdout = _Socket("s1", [b"ORCA_OK\n"])
    stdin = _Socket("s0", [])
    opened = _patch(monkeypatch, sockets={"s0": stdin, "s1": stdout})
    result = _run(_guest(lxd))
    assert (result.stdout, result.exit_code) == ("ORCA_OK\n", 0)
    assert stdin.sent == [SECRET_LINE, ""]
    assert sorted(s.key for s in opened) == ["s0", "s1", "s2", "sc"]
    assert all(s.closed for s in opened)
    body = lxd.posts[0]
    assert body["command"] == ["chpasswd"] and SECRET_LINE.decode() not in repr(body)
    assert body["wait-for-websocket"] is True and body["interactive"] is False and body["record-output"] is False


def test_output_is_bounded_as_it_arrives(monkeypatch):
    lxd = _Lxd()
    flood = _Socket("s1", [b"x" * 8192] * 64)
    _patch(monkeypatch, sockets={"s1": flood})
    result = _run(_guest(lxd))
    assert len(result.stdout) == runtime.STDIN_CALL_OUTPUT_LIMIT


def test_a_send_with_no_exit_code_is_unknown_not_failed(monkeypatch):
    lxd = _Lxd(wait_fails=True)
    _patch(monkeypatch)
    with pytest.raises(errors.CredentialError) as raised:
        _run(_guest(lxd))
    assert (raised.value.code, raised.value.status) == ("credential_outcome_unknown", 504)


def test_a_guest_that_never_closes_its_output_is_unknown_after_the_wall_clock(monkeypatch):
    lxd = _Lxd()
    monkeypatch.setattr(runtime, "EXEC_TIMEOUT", 0.3)
    _patch(monkeypatch, sockets={"s1": _Socket("s1", [], hang=True)})
    with pytest.raises(errors.CredentialError) as raised:
        _run(_guest(lxd))
    assert raised.value.code == "credential_outcome_unknown"


def test_the_refusal_reaches_the_caller_as_a_code_not_a_stack(monkeypatch):
    lxd = _Lxd()
    _patch(monkeypatch, refuse=True)
    try:
        _run(_guest(lxd))
    except errors.CredentialError as failure:
        answer = failure.to_http()
    assert isinstance(answer, HTTPException) and answer.detail["error"] == "cluster_not_pinned"
    assert "secret" not in repr(answer.detail)
