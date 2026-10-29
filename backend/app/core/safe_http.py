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
from typing import Any, FrozenSet, Mapping, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.core.url_safety import describe_url_risk, resolve_safe_addresses

logger = logging.getLogger(__name__)

# Split rather than a single total, because one number is gamed by a receiver that accepts the
# connection and then drips a byte at a time.
DEFAULT_TIMEOUT = httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=2.0)

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class SafeHTTPError(Exception):
    """An outbound fetch was refused or failed. Carries a category, never an address."""


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
    # `transport` is a test seam. Production passes nothing and gets httpx's own.
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, transport=transport
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

            try:
                async with client.stream("GET", current, headers=headers) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise SafeHTTPError("redirect without a destination")
                        # Relative redirects are legal and common on CDNs.
                        current = urljoin(current, location)
                        continue

                    if response.status_code >= 400:
                        raise SafeHTTPError(f"upstream returned {response.status_code}")

                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise SafeHTTPError("response exceeds the size limit")
                    return response.headers.get("content-type", ""), bytes(body)
            except SafeHTTPError:
                raise
            except httpx.HTTPError as exc:
                # note: the exception string can carry the target URL and, from httpx, sometimes
                # request headers. Log the type only and hand the caller a category.
                logger.warning("Guarded fetch failed: %s", type(exc).__name__)
                raise SafeHTTPError("could not reach the URL") from exc

    raise SafeHTTPError("too many redirects")


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
) -> Tuple[int, bytes, int]:
    """Deliver a signed body to a caller-registered endpoint, safely.

    Returns (status_code, first bytes of the response, total bytes seen).

    Redirects are refused outright rather than revalidated. A receiver controls the URL it
    registered with us, so it has no legitimate need to redirect, and every implementation of
    per-hop revalidation in the wild has had a bug in it. A 3xx is a delivery failure with a
    reason, which is more useful to the operator than a silent hop.
    """
    from app.core.config import webhook_destination_exemptions, webhook_requires_https

    exemptions = webhook_destination_exemptions()
    reason = describe_url_risk(
        url, require_https=webhook_requires_https(), exemptions=exemptions
    )
    if reason is not None:
        raise SafeHTTPError(reason)

    try:
        connect_url, host, address = _pin(url, exemptions=exemptions)
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
            headers={**dict(headers), "Host": host},
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

            seen = 0
            head = bytearray()
            async for chunk in response.aiter_bytes():
                seen += len(chunk)
                if len(head) < max_response_bytes:
                    head.extend(chunk[: max_response_bytes - len(head)])
                if seen > max_response_bytes:
                    # Stop reading. What the receiver says is diagnostic, not payload.
                    break
            return response.status_code, bytes(head), seen
        finally:
            await response.aclose()
