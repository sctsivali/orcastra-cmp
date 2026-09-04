"""Per-target rate limits for desktop actions.

Keyed per `(key, cluster, project, instance)` rather than per key, for two reasons. One runaway
agent on one guest must not starve another guest the same key is legitimately driving, and raising
the per-key limit for an unrelated integration must not silently widen this.

Two buckets, because the costs are not comparable. An action is one short command inside the
guest. A capture encodes and ships megabytes and then costs the model roughly 1334 visual tokens
to read, so twenty a minute is already generous for a see-act loop and sixty would not be.
"""

import logging
from typing import Optional, Tuple

from app.middleware.security import _shared_allow

from .runtime import DESKTOP_ACTIONS_PER_MINUTE, DESKTOP_SCREENSHOTS_PER_MINUTE

logger = logging.getLogger(__name__)

__all__ = ["consume", "_bucket_key"]

_WINDOW_SECONDS = 60


def _bucket_key(kind: str, key_id: str, cluster_id: str, project: str, instance: str) -> str:
    return f"desktop:{kind}:{key_id}:{cluster_id}:{project}:{instance}"


def consume(
    *,
    key_id: str,
    cluster_id: str,
    project: str,
    instance: str,
    capture: bool,
    fail_closed: bool,
) -> Tuple[bool, Optional[int]]:
    """Spend one action against this target's budget. Returns (allowed, retry_after_seconds).

    A capture spends BOTH buckets: it is an action as well as a capture, so a screenshot loop
    cannot dodge the action ceiling by never doing anything else.

    important: `fail_closed` is the deliberate exception to this codebase's general rule. Every
    other limiter here fails open, and `_shared_allow` documents why: a limiter that rejects when
    its store is unreachable turns a Redis outage into a total product outage. That reasoning
    holds for reads and for uploads. It does not hold for typing into a customer's desktop, where
    the cost of allowing an unbounded number during an outage is higher than the cost of refusing
    them, so input fails closed and everything else keeps the house rule.
    """
    checks = [("actions", DESKTOP_ACTIONS_PER_MINUTE)]
    if capture:
        checks.append(("captures", DESKTOP_SCREENSHOTS_PER_MINUTE))

    for kind, limit in checks:
        allowed = _shared_allow(
            _bucket_key(kind, key_id, cluster_id, project, instance), limit, _WINDOW_SECONDS
        )
        if allowed is None:
            if fail_closed:
                logger.warning(
                    "Refusing desktop input for key %s on %s/%s: the shared budget store cannot "
                    "answer, and input fails closed",
                    key_id, cluster_id, instance,
                )
                return False, _WINDOW_SECONDS
            continue
        if not allowed:
            return False, _WINDOW_SECONDS
    return True, None
