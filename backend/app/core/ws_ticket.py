"""Single-use, short-lived tickets for authenticating WebSocket connections.

Browsers cannot set an Authorization header on a WebSocket handshake, so the old
approach put the bearer JWT in the query string (``?token=<JWT>``) where it leaks
into devtools, browser history, Referer headers and proxy/access logs. Instead the
client exchanges its token for an opaque one-time ticket through an authenticated
HTTP call, then connects with ``?ticket=<opaque>``.

Properties:
- opaque random value (no identity material in the URL)
- short TTL (seconds), so a captured ticket is useless almost immediately
- single-use (deleted on redeem), so it cannot be replayed
- bound to a specific instance/host/project, so it cannot be reused elsewhere

Backed by the shared CacheManager (Redis) so a ticket minted on one uvicorn worker
can be redeemed on another (the backend runs with multiple workers).
"""

import json
import logging
import secrets
from typing import Optional

from app.core.auth import AuthenticatedUser
from app.core.cache import get_cache_manager

logger = logging.getLogger(__name__)

_NAMESPACE = "ws_ticket"
TICKET_TTL_SECONDS = 60


def create_ticket(
    user: AuthenticatedUser,
    *,
    scope: str = "instance",
    instance: Optional[str] = None,
    host: Optional[str] = None,
    project: str = "default",
    console_type: Optional[str] = None,
) -> tuple[str, int]:
    """Mint a single-use ticket bound to (scope, instance, host, project) for ``user``.

    ``scope`` separates per-instance tickets (terminal/console) from global ones
    (ex; "monitoring") so a ticket for one channel cannot be redeemed on another.
    """
    ticket = secrets.token_urlsafe(32)
    # note: store only the minimal identity the redeem path needs (roles/groups drive
    # resolve_roles and the monitoring admin re-check). Do NOT persist raw_claims - that
    # would put the full JWT in Redis with no benefit.
    payload = {
        "user": {
            "user_id": user.user_id,
            "email": user.email,
            "name": user.name,
            "groups": user.groups,
            "roles": user.roles,
        },
        "scope": scope,
        "instance": instance,
        "host": host,
        "project": project or "default",
        "console_type": console_type,
    }
    get_cache_manager().set_ephemeral(_NAMESPACE, ticket, json.dumps(payload), TICKET_TTL_SECONDS)
    return ticket, TICKET_TTL_SECONDS


def redeem_ticket(
    ticket: Optional[str],
    *,
    scope: str = "instance",
    instance: Optional[str] = None,
    host: Optional[str] = None,
    project: str = "default",
    consume: bool = True,
) -> Optional[AuthenticatedUser]:
    """Validate a ticket and return the bound user, or None if invalid.

    ``consume`` (default True) deletes the ticket on lookup so it cannot be replayed -
    a target mismatch still burns it. Set ``consume=False`` for the graphics console:
    the SPICE client opens one WebSocket per channel (main/display/inputs/cursor), all
    reusing the same ticket, so it must survive multiple redeems within its short TTL.
    Returns None if the ticket is missing, expired, malformed, or bound to a different
    scope/instance/host/project.
    """
    if not ticket:
        return None
    if consume:
        # Atomic single-use consume (Redis GETDEL when available) - two concurrent
        # redeems cannot both win, and no stale per-worker memory copy can satisfy a
        # later redeem. get_and_delete returns the raw JSON string.
        raw = get_cache_manager().get_and_delete(_NAMESPACE, ticket)
    else:
        # Multi-use within TTL (graphics console). Non-consuming peek; cache.get returns
        # the already-parsed dict, handled by the payload branch below. The ticket
        # expires on its own TTL, bounding the replay window to that instance.
        raw = get_cache_manager().get(_NAMESPACE, ticket)
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        logger.warning("WS ticket payload could not be parsed")
        return None
    if not isinstance(payload, dict):
        return None

    expected_project = project or "default"
    if (
        payload.get("scope") != scope
        or payload.get("instance") != instance
        or payload.get("host") != host
        or payload.get("project") != expected_project
    ):
        logger.warning("WS ticket target mismatch (scope/instance/host/project)")
        return None

    u = payload.get("user") or {}
    return AuthenticatedUser(
        user_id=u.get("user_id", ""),
        email=u.get("email"),
        name=u.get("name"),
        groups=u.get("groups") or [],
        roles=u.get("roles") or [],
        raw_claims=u.get("raw_claims") or {},
    )
