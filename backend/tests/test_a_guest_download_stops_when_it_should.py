"""A download holds a guest connection and a slot, so it has to be able to end, and to look ended.

`StreamingResponse` has no send timeout and no way to end on revocation. And every middleware here
is `BaseHTTPMiddleware`, which finishes the response itself when the inner app simply returns, so
a cut stream that returned quietly would hand a browser a truncated file reported as complete.
"""

import asyncio

import pytest

from app.routers import instance_file_transfer as transfer
from app.services.instance_files import guarded_stream
from app.services.instance_files.guarded_stream import ClientLeft, StreamCut, pump_guarded


class _User:
    user_id = "u1"
    groups = ["role_tenant"]


async def _body(*pieces, then_hang=False):
    for piece in pieces:
        yield piece
    if then_hang:
        await asyncio.Event().wait()


async def _quiet_receive():
    await asyncio.Event().wait()


async def _allowed(user):
    return None


async def _pump(send, *, body=None, receive=_quiet_receive, expected=None, user=None, check=_allowed, **kw):
    await pump_guarded(
        receive,
        send,
        status=200,
        headers=[],
        body=body if body is not None else _body(b"abc"),
        expected=expected,
        user=user,
        check=check,
        label="test download",
        interval=0.01,
        on_bytes=lambda sent: None,
        **kw,
    )


async def test_a_complete_stream_ends_with_the_terminal_chunk():
    sent = []

    async def send(message):
        sent.append(message)

    await _pump(send, body=_body(b"ab", b"c"), expected=3)
    assert sent[0]["type"] == "http.response.start"
    assert [m.get("body") for m in sent[1:]] == [b"ab", b"c", b""]
    assert sent[-1]["more_body"] is False


async def test_a_file_that_ends_early_never_sends_the_terminal_chunk():
    sent = []

    async def send(message):
        sent.append(message)

    with pytest.raises(StreamCut):
        await _pump(send, body=_body(b"ab"), expected=10)
    assert all(m.get("more_body", True) for m in sent)


async def test_a_client_that_stops_reading_is_cut_at_the_stall_limit():
    calls = []

    async def send(message):
        calls.append(message)
        if message["type"] == "http.response.body":
            await asyncio.Event().wait()

    with pytest.raises(StreamCut):
        await asyncio.wait_for(_pump(send, stall_seconds=0.02), 2)


async def test_a_body_that_stops_producing_hits_the_time_limit():
    async def send(message):
        return None

    with pytest.raises(StreamCut):
        await asyncio.wait_for(_pump(send, body=_body(b"a", then_hang=True), max_seconds=0.05), 2)


async def test_revocation_cuts_the_stream_without_a_terminal_chunk(monkeypatch):
    sent = []

    async def send(message):
        sent.append(message)

    async def fake_until(user, check, refused, *, label, interval):
        await asyncio.sleep(0.01)
        await refused("Access revoked")

    monkeypatch.setattr(guarded_stream, "reauthorize_until", fake_until)
    with pytest.raises(StreamCut) as cut:
        await asyncio.wait_for(_pump(send, body=_body(b"a", then_hang=True), user=_User()), 2)
    assert cut.value.reason == "Access revoked"
    assert all(m.get("more_body", True) for m in sent)


async def test_a_browser_leaving_is_a_normal_end():
    async def send(message):
        return None

    async def receive():
        return {"type": "http.disconnect"}

    with pytest.raises(ClientLeft):
        await asyncio.wait_for(_pump(send, body=_body(b"a", then_hang=True), receive=receive), 2)


def _header(headers, name):
    return dict(headers)[name].decode()


@pytest.mark.parametrize(
    "name",
    ['evil"; filename="x.html', "line\r\nSet-Cookie: a=b", "..\\..\\x", "café.txt", "‮gpj.exe", "a;b%c"],
)
def test_a_guest_file_name_cannot_touch_the_response_headers(name):
    headers = transfer.attachment_headers(name, size=1, mime="application/octet-stream")
    value = _header(headers, b"content-disposition")
    value.encode("ascii")
    assert "\r" not in value and "\n" not in value
    plain = value.split('filename="', 1)[1].split('"', 1)[0]
    assert '"' not in plain and "\\" not in plain and ";" not in plain


def test_a_download_can_never_render():
    headers = transfer.attachment_headers("page.html", size=None, mime="application/octet-stream")
    assert _header(headers, b"content-disposition").startswith("attachment")
    assert _header(headers, b"x-content-type-options") == "nosniff"
    assert "sandbox" in _header(headers, b"content-security-policy")
    assert _header(headers, b"cache-control") == "no-store"
    assert _header(headers, b"content-type") == "application/octet-stream"
    assert b"content-length" not in dict(headers)
