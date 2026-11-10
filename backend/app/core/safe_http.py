"""Outbound HTTP the server makes on a caller's behalf.

`core/url_safety.py` decides at the request boundary whether a URL may be called. A later fetch
needs more: a validated name can resolve differently when it is fetched, a redirect moves the
request to an unvalidated URL, and a response body is read into memory before the caller can
check its size. Every outbound fetch on behalf of a caller goes through here, which pins the
judged address, rechecks every redirect and caps the body while streaming it. A test keeps
`url_safety.py` the only private-range table.
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

# Per-phase limits. httpx applies `read` to each wait for the next chunk; there is no total.
DEFAULT_TIMEOUT = httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=2.0)

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Every fetch asks for the body as it is stored. A server may still compress it, and httpx would
# then hand out decompressed chunks of any size before a cap could look at them: one 64 KiB gzip
# chunk inflates to about 64 MB. So bodies are read raw and inflated here, never more than the
# caller's limit plus one byte at a time.
_ACCEPT_ENCODING = "identity"


class _Undecodable(Exception):
    """A Content-Encoding this module does not inflate, or a body that is not what it claims."""


class _Inflater:
    """One gzip or deflate layer. "deflate" is meant to be zlib-wrapped, but some servers send the
    bare stream, which httpx also accepts; the first block decides, and the stream is read once."""

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
    unused_data on every chunk, a cost that grows with the square of what a sender trickles in,
    and a second gzip member is not something these callers expect.
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
    past `max_bytes`; the body is streamed and abandoned as soon as it exceeds the cap.

    `allowed_hosts` (lowercase) pins every hop, the first included, to those names, for a fetch
    with a fixed purpose such as the release feed. `exemptions` are the private networks an
    operator allowed for it (url_safety.describe_url_risk).
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
            # important: connect to the address just judged, never to the name. If httpx resolved
            # the name again, a rebinding name could pass the check as public and then connect to
            # loopback. Every hop is pinned, redirects included.
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
                    # important: read raw chunks; aiter_bytes inflates before any cap applies. Both
                    # the bytes on the wire and what they inflate to are held to max_bytes.
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
                # The exception string can carry the target URL and, from httpx, sometimes
                # request headers. Log the type only and hand the caller a category.
                logger.warning("Guarded fetch failed: %s", type(exc).__name__)
                raise SafeHTTPError("could not reach the URL") from exc

    raise SafeHTTPError("too many redirects")


def _host_header(url: str) -> str:
    """The Host a pinned request carries: the name and any non-default port, never userinfo."""
    return urlsplit(url).netloc.rsplit("@", 1)[-1]


def _pin(url: str, *, exemptions: Sequence[Any] = ()) -> Tuple[str, str, str]:
    """Resolve once and rewrite the URL to connect to that exact address.

    Returns (connect_url, original_host, resolved_address). Without it the HTTP client resolves
    the name again to connect, and a stored endpoint can answer with a public address when judged
    and an internal one later. Every address the name answers with is judged. The caller sends
    the real name in `Host` and `sni_hostname`, so TLS validates the certificate against it.
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
    """Deliver a signed body to a caller-registered endpoint, pinned to the judged address.

    Returns (status_code, first bytes of the response, total bytes seen). A redirect is a delivery
    failure: the receiver controls the URL it registered, so it has no need to redirect.

    `exemptions` and `require_https` default to the webhook settings. A caller whose URL was
    accepted under a stricter boundary passes that boundary.
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

    # No keepalive: a pooled connection outlives the moment its address was judged.
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
                    # Stop reading: the reply is only kept as a diagnostic excerpt.
                    break
            return response.status_code, bytes(head[:max_response_bytes]), seen
        finally:
            await response.aclose()
