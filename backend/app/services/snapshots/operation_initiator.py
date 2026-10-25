"""Who started a snapshot operation, so that person can follow it to its end.

`GET /operations/{id}` admits a caller only through the project's single owning organization, and
the `default` project of a cluster shared by several organizations never has one, admin included.
The console then could not learn how a snapshot delete it had just sent ended. A snapshot write
records its initiator here, and `instance_snapshot_follow.py` lets exactly that caller read that
operation's status, on that instance, and nothing else.

important: best effort in both directions. A record that cannot be written, or a shared cache
that is down (a per-worker memory fallback another worker cannot see), leaves the caller with a
404, which the console already treats as "outcome unknown". Nothing here widens access.
"""

import logging
from dataclasses import dataclass
from typing import Any, Optional

from app.core.cache import get_cache_manager

logger = logging.getLogger(__name__)

INITIATOR_KEY_PREFIX = "snapshot_op_initiator:"
# note: the console follows an operation for up to 30 minutes (use-lxd.ts COPY_OPERATION_MAX_POLL_MS).
INITIATOR_TTL_SECONDS = 3600


@dataclass(frozen=True)
class SnapshotOperationInitiator:
    actor: str
    host: str
    project: str
    instance: str


def actor_of(user: Any) -> str:
    """The caller's immutable subject, so a record matches one account and not an editable email.

    note: `user_id` is the email when the token carries one (`core/auth.py`), and an email can be
    reassigned or collide with another account's subject; `sub` cannot. A token without `sub` (the
    auth-disabled dev user) falls back to `user_id`, under a different prefix so it can never equal
    a subject.
    """
    claims = getattr(user, "raw_claims", None) or {}
    subject = claims.get("sub") if isinstance(claims, dict) else None
    if subject:
        return f"sub:{subject}"
    return f"user:{getattr(user, 'user_id', '')}"


def _key(host: str, operation_id: str) -> str:
    return f"{INITIATOR_KEY_PREFIX}{host}:{operation_id}"


def remember(
    host: str, operation_id: Optional[str], *, actor: str, project: str, instance: str
) -> None:
    if not operation_id or not actor:
        return
    try:
        get_cache_manager().set(
            "api_responses",
            _key(host, operation_id),
            {"actor": actor, "host": host, "project": project, "instance": instance},
            ttl=INITIATOR_TTL_SECONDS,
        )
    except Exception:  # noqa: BLE001 - the follow degrades to "unknown", the write is unaffected
        logger.warning("Could not record the initiator of snapshot operation %s on %s", operation_id, host)


def recall(host: str, operation_id: str) -> Optional[SnapshotOperationInitiator]:
    try:
        stored = get_cache_manager().get("api_responses", _key(host, operation_id))
    except Exception:  # noqa: BLE001 - unreadable is the same answer as absent: no access
        return None
    if not isinstance(stored, dict):
        return None
    try:
        return SnapshotOperationInitiator(
            actor=str(stored["actor"]),
            host=str(stored["host"]),
            project=str(stored["project"]),
            instance=str(stored["instance"]),
        )
    except KeyError:
        return None
