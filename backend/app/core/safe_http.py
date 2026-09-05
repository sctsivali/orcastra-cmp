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

import logging
from typing import Optional, Tuple
from urllib.parse import urljoin

import httpx

from app.core.url_safety import describe_url_risk

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
) -> Tuple[str, bytes]:
    """GET `url`, following redirects only to destinations that pass the same check.

    Returns (content_type, body). Raises SafeHTTPError on refusal, transport failure, or a body
    that runs past `max_bytes`.

    important: the body is streamed and abandoned the moment it exceeds the cap. Reading first
    and measuring afterwards means a hostile responder decides how much memory a worker uses.
    """
    current = url
    # `transport` is a test seam. Production passes nothing and gets httpx's own.
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, transport=transport
    ) as client:
        for _ in range(max_redirects + 1):
            reason = describe_url_risk(current, require_https=require_https)
            if reason is not None:
                raise SafeHTTPError(reason)

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
