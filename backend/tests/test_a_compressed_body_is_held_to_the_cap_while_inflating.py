"""A compressed body is held to the caller's cap while it inflates, not after (core/safe_http.py).

Security review 2026-11-07: fetch_guarded and post_pinned read aiter_bytes(), which hands out
decompressed chunks, so one 64 KiB gzip chunk could inflate to about 64 MB before the size check
ran. Two callers fetch or post to a URL a user chose (a branding image, a webhook receiver), so a
user's own server could send that body. Bodies are now read raw and inflated here, at most the
cap plus one byte at a time; the request asks for the body uncompressed in the first place.
"""

from __future__ import annotations

import asyncio
import gzip
import socket
import zlib

import httpx
import pytest

from app.core import safe_http
from app.core.safe_http import SafeHTTPError, fetch_guarded, post_pinned

URL = "https://feed.example/data.json"
CAP = 64 * 1024


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    real = socket.getaddrinfo

    def resolve(host, port, *args, **kwargs):
        if host in ("feed.example", "hooks.example"):
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port or 443))]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr("app.core.url_safety.socket.getaddrinfo", resolve)


class Chunks(httpx.AsyncByteStream):
    """A body that arrives in pieces, as a network read does, and counts how many were pulled."""

    def __init__(self, data: bytes, size: int = 1024):
        self.parts = [data[i:i + size] for i in range(0, len(data), size)]
        self.pulled = 0

    async def __aiter__(self):
        for part in self.parts:
            self.pulled += 1
            yield part


@pytest.fixture
def inflate_watch(monkeypatch):
    """Records the largest single output the inflater produced."""
    seen = {"largest": 0}
    real = zlib.decompressobj

    class Watched:
        def __init__(self, *args):
            self._inner = real(*args)

        def decompress(self, data, max_length=0):
            out = self._inner.decompress(data, max_length)
            seen["largest"] = max(seen["largest"], len(out))
            return out

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(safe_http.zlib, "decompressobj", Watched)
    return seen


def _fetch(response: httpx.Response, *, headers=None, requests=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return response

    return asyncio.run(fetch_guarded(
        URL, max_bytes=CAP, transport=httpx.MockTransport(handler), headers=headers,
        allowed_hosts=frozenset({"feed.example"}),
    ))


def test_a_gzip_bomb_is_refused_without_inflating_past_the_cap(inflate_watch) -> None:
    bomb = gzip.compress(b"\0" * (64 * 1024 * 1024))  # 64 MB of zeros, about 64 KB on the wire
    stream = Chunks(bomb)
    with pytest.raises(SafeHTTPError, match="size limit"):
        _fetch(httpx.Response(200, headers={"content-encoding": "gzip"}, stream=stream))
    assert inflate_watch["largest"] <= CAP + 1
    assert stream.pulled < len(stream.parts), "the rest of the body is never read"


def test_a_compressed_body_within_the_cap_is_inflated_and_returned() -> None:
    payload = b'{"ok": true, "pad": "' + b"a" * 20000 + b'"}'
    for encoding, data in (("gzip", gzip.compress(payload)), ("deflate", zlib.compress(payload))):
        content_type, body = _fetch(httpx.Response(
            200, headers={"content-encoding": encoding, "content-type": "application/json"}, stream=Chunks(data, 512),
        ))
        assert (content_type, body) == ("application/json", payload)


@pytest.mark.parametrize("encoding, data, message", [
    ("br", b"\x0b\x02\x80hello\x03", "unsupported content encoding"),
    ("gzip, gzip", gzip.compress(gzip.compress(b"x")), "stacked content encodings"),
    ("gzip", b"this is not gzip at all", "does not match its content encoding"),
    ("gzip", gzip.compress(b"x" * 5000)[:-12], "cut short"),
])
def test_a_body_this_cannot_inflate_safely_is_refused(encoding, data, message) -> None:
    with pytest.raises(SafeHTTPError, match=message):
        _fetch(httpx.Response(200, headers={"content-encoding": encoding}, stream=Chunks(data, 256)))


def test_the_request_asks_for_the_body_uncompressed() -> None:
    requests = []
    _fetch(httpx.Response(200, stream=Chunks(b"plain")), requests=requests)
    assert requests[0].headers["accept-encoding"] == "identity"
    requests.clear()
    _fetch(httpx.Response(200, stream=Chunks(b"plain")), headers={"Accept-Encoding": "gzip"}, requests=requests)
    assert requests[0].headers["accept-encoding"] == "gzip", "a caller's own choice is kept"


def test_a_plain_body_still_meets_the_cap_on_the_wire() -> None:
    with pytest.raises(SafeHTTPError, match="size limit"):
        _fetch(httpx.Response(200, stream=Chunks(b"x" * (CAP + 10))))


def _post(response: httpx.Response, limit: int = 4096):
    return asyncio.run(post_pinned(
        "https://hooks.example/receive", content=b"{}", headers={"Content-Type": "application/json"},
        max_response_bytes=limit, transport=httpx.MockTransport(lambda request: response),
        exemptions=(), require_https=True,
    ))


def test_a_receivers_gzip_bomb_yields_a_short_excerpt_and_stops(inflate_watch) -> None:
    stream = Chunks(gzip.compress(b"A" * (32 * 1024 * 1024)))
    status, head, seen = _post(httpx.Response(500, headers={"content-encoding": "gzip"}, stream=stream))
    assert status == 500
    assert head == b"A" * 4096
    assert inflate_watch["largest"] <= 4096 + 1
    assert seen <= 4096 + 1024, "the raw read stops right after the limit"
    assert stream.pulled < len(stream.parts)


def test_a_receivers_compressed_reply_is_readable_and_an_unknown_encoding_keeps_the_status() -> None:
    status, head, _seen = _post(httpx.Response(
        200, headers={"content-encoding": "gzip"}, stream=Chunks(gzip.compress(b"thanks"), 4),
    ))
    assert (status, head) == (200, b"thanks")
    status, head, _seen = _post(httpx.Response(202, headers={"content-encoding": "br"}, stream=Chunks(b"\x0b\x02")))
    assert (status, head) == (202, b"")


@pytest.mark.parametrize("data, message", [
    # a tiny member, then a trickle: refused instead of copied into zlib's tail chunk after chunk
    (gzip.compress(b"ok") + b"\0" * 4096, "data after the end"),
    (gzip.compress(b"one") + gzip.compress(b"two"), "data after the end"),
    (b"", "cut short"),
])
def test_bytes_after_the_compressed_stream_or_none_at_all_are_refused(data, message) -> None:
    with pytest.raises(SafeHTTPError, match=message):
        _fetch(httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Chunks(data, 64)))


def test_a_bare_deflate_stream_still_reads_as_httpx_read_it() -> None:
    raw = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    data = raw.compress(b"bare deflate body" * 100) + raw.flush()
    _content_type, body = _fetch(httpx.Response(200, headers={"content-encoding": "deflate"}, stream=Chunks(data, 128)))
    assert body == b"bare deflate body" * 100


def test_both_caps_hold_at_the_boundary_and_on_the_wire() -> None:
    exact = gzip.compress(b"y" * CAP)
    assert _fetch(httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Chunks(exact)))[1] == b"y" * CAP
    with pytest.raises(SafeHTTPError, match="size limit"):
        _fetch(httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Chunks(gzip.compress(b"y" * (CAP + 1)))))
    # incompressible: more raw bytes than the cap, refused on the wire before it inflates that far
    noise = gzip.compress(bytes((i * 7919 + i // 3) % 256 for i in range(CAP * 2)), compresslevel=0)
    with pytest.raises(SafeHTTPError, match="size limit"):
        _fetch(httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Chunks(noise)))


@pytest.mark.parametrize("encoding", ["x-gzip", " GZip ", "gzip, identity"])
def test_encoding_names_are_read_as_written(encoding) -> None:
    body = _fetch(httpx.Response(200, headers={"content-encoding": encoding}, stream=Chunks(gzip.compress(b"hi"))))[1]
    assert body == b"hi"


def test_a_redirect_hop_asks_for_the_body_uncompressed_too() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(302, headers={"location": "/moved.json"})
        return httpx.Response(200, stream=Chunks(b"moved"))

    body = asyncio.run(fetch_guarded(
        URL, max_bytes=CAP, transport=httpx.MockTransport(handler), allowed_hosts=frozenset({"feed.example"}),
    ))[1]
    assert body == b"moved"
    assert [r.headers["accept-encoding"] for r in requests] == ["identity", "identity"]


def test_a_delivery_asks_for_identity_counts_raw_bytes_and_drops_a_corrupt_excerpt() -> None:
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Chunks(gzip.compress(b"z" * 100), 8))

    wire = len(gzip.compress(b"z" * 100))
    status, head, seen = asyncio.run(post_pinned(
        "https://hooks.example/receive", content=b"{}", headers={}, max_response_bytes=4096,
        transport=httpx.MockTransport(handler), exemptions=(), require_https=True,
    ))
    assert sent[0].headers["accept-encoding"] == "identity"
    assert (status, head, seen) == (200, b"z" * 100, wire)
    status, head, _seen = _post(httpx.Response(
        200, headers={"content-encoding": "gzip"}, stream=Chunks(gzip.compress(b"z" * 100)[:10] + b"garbage!!", 4),
    ))
    assert (status, head) == (200, b"")
