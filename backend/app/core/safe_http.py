"""Outbound HTTP the server makes on a caller's behalf.

`core/url_safety.py` decides whether a URL is one this server may call. That check happens at the
request boundary and is the right shape for a Pydantic refusal, but on its own it is not enough
for anything the server fetches later:

  * a name validated once and fetched afterwards can answer differently the second time, and the
    gap is the whole life of the stored value rather than a few milliseconds;
  * a redirect moves the request to a URL nobody validated at all;
  * a response body is read into memory before any size check the caller writes after the call.

So every outbound fetch on behalf of a caller goes through here, and this module owns all three.
There is exactly one such implementation in the tree, enforced by a test, because the reason the
image proxy shipped a weaker copy of the private-range list is that having two made one of them
stale without anybody noticing.
"""

import asyncio
import ipaddress
import logging
import zlib
from typing import Any, AsyncIterator, FrozenSet, Mapping, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.core.url_safety import describe_url_risk, resolve_safe_addresses

logger = logging.getLogger(__name__)

# Split rather than a single total, because one number is gamed by a receiver that accepts the
# connection and then drips a byte at a time.
DEFAULT_TIMEOUT = httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=2.0)

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Every fetch asks for the body as it is stored. A server may still compress it, and httpx would
# then hand out decompressed chunks of any size before a cap could look at them: one 64 KiB gzip
# chunk inflates to about 64 MB (security review 2026-11-07). So bodies are read raw and inflated
# here, never more than the caller's limit plus one byte at a time.
_ACCEPT_ENCODING = "identity"


class _Undecodable(Exception):
    """A Content-Encoding this module does not inflate, or a body that is not what it claims."""


class _Inflater:
    """One gzip or deflate layer. "deflate" is meant to be zlib-wrapped, but some servers send the
    bare stream, which httpx accepted; the first block decides, and the stream is read once."""

    def __init__(self, gzip: bool) -> None:
        self._raw_fallback = not gzip
        self._fed = False
        self._z = zlib.decompressobj(16 + zlib.MAX_WBITS if gzip else zlib.MAX_WBITS)

    @property
    def eof(self) -> bool:
        return self._z.eof

    @property
    def unused_data(self) -> bytes:
        return self._z.unused_data

    @property
    def unconsumed_tail(self) -> bytes:
        return self._z.unconsumed_tail

    def decompress(self, data: bytes, max_length: int) -> bytes:
        try:
            out = self._z.decompress(data, max_length)
        except zlib.error:
            if not (self._raw_fallback and not self._fed):
                raise
            self._z = zlib.decompressobj(-zlib.MAX_WBITS)
            out = self._z.decompress(data, max_length)
        self._fed = True
        return out


def _decoder(content_encoding: Optional[str]) -> Optional[_Inflater]:
    """An inflater for one gzip or deflate layer, or None for a plain body."""
    layers = [part.strip().lower() for part in (content_encoding or "").split(",")]
    layers = [part for part in layers if part and part != "identity"]
    if not layers:
        return None
    if len(layers) > 1:
        raise _Undecodable("stacked content encodings")
    if layers[0] in ("gzip", "x-gzip"):
        return _Inflater(gzip=True)
    if layers[0] == "deflate":
        return _Inflater(gzip=False)
    raise _Undecodable("unsupported content encoding")


def _inflate_into(decoder: _Inflater, raw: bytes, body: bytearray, limit: int) -> bool:
    """Append what `raw` inflates to, stopping one byte past `limit`. True when it went past.

    Bytes after the end of the compressed stream are refused: zlib would keep copying them into
    unused_data on every chunk, a cost that grows with the square of what a sender trickles in
    (security review 2026-11-07), and a second gzip member is not something these callers expect.
    """
    data = raw
    try:
        while data:
            if decoder.eof:
                raise _Undecodable("data after the end of the compressed body")
            out = decoder.decompress(data, limit + 1 - len(body))
            body.extend(out)
            if len(body) > limit:
                return True
            if decoder.unused_data:
                raise _Undecodable("data after the end of the compressed body")
            data = decoder.unconsumed_tail
    except zlib.error as exc:
        raise _Undecodable("body does not match its content encoding") from exc
    return False


async def _one(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _body_source(response: httpx.Response) -> Tuple[AsyncIterator[bytes], Optional[str]]:
    """(chunks, content encoding still to undo) for a streamed response.

    The raw stream as it arrives, normally. httpx reads an in-memory body in full when the
    response is built (a mock transport does this), and that body is already decoded, so it is
    handed over as one plain chunk and still meets the same cap.
    """
    if response.is_stream_consumed:
        return _one(response.content), None
    return response.aiter_raw(), response.headers.get("content-encoding")


def _with_accept_encoding(headers: Optional[Mapping[str, str]]) -> dict:
    merged = dict(headers or {})
    if not any(key.lower() == "accept-encoding" for key in merged):
        merged["Accept-Encoding"] = _ACCEPT_ENCODING
    return merged


class SafeHTTPError(Exception):
    """An outbound fetch was refused or failed. Carries a category, never an address.

    `status_code` is set when the destination answered with an error status, so a caller can
    report the status without parsing the message.
    """

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


async def fetch_guarded(
    url: str,
    *,
    max_bytes: int,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    max_redirects: int = 3,
    require_https: bool = False,
    headers: Optional[dict] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    allowed_hosts: Optional[FrozenSet[str]] = None,
    exemptions: Sequence[Any] = (),
) -> Tuple[str, bytes]:
    """GET `url`, following redirects only to destinations that pass the same check.

    Returns (content_type, body). Raises SafeHTTPError on refusal, transport failure, or a body
    that runs past `max_bytes`.

    `allowed_hosts` pins every hop, the first included, to those names (lowercase): a fetch with
    a fixed purpose, like the release feed, never follows a redirect anywhere else. `exemptions`
    are the private networks an operator allowed for it (url_safety.describe_url_risk).

    important: the body is streamed and abandoned the moment it exceeds the cap. Reading first
    and measuring afterwards means a hostile responder decides how much memory a worker uses.
    """
    current = url
    # No keepalive: a pooled connection outlives the moment its address was judged (see _pin).
    limits = httpx.Limits(max_connections=4, max_keepalive_connections=0)
    # `transport` is a test seam. Production passes nothing and gets httpx's own.
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, limits=limits, transport=transport
    ) as client:
        for _ in range(max_redirects + 1):
            # The risk check resolves the name with a blocking getaddrinfo, so it runs off the loop.
            reason = await asyncio.to_thread(
                describe_url_risk, current, require_https=require_https, exemptions=exemptions
            )
            if reason is not None:
                raise SafeHTTPError(reason)
            if allowed_hosts is not None and (urlsplit(current).hostname or "").lower() not in allowed_hosts:
                raise SafeHTTPError("destination is not one this fetch may reach")
            # important: connect to the address just judged, never to the name. httpx resolving the
            # name again let a rebinding name pass the check public and connect to loopback
            # (security review 2026-11-05, finding 3). Every hop is pinned, redirects included.
            try:
                connect_url, host, _address = await asyncio.to_thread(_pin, current, exemptions=exemptions)
            except ValueError as exc:
                raise SafeHTTPError(str(exc)) from exc
            request = client.build_request(
                "GET", connect_url, headers={**_with_accept_encoding(headers), "Host": _host_header(current)},
                extensions={"sni_hostname": host},
            )

            try:
                response = await client.send(request, stream=True)
                try:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise SafeHTTPError("redirect without a destination")
                        # Relative redirects are legal and common on CDNs; resolved against the
                        # name, never the pinned literal.
                        current = urljoin(current, location)
                        continue

                    if response.status_code >= 400:
                        raise SafeHTTPError(
                            f"upstream returned {response.status_code}", status_code=response.status_code
                        )

                    chunks, encoding = _body_source(response)
                    try:
                        decoder = _decoder(encoding)
                    except _Undecodable as exc:
                        raise SafeHTTPError(str(exc)) from exc
                    body = bytearray()
                    raw_seen = 0
                    # important: raw chunks, not aiter_bytes. Both the bytes on the wire and what
                    # they inflate to are held to max_bytes (see _ACCEPT_ENCODING).
                    async for chunk in chunks:
                        raw_seen += len(chunk)
                        if raw_seen > max_bytes:
                            raise SafeHTTPError("response exceeds the size limit")
                        if decoder is None:
                            body.extend(chunk)
                            over = len(body) > max_bytes
                        else:
                            try:
                                over = _inflate_into(decoder, chunk, body, max_bytes)
                            except _Undecodable as exc:
                                raise SafeHTTPError(str(exc)) from exc
                        if over:
                            raise SafeHTTPError("response exceeds the size limit")
                    if decoder is not None and not decoder.eof:
                        raise SafeHTTPError("response was cut short")
                    return response.headers.get("content-type", ""), bytes(body)
                finally:
                    await response.aclose()
            except SafeHTTPError:
                raise
            except httpx.HTTPError as exc:
                # note: the exception string can carry the target URL and, from httpx, sometimes
                # request headers. Log the type only and hand the caller a category.
                logger.warning("Guarded fetch failed: %s", type(exc).__name__)
                raise SafeHTTPError("could not reach the URL") from exc

    raise SafeHTTPError("too many redirects")


def _host_header(url: str) -> str:
    """The Host a pinned request carries: the name and any non-default port, never userinfo."""
    return urlsplit(url).netloc.rsplit("@", 1)[-1]


def _pin(url: str, *, exemptions: Sequence[Any] = ()) -> Tuple[str, str, str]:
    """Resolve once and rewrite the URL to connect to that exact address.

    Returns (connect_url, original_host, resolved_address).

    important: this is the half a boundary check cannot do. `describe_url_risk` resolves a name to
    decide whether it is safe, and then the HTTP client resolves it again to connect. For a value
    validated at the boundary and fetched moments later the gap is small. For a delivery endpoint
    stored at subscribe time and contacted for the life of an installation, the gap is the whole
    life of the installation: a name registered against a public record can answer with Vault's
    address the next day and nothing revalidates.

    Pinning closes it by resolving exactly once, judging every address that name answers with, and
    then connecting to the literal rather than to the name. `Host` and `sni_hostname` are carried
    across so the server still receives the request it expects and TLS still validates the
    certificate against the real hostname, which means a pinned connection to an attacker's
    address fails the handshake rather than succeeding quietly.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    addresses = resolve_safe_addresses(host, port=parts.port, exemptions=exemptions)
    address = addresses[0]

    literal = ipaddress.ip_address(address)
    netloc = f"[{address}]" if literal.version == 6 else address
    if parts.port:
        netloc = f"{netloc}:{parts.port}"

    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment)), host, address


async def post_pinned(
    url: str,
    *,
    content: bytes,
    headers: Mapping[str, str],
    max_response_bytes: int,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    exemptions: Optional[Sequence[Any]] = None,
    require_https: Optional[bool] = None,
) -> Tuple[int, bytes, int]:
    """Deliver a signed body to a caller-registered endpoint, safely.

    Returns (status_code, first bytes of the response, total bytes seen).

    Redirects are refused outright rather than revalidated. A receiver controls the URL it
    registered with us, so it has no legitimate need to redirect, and every implementation of
    per-hop revalidation in the wild has had a bug in it. A 3xx is a delivery failure with a
    reason, which is more useful to the operator than a silent hop.

    `exemptions` and `require_https` default to the webhook settings. A caller whose URL was
    accepted under a stricter boundary passes that boundary, so delivery is held to the same rule.
    """
    from app.core.config import webhook_destination_exemptions, webhook_requires_https

    if exemptions is None:
        exemptions = webhook_destination_exemptions()
    if require_https is None:
        require_https = webhook_requires_https()
    # Both resolve the name with a blocking getaddrinfo, so they run off the loop.
    reason = await asyncio.to_thread(
        describe_url_risk, url, require_https=require_https, exemptions=exemptions
    )
    if reason is not None:
        raise SafeHTTPError(reason)

    try:
        connect_url, host, address = await asyncio.to_thread(_pin, url, exemptions=exemptions)
    except ValueError as exc:
        raise SafeHTTPError(str(exc)) from exc

    # No keepalive: a pooled connection outlives the moment its address was judged, which is the
    # same staleness the pin exists to remove.
    limits = httpx.Limits(max_connections=4, max_keepalive_connections=0)
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, limits=limits, transport=transport
    ) as client:
        request = client.build_request(
            "POST",
            connect_url,
            content=content,
            headers={**_with_accept_encoding(headers), "Host": _host_header(url)},
            extensions={"sni_hostname": host},
        )
        try:
            response = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            logger.warning("Pinned delivery failed: %s", type(exc).__name__)
            raise SafeHTTPError("could not reach the endpoint") from exc

        try:
            if response.status_code in _REDIRECT_STATUSES:
                raise SafeHTTPError("endpoint redirected, which a delivery does not follow")

            # Read raw and inflate here, as fetch_guarded does: a receiver is caller-registered,
            # so a compressed reply is no reason to inflate megabytes for a log excerpt. An
            # encoding this cannot read keeps the status and drops the excerpt.
            chunks, encoding = _body_source(response)
            try:
                decoder = _decoder(encoding)
            except _Undecodable:
                decoder, readable = None, False
            else:
                readable = True
            seen = 0
            head = bytearray()
            async for chunk in chunks:
                seen += len(chunk)
                if readable and len(head) < max_response_bytes:
                    if decoder is None:
                        head.extend(chunk[: max_response_bytes - len(head)])
                    else:
                        try:
                            _inflate_into(decoder, chunk, head, max_response_bytes)
                        except _Undecodable:
                            readable = False
                            head.clear()
                if seen > max_response_bytes:
                    # Stop reading. What the receiver says is diagnostic, not payload.
                    break
            return response.status_code, bytes(head[:max_response_bytes]), seen
        finally:
            await response.aclose()
