"""The requests this server sends to an integration's stored `app_url`.

Three requests are built from that URL, each carrying a credential: the lifecycle webhook (an
HMAC), the member push (the key's plaintext secret) and the organization refresh (the secret's
hash, with the answer upserted as organizations). The value is checked when it is stored
(`integrations._refuse_a_callback_this_server_must_not_call`), but a name checked once can resolve
differently later. These requests go through the pinned helpers, held at every call to the same
boundary the stored value passed: https, no private network, and no redirect, so a credential
never follows a hop.
"""

from __future__ import annotations

from typing import Mapping, Optional, Tuple

import httpx

from app.core.safe_http import SafeHTTPError, fetch_guarded, post_pinned

CALLBACK_TIMEOUT = httpx.Timeout(connect=3.0, read=15.0, write=5.0, pool=2.0)
# What the receiver says back is read for a log line, never stored.
RESPONSE_HEAD_BYTES = 64 * 1024
# An organization list of the size the sync accepts (1000 organizations) fits well inside this.
ORGANIZATIONS_MAX_BYTES = 5 * 1024 * 1024


async def post_to_app(
    url: str,
    *,
    content: bytes,
    headers: Mapping[str, str],
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> Tuple[int, bytes]:
    """POST to the app; returns (status, first bytes of the answer) or raises SafeHTTPError."""
    status, head, _seen = await post_pinned(
        url,
        content=content,
        headers=headers,
        max_response_bytes=RESPONSE_HEAD_BYTES,
        timeout=CALLBACK_TIMEOUT,
        transport=transport,
        exemptions=(),
        require_https=True,
    )
    if not 200 <= status < 300:
        raise SafeHTTPError(f"the app answered {status}", status_code=status)
    return status, head


async def get_from_app(
    url: str,
    *,
    headers: Mapping[str, str],
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> bytes:
    """GET from the app; returns the body or raises SafeHTTPError (status_code set for 4xx/5xx)."""
    _content_type, body = await fetch_guarded(
        url,
        max_bytes=ORGANIZATIONS_MAX_BYTES,
        timeout=CALLBACK_TIMEOUT,
        max_redirects=0,
        require_https=True,
        headers=dict(headers),
        transport=transport,
    )
    return body
