"""Outbound HTTP this server makes on a caller's behalf.

url_safety decides whether a URL may be called. That is not enough for a fetch on its own:
  * a name validated once and fetched later can answer differently the second time;
  * a redirect moves the request to a URL nobody validated;
  * a body read into memory before a size check lets the responder decide how much memory a worker
    uses.

guarded_request owns all three. On every hop it validates the URL, resolves the host off the loop,
refuses the hop if any answer is internal, and connects to the address it judged, carrying the
original Host header and TLS server name so the certificate is still checked against the real name.

Errors: a URL or address the policy refuses raises url_safety.UnsafeURLError. Transport failures,
a refused redirect, a wrong content type, a body over the limit, or (with raise_for_status) an
error status raise SafeHTTPError. Both carry a category, never an address or the upstream text.
"""

import ipaddress
import json as jsonlib
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence, Union
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.core.url_safety import (
    DEFAULT_PORTS,
    IPNetwork,
    UnsafeURLError,
    check_url,
    resolve_public_addresses,
)

logger = logging.getLogger(__name__)

# Split rather than one total, because a single number is gamed by a receiver that accepts the
# connection and then drips a byte at a time.
DEFAULT_TIMEOUT = httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=2.0)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class SafeHTTPError(Exception):
    """An outbound request failed or was refused after the URL passed. Carries a category only."""


@dataclass(frozen=True)
class GuardedResponse:
    status_code: int
    content_type: str
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return jsonlib.loads(self.body.decode("utf-8") or "null")


def _pinned(url: str, host: str, address: str) -> tuple:
    """(connect_url, host_header, sni_name) for connecting to `address` while naming `host`.

    `host` is the A-label check_url returned, never the raw hostname, because a Unicode name
    cannot go into a header or the TLS handshake.
    """
    parts = urlsplit(url)
    literal = ipaddress.ip_address(address)
    netloc = f"[{address}]" if literal.version == 6 else address
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    default_port = DEFAULT_PORTS.get(parts.scheme.lower())
    named = f"[{host}]" if ":" in host else host
    host_header = named if not parts.port or parts.port == default_port else f"{named}:{parts.port}"
    try:
        ipaddress.ip_address(host)
        sni = None
    except ValueError:
        sni = host if parts.scheme.lower() == "https" else None
    connect_url = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
    return connect_url, host_header, sni


async def guarded_request(
    method: str,
    url: str,
    *,
    max_bytes: int,
    headers: Optional[Mapping[str, str]] = None,
    json: Any = None,
    content: Optional[bytes] = None,
    max_redirects: int = 0,
    require_https: bool = False,
    allowed_ports: Optional[frozenset] = None,
    allowed_networks: Sequence[IPNetwork] = (),
    content_type_prefix: Optional[str] = None,
    raise_for_status: bool = True,
    timeout: Union[float, httpx.Timeout] = DEFAULT_TIMEOUT,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> GuardedResponse:
    """Make one outbound request under the URL policy, re-checked and pinned on every hop.

    `content` sends raw bytes as given, for a body a receiver verifies byte for byte (an HMAC over
    the body). It cannot be combined with `json`. Redirects are followed only for GET, only while
    hops remain, and each target passes the same checks. `transport` is a test seam.
    """
    if json is not None and content is not None:
        raise ValueError("pass json or content, not both")
    method = method.upper()
    body_bytes: Optional[bytes] = content
    send_headers: Dict[str, str] = dict(headers or {})
    if json is not None:
        body_bytes = jsonlib.dumps(json).encode("utf-8")
        send_headers.setdefault("Content-Type", "application/json")
    if not isinstance(timeout, httpx.Timeout):
        timeout = httpx.Timeout(float(timeout), connect=min(3.0, float(timeout)))

    current = url
    # No keepalive: a pooled connection outlives the moment its address was judged.
    limits = httpx.Limits(max_connections=4, max_keepalive_connections=0)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        limits=limits,
        transport=transport,
    ) as client:
        for hop in range(max_redirects + 1):
            target = check_url(
                current,
                require_https=require_https,
                allowed_ports=allowed_ports,
                allowed_networks=allowed_networks,
            )
            addresses = await resolve_public_addresses(
                target.host, target.port, allowed_networks=allowed_networks
            )
            connect_url, host_header, sni = _pinned(current, target.host, addresses[0])
            request = client.build_request(
                method,
                connect_url,
                content=body_bytes,
                headers={**send_headers, "Host": host_header},
                extensions={"sni_hostname": sni} if sni else {},
            )
            try:
                response = await client.send(request, stream=True)
            except httpx.HTTPError as exc:
                # note: the exception text can carry the target and request headers. Type only.
                logger.warning("Guarded request failed: %s", type(exc).__name__)
                raise SafeHTTPError("could not reach the URL") from exc
            try:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if method != "GET" or hop >= max_redirects or not location:
                        raise SafeHTTPError("redirect not followed")
                    current = urljoin(current, location)
                    continue
                if raise_for_status and response.status_code >= 400:
                    raise SafeHTTPError(f"upstream returned {response.status_code}")
                content_type = response.headers.get("content-type", "")
                if content_type_prefix and not content_type.lower().startswith(content_type_prefix):
                    raise SafeHTTPError("unexpected content type")
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise SafeHTTPError("response exceeds the size limit")
                received = bytearray()
                try:
                    async for chunk in response.aiter_bytes():
                        received.extend(chunk)
                        if len(received) > max_bytes:
                            raise SafeHTTPError("response exceeds the size limit")
                except httpx.HTTPError as exc:
                    logger.warning("Guarded request body failed: %s", type(exc).__name__)
                    raise SafeHTTPError("could not read the response") from exc
                return GuardedResponse(
                    status_code=response.status_code,
                    content_type=content_type,
                    body=bytes(received),
                    headers=dict(response.headers),
                )
            finally:
                await response.aclose()
    raise SafeHTTPError("too many redirects")


__all__ = ["DEFAULT_TIMEOUT", "GuardedResponse", "SafeHTTPError", "UnsafeURLError", "guarded_request"]
